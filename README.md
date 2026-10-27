# ai-toolkit-observability
Observability and evaluation for ai-toolkit: Langfuse stack, OpenTelemetry collector, scores and dashboards

## Langfuse stack

One shared, self-hosted [Langfuse v4][langfuse-compose] for every project that uses the
toolkit: web, worker, Postgres, ClickHouse, Redis and MinIO, defined in `compose.yaml`
with pinned images and named volumes. Only the web UI is published, on
`127.0.0.1:3000`.

```sh
scripts/langfuse.sh up      # first run creates the secrets, the org, the project, the keys
scripts/langfuse.sh status  # prints "up" (exit 0) or "down" (exit 1)
scripts/langfuse.sh down    # stops the stack, keeps the data volumes
```

Requires Docker Compose, `openssl` and the macOS Keychain. `up` warns when Docker has
less than 16 GB of memory, the docs' recommendation for this stack.

### Secrets

Every secret is generated once and kept in the macOS Keychain, account `$USER`, service
`ai-toolkit-langfuse-<NAME>`: the project key pair (`LANGFUSE_INIT_PROJECT_PUBLIC_KEY`,
`LANGFUSE_INIT_PROJECT_SECRET_KEY`), the admin password (`LANGFUSE_INIT_USER_PASSWORD`),
the datastore passwords, the encryption key and the salt. They reach Docker through the
environment only. A second `up` reuses them. If the data volumes exist but the Keychain
items are gone, `up` stops instead of generating passwords the databases would reject.

Sign in to the UI at <http://localhost:3000> as `admin@ai-toolkit.local` with:

```sh
security find-generic-password -a "$USER" -s ai-toolkit-langfuse-LANGFUSE_INIT_USER_PASSWORD -w
```

### Send the collector's traces here

Restart the collector from the `ai-toolkit` checkout with the same key pair, so traces
flow to the `ai-toolkit` project. Nothing in that checkout is edited:

```sh
kc() { security find-generic-password -a "$USER" -s "ai-toolkit-langfuse-$1" -w; }
LANGFUSE_PUBLIC_KEY="$(kc LANGFUSE_INIT_PROJECT_PUBLIC_KEY)" \
LANGFUSE_SECRET_KEY="$(kc LANGFUSE_INIT_PROJECT_SECRET_KEY)" \
  path/to/ai-toolkit/scripts/otel.sh up
```

Each worker then appears as a session named by its `spoke_run_id`. In v4 the legacy
`/api/public/sessions` and `/api/public/observations` endpoints are disabled; read with
`/api/public/v2/observations` instead.

### Limits

MinIO is not published, so browser media uploads and batch-export download links in the
UI do not work. Trace ingestion uses MinIO internally and is unaffected.

## Evaluate the toolkit

`scripts/evaluate.py` joins every worker session to its issue and outcome, cuts the session
into cycle steps, prices each step and each toolkit component, and writes the numbers back
to Langfuse as scores on the session. Standard library only; the stack must be up.

```sh
scripts/evaluate.py --issue 1                      # report + scores for one issue
scripts/evaluate.py --since 2026-10-01             # every session of this repo that started since then
scripts/evaluate.py --since 2026-10-01 --repo ai-toolkit=mcrilo33/ai-toolkit=~/Repos/ai-toolkit   # and of ai-toolkit
scripts/evaluate.py --issue 1 --no-publish --json  # machine-readable, pushes nothing
scripts/evaluate.py --issue 7 --session <spoke_run_id>   # force the join when it fails
```

All host repos share one collector and one Langfuse, and each event carries a `repo`
attribute (the bare repo name). The repo of the current directory is always scored. Add
others with `--repo NAME=OWNER/REPO=PATH` (repeatable): `NAME` is the telemetry name,
`OWNER/REPO` the GitHub repo its issues are read from (`gh -R`), `PATH` the local checkout
its commits and landing are read from (`git -C`). Each session is joined against its own
repo, so the same issue number in two repos never collides; `--issue N` matches that number
in every mapped repo. A session of a repo that is not mapped is not scored but listed in a
final `skipped:` line (and under `skipped` in `--json`), never dropped silently (except with
`--issue`, which lists no skipped sessions: their issue is unknown). The
Sessions table has a `repo` column.

