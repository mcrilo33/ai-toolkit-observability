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
scripts/evaluate.py --since 2026-10-01             # every session of this repo since then
scripts/evaluate.py --issue 1 --no-publish --json  # machine-readable, pushes nothing
scripts/evaluate.py --issue 7 --session <spoke_run_id>   # force the join when it fails
```

Keys come from the Keychain (`ai-toolkit-langfuse-LANGFUSE_INIT_PROJECT_{PUBLIC,SECRET}_KEY`)
at runtime and travel only in an `Authorization` header. If the stack is down the run stops
before any score is written. Everything is read and computed first; scores are written last,
and a re-run upserts: a score's id is a hash of the session and its name, so no duplicate.
(Langfuse v4 cannot read scores back through the API; the UI shows the latest value.)

Per session the scores are `cost_usd_total`, `duration_active_s`, `waiting_s`, `other_s`,
`step_<plan|implement|review|ship>_{cost_usd,active_s}`, `review_rounds`, `redispatches`,
`landed` (boolean) and `gate_lane` (categorical). The report adds per-step, per-agent,
per-tool, per-hook and per-rule tables, the zero-use on-demand rules, and a static
token-size table of the always-on rules (cost only, usefulness is not measurable here).

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
| `review_rounds` | Count of `sub-agent:code-review` spans | Comments and commits carry no review marker |
| `redispatches` | Distinct Claude `session.id` in one `spoke_run_id`, minus one | Cross-checked with `blocked:` comments |
| On-demand rule loads | `Read` of `.ai-toolkit/rules/<name>.md`, or a Bash `cat`/`sed`/`head`/`grep` of it | Workers use Bash, so both count |
| Always-on rule size | Files in `.claude/rules/ai-toolkit/` | `characters / 4`; `paths:` frontmatter marks a rule path-scoped |

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
  the raw JSONL, which has no sub-agent attribution, and the report lists them. When the raw
  file has rotated away too, the session keeps its trace-level numbers and says what is missing.
- `vcs.ref.head.name` is on a handful of events, so the issue join leans on the worktree path;
  a session that never touched it needs `--issue`.
- A permission request nobody answered is a wait only when a `blocked_on_user` span confirms it.
- A re-dispatch is the same spoke with a new Claude session; the gap before it is `other`.
- Step boundaries are heuristics: no explicit step markers exist, and commit time is the end of
  the work it labels. A Bash `cat` of a rule is counted as a load even if the file was only skimmed.
- Hooks are identified by event and matcher (`PreToolUse:Bash`), not by the script that ran.

## Development

```sh
pip install --group dev
pytest -n auto tests
```

CI runs the same command, plus `shellcheck`, on every push.

[langfuse-compose]: https://langfuse.com/self-hosting/deployment/docker-compose