### Sessions Langfuse never received

A session that has records in the raw JSONL but no Langfuse span at all (the collector's
export was rejected, as happened from 2026-10-21 to 2026-10-24) is still scored. It starts
at its first raw record, its repo is the `repo` attribute of its records, and it then goes
through the same window, `--session`, `--issue` and unmapped-repo rules. Its `environment`
is that `repo`, its toolkit version the raw `toolkit_version` attribute, and its score ids
are the same `sha1(session:name)` a Langfuse-backed run would write, so a later run upserts
them. The report marks it `(raw only)` in the session header, in the `raw_only` column and
in `--json`, with a note. These scores are not computed, because they need spans, and are
never sent as 0: `tool_<name>_long_waits` and `tool_<name>_long_wait_s` (they need
`blocked_on_user` spans) and `rule_<name>_cost_usd` (the raw records give only a call's
total cost, no input cost). `waiting_s` is a lower bound for the same reason: only the
coordinator's gate and permission comments count. Sub-agent attribution (`agent_<name>_*`
and the review step) comes from the `agent.name` of the raw requests; a raw tool call is
attributed to its sub-agent only when its `agent_id` matches one of those requests.

To backfill a gap from a backup of the raw files (`--raw-dir` reads every `logs*.jsonl` in it):

```sh
scripts/evaluate.py --raw-dir ~/.ai-toolkit/otel-raw-backup --since 2026-10-21T17:00 --until 2026-10-24T07:00 --repo ai-toolkit=mcrilo33/ai-toolkit=~/Repos/ai-toolkit
```

Add a `--repo NAME=OWNER/REPO=PATH` for every other repo whose gap sessions should be scored.

Keys come from the Keychain (`ai-toolkit-langfuse-LANGFUSE_INIT_PROJECT_{PUBLIC,SECRET}_KEY`)
at runtime and travel only in an `Authorization` header. If the stack is down the run stops
before any score is written. Everything is read and computed first; scores are written last,
and a re-run upserts: a score's id is a hash of the session and its name, so no duplicate.
(Langfuse v4 cannot read scores back through the API; the UI shows the latest value.)

Per session the scores are `cost_usd_total`, `duration_active_s`, `waiting_s`, `other_s`,
`step_<plan|implement|review|ship>_{cost_usd,active_s}`, `review_rounds`, `redispatches`,
`landed` (boolean), `review_tdd_followed` and `review_tests_weakened` (booleans, only when
a review verdict or coordinator rejection judged the session, see the source table) and `gate_lane` (categorical).
The report adds per-step, per-agent, per-tool, per-hook and per-rule tables, the zero-use on-demand
rules, and a static token-size table of the always-on rules (cost only, usefulness is not
measurable here).

Components are scores too, summed over the steps, one score name per component because a
Langfuse dashboard widget groups scores by name and has no comment or metadata dimension:
`agent_<name>_{cost_usd,calls,llm_s}` (`main` included),
`tool_<name>_{calls,failures,long_waits,long_wait_s}`, `hook_<name>_{runs,total_ms,blocking,avg_ms}`
`rule_<name>_reads`, the context cost `rule_<name>_{cost_usd,loaded_requests}` (below) and
the review's violation counts `rule_<name>_{blockers,warnings}`: how many times
the reviews that judged the session named that rule as a blocker or a warning (a rule named twice
counts twice), `none` being a finding no rule covers. Bash is split by the first program of the command (after a leading
`cd <dir> &&`, `VAR=value`, `timeout <n>` or `env`) into `tool_bash-<group>_*`, where the group
is one of `read` (grep, sed, cat, ls, ...), `git`, `gh`, `orca`, `test` (a bare `pytest` or `shellcheck`; `python -m pytest` counts as `python`),
`python`, `toolkit` (a program under `.ai-toolkit/scripts/`) or `other`; `tool_Bash_*` is no
longer sent. An MCP tool `mcp__<server>__<tool>` is grouped per server into `tool_mcp-<server>_*`;
the collector's raw JSONL only says `mcp_tool` with no server, so a raw-only MCP call is
published as `tool_mcp-unknown_*`. `hook_<name>_avg_ms` is `total_ms / runs` for the session,
the mean time per run that a dashboard cannot compute from the other two, and is only sent when
the hook ran. Only non-zero values are published, so a component that did
not run, fail or block adds no score. The exceptions are `rule_<name>_reads`,
`agent_<name>_calls` and `tool_mcp-<server>_calls`; the violation counts are non-zero only. Every on-demand
rule in `--rules-dir` gets a score for every scored session, 0 when unread, so the "On-demand rule
reads" widget lists the unused rules (a fixed cost of one score per rule, 6 today). Likewise every toolkit agent in `--agents-dir`
(`.claude/agents`, 13 today) gets `agent_<name>_calls` for every scored session, 0 when the session
launched none, so the unused agents show up; `_cost_usd` and `_llm_s` are never zero-filled, so
their averages stay "when used", and neither `main` nor a built-in agent such as `Explore` gets a
zero. Likewise every server in `--mcp-config` (`.ai-toolkit/mcp.json`, the `mcpServers` keys, the
only servers a worker can use; 0 today) gets `tool_mcp-<server>_calls` for every scored session,
0 when it was not called, so the "MCP servers: calls (total)" widget lists the unused ones. A
missing or empty file, or an empty `mcpServers`, gives no zero and no error. Failures and waits
are never zero-filled, and `mcp-unknown` never gets a zero. A raw-only MCP call is published as
`tool_mcp-unknown_calls` with no server name, and could have been to any listed server, so a
session with one gets no MCP zero at all: a false 0 would be worse than a missing one. Read a zero
with care: `afk-answering`, `planning-hub` and `workflow` are mostly read by coordinator sessions,
which send no telemetry, so for them 0 means "not tracked", not "unused". A long wait is a tool
call blocked on the user for 10 s or more; shorter waits are automatic permission checks. A
session publishes at most 3 x agents + 4 x tools + 4 x hook names + 1 x on-demand rules + 1 x toolkit agents +
1 x listed MCP servers plus 2 x review rule names (up to 38 in the worst case, 2 x the 18 rules plus `none`; a handful in
practice) plus 2 x loaded rules (`cost_usd` and `loaded_requests`, up to 36 for the 18 rules)
scores on top of its 18, where Bash adds up to 8 groups x 4 scores instead of 4 and the MCP tools add 4
scores per server instead of 4 per tool; a hook name is its event and matcher, so each MCP tool
adds hook scores. A re-run overwrites a published score but never clears one that has since
become zero, so sessions scored before the Bash split keep their `tool_Bash_*` scores and
sessions scored before the MCP grouping keep their `tool_mcp__*` scores, with the new names added
next to them.

`rule_<name>_cost_usd` is what a rule cost the session in input tokens: the sum, over the LLM
calls in which the rule was in context, of `min(rule_tokens / call_input_tokens, 1) x
call_input_cost`, so it uses each call's real prices and caching (a rule is mostly paid at the
cache-read price). `rule_<name>_loaded_requests` counts those calls. Both are non-zero only, so a
rule that never loaded publishes neither. Compare `loaded_requests` with the session's LLM calls
(the sum of `agent_*_calls`) to see how much a `paths:` scoping saves.

A rule is in context, per Claude session (a re-dispatch starts a fresh context), from:

- its first LLM call, for an always-on rule without `paths:`;
- the first `Read`, `Edit` or `Write` whose path matches one of its `paths:` globs, for a
  path-scoped rule. The globs are repo-relative and the paths absolute, so a glob matches the
  path or any suffix of it that starts after a `/`; `**`, `*`, `?` and `{a,b}` are supported;
- its first read, detected as for `rule_<name>_reads`, for an on-demand rule;

and stays until the session ends. Rule sizes are `characters / 4` of the text at the session's
toolkit commit, read with `git show <sha>:shared/rules/[on-demand/]<name>.md` in the checkout
mapped as `ai-toolkit` (the `--repo ai-toolkit=...` of the sweep); the commit is the
`toolkit_version` attribute of the session's spans, minus `-dirty`. A session without a version
(before ai-toolkit #478), an unmapped `ai-toolkit` or a failing `git show` uses the file in
`--always-on-dir` / `--rules-dir` as it is today. Approximations:

- Path-scoped loading is approximated by the first matching `Read`/`Edit`/`Write`; Claude
  Code's real trigger may differ.
- Sub-agent calls in the same Claude session are treated like the main context.
- Context compaction is ignored: a rule stays loaded.
- Tokens are `characters / 4`, and a `-dirty` version uses the committed text.
- A raw-only LLM call (no Langfuse span) counts in `loaded_requests` but adds no cost, as the
  raw JSONL has no input cost.
- Only the rules present in the current directories are listed, and one added after the
  session's commit is measured with today's text.

### Which source answers which metric

| Metric | Source | Notes |
| ------ | ------ | ----- |
| Session, cost, tokens, per-sub-agent cost | Langfuse `/api/public/v2/observations` | Survives raw-file rotation |
| Tool calls, failures, p50/p90 latency | Langfuse tool spans | Latency excludes `blocked_on_user` |
| Hooks (runs, total ms, blocking) | Raw JSONL `hook_execution_complete` | Named by event:matcher, not by script |
| Spans Langfuse never received | Raw JSONL `api_request`, `tool_result` | Deduped by `request_id` / `tool_use_id`; the report says how many |
| Issue number | Raw `vcs.ref.head.name`, then the `/<n>-<slug>` worktree path in tool commands | `--issue N --session ID` overrides |
| `gate_lane`, labels | `gh issue view`: the `Gate:` footer | |
| Gate answer, permission/gate waits, `blocked:`, `landed in` | `gh` issue comments written by the coordinator loop | Timestamps drive the segmentation |
| `landed` | The `landed in <sha>` comment, checked with `git merge-base --is-ancestor` | An unknown SHA leaves the comment as evidence |
| `review_tdd_followed`, `review_tests_weakened` | The toolkit's `review-verdict: {...}` issue comment (`"v":1`), posted on every review since 2026-10-22, approvals included: `tdd_followed` and `tests_weakened` are its own fields. Older issues keep the coordinator's `round N: address: the independent review requested changes` comment: its fixed `tdd_followed is false:` and `tests_weakened:` numbered blocker lines. A rejection leaves both comments and counts once (the `round N` comment right after a verdict is skipped). A session takes every review posted during its spans (a re-dispatch keeps the spoke's session) plus the first one after them, unless a `blocked:`, attended round or `landed in` comment comes first; a flag is set if any holds it | Published only for a session a review judged. A manual `land.sh` without `--review` posts no verdict, so it publishes neither score; attended rounds and the final `blocked: review still rejects` comment carry no flags themselves, but the verdict before them does. For sessions before 2026-10-22 an approval leaves no trace, so `landed` is the denominator, a proxy that a manual `land.sh` makes look lower than the true rate. A malformed verdict (bad JSON, `v` other than 1, non-list rule names) is ignored |
| `rule_<name>_blockers`, `rule_<name>_warnings` | The `blockers` and `warnings` rule names of the same `review-verdict` comments, one count per name listed (a name listed twice counts twice); names come from the reviewer's fixed rule inventory plus `none` | Old-style `round N` rejections name no rule, so sessions before 2026-10-22 publish none; only non-zero counts are sent |
| `review_rounds` | `sub-agent:code-review` spans, or raw `Agent` tool results with `subagent_type` `code-review`; the larger count | Comments and commits carry no review marker; a lower bound when telemetry is incomplete |
| `redispatches` | Distinct Claude `session.id` in Langfuse and the raw JSONL, minus one, or the `blocked:` comment count if larger | A lower bound when telemetry is incomplete |
| On-demand rule loads | `Read` of `.ai-toolkit/rules/<name>.md`, or a Bash `cat`/`sed`/`head`/`grep` of it | Workers use Bash, so both count |
| Always-on rule size | Files in `.claude/rules/ai-toolkit/` (the terminal table), and per session `git show <toolkit_version>:shared/rules/<name>.md` | `characters / 4`; `paths:` frontmatter marks a rule path-scoped |
| `rule_<name>_cost_usd`, `rule_<name>_loaded_requests` | Per LLM call `inputUsage` / `inputCost` (raw: the three input token counts, no cost), the rule text above and the load model above | Approximations listed above |

### How time is segmented

Time is counted on the wall clock, not summed per event, so parallel sub-agents do not
inflate it. The session is cut into segments; each is `waiting`, `active` or `other`:

- **waiting**: a human was asked. The union of `blocked_on_user` spans, permission requests
  answered by a "Permission answered" comment (matched by tool), and a gate question open
  until the gate is answered, the worker reports `blocked:`, or it lands. Waiting beats
  active, so a 25-minute `orca orchestration ask` is not charged to a step.
- **active**: at least one LLM request or tool span is running.
- **other**: no span and no wait: idle gaps, and the time between two dispatches (also
  reported on its own).

A segment's step comes from its time: **review** while the in-spoke `code-review` agent's
spans run; **plan** before the gate-answer comment; **ship** for each `git push` command
and from the last push before `worker_done` to `worker_done`; otherwise **implement**.
Active implement time is split RED / GREEN / REFACTOR by the kind of the next commit
carrying `#<n>` (`test`, `feat`/`fix`, `refactor`; anything else is `other`).

### Known limits

- Langfuse has no span from before the collector exported to it. Those spans are filled from
  the raw JSONL, whose sub-agent is known only where a request names it, and the report lists
  them. When the raw file has rotated away too, the session keeps its trace-level numbers and
  says what is missing.
- `review_rounds` and `redispatches` are lower bounds whenever Langfuse or the raw JSONL lacks part of the session; the report says so in a note.
- `vcs.ref.head.name` is on a handful of events, so the issue join leans on the worktree path;
  a session that never touched it needs `--issue`.
- `--since`/`--until` pick whole sessions by their first span (first raw record for a session Langfuse never received); a session is always scored from all its spans.
- Review time is the review agent's own spans; the main thread's work around them stays in its step.
- A permission request nobody answered is a wait only when a `blocked_on_user` span confirms it.
- A re-dispatch is the same spoke with a new Claude session; the gap before it is `other`.
- Step boundaries are heuristics: no explicit step markers exist, and commit time is the end of
  the work it labels. A Bash `cat` of a rule is counted as a load even if the file was only skimmed.
- Hooks are identified by event and matcher (`PreToolUse:Bash`), not by the script that ran.

## Automatic scoring

Raw JSONL rotates away after about three days, so a session nobody scores by then never
gets its scores. `scripts/evaluate_sweep.sh` keeps them current: it runs
`scripts/evaluate.py --since <7 days ago>` over this repo and `ai-toolkit` (the `--repo`
mapping is the `toolkit_repo` line of the script; edit it if your checkout lives elsewhere;
a re-score is an upsert, so overlap is harmless) and exits with its status. A macOS launchd agent runs it daily at 03:17, or at
wake-up if the Mac was asleep.

```sh
scripts/evaluate_sweep.sh            # one sweep now
scripts/evaluate_sweep.sh install    # load the daily agent for this checkout (idempotent)
scripts/evaluate_sweep.sh uninstall  # unload and remove it (idempotent)
```

Each sweep appends its report to `~/.ai-toolkit/evaluate-sweep.log`. With the stack down
it logs the error and exits non-zero without writing a score. The agent
(`launchd/com.ai-toolkit.evaluate-sweep.plist`, copied to `~/Library/LaunchAgents`) holds
no secret: the keys stay in the Keychain and are read at run time. Run `install` from the
main checkout, not a worktree that will be removed, and re-run it if the checkout moves. The
agent's `PATH` is `/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`, so `python3` (3.12+) and
`gh` must be found there.

## Development

```sh
pip install --group dev
pytest -n auto tests
```

CI runs the same command, plus `shellcheck`, on every push.

[langfuse-compose]: https://langfuse.com/self-hosting/deployment/docker-compose
