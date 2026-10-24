"""Attribution per step, deterministic Langfuse scores, the Langfuse client and the report."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from evaluate_time import STEPS, Accounting, EvalError, Leaf, Stepper, StepTime

ON_DEMAND_RULE = re.compile(r"\.ai-toolkit/rules/([\w-]+)\.md$")
# Workers load rules with Bash far more often than with Read: cat/sed/head/grep on the file.
BASH_READER = re.compile(r"\b(?:cat|sed|head|tail|grep|rg|less|bat|awk|nl)\b")
BASH_SEGMENT = re.compile(r"&&|\|\||[;|\n]")
RULE_PATH = re.compile(r"\.ai-toolkit/rules/([\w-]+)\.md")
NOT_A_READ = (  # commands that carry free text, where a path is quoted rather than read
    "git commit",
    "gh issue create",
    "gh issue comment",
    "gh issue edit",
    "gh pr create",
    "gh pr comment",
    "gh pr edit",
    "orca orchestration",
)
# Most blocked_on_user spans are sub-second automatic permission checks; only a wait on the
# user that lasts this long is signal worth a score.
LONG_WAIT_S = 10
FILE_TOOLS = ("Read", "Edit", "Write")  # what loads a path-scoped rule, as an approximation
GLOB_TOKENS = {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]", "{": "(?:", "}": ")"}
# A fixed group list keeps the score count bounded; a raw program name would not.
BASH_GROUPS = {
    **dict.fromkeys(("grep", "rg", "sed", "cat", "ls", "wc", "head", "tail", "find", "awk"), "read"),
    **dict.fromkeys(("pytest", "shellcheck"), "test"),
    **dict.fromkeys(("python", "python3"), "python"),
    "git": "git",
    "gh": "gh",
    "orca": "orca",
}
# What a command may start with before its real program: `cd <dir> &&`, `VAR=value`, wrappers.
BASH_PREFIX = re.compile(r"\s*(?:cd\s+\S+\s*(?:&&|;)|\w+=\S*|timeout\s+\d+\S*|env(?=\s))\s*")
BASH_PROGRAM = re.compile(r"[^\s|;&<>()]+")
# (score prefix, Attribution table, measures published): Langfuse dashboards group scores by
# name and have no comment or metadata dimension, so each component is a score name.
COMPONENT_SCORES = (
    ("agent", "agents", ("cost_usd", "calls", "llm_s")),
    ("tool", "tools", ("calls", "failures", "long_waits", "long_wait_s")),
    ("hook", "hooks", ("runs", "total_ms", "blocking")),
)


def percentile(values: list[float], q: int) -> float:
    """Return the nearest-rank q-th percentile of `values` (0.0 when empty)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(math.ceil(q / 100 * len(ordered)), 1) - 1]


@dataclass(slots=True)
class Attribution:
    """Cost and time per (step, name): sub-agents, tools and hooks, plus on-demand rule reads."""

    agents: dict[tuple[str, str], dict] = field(
        default_factory=lambda: defaultdict(
            lambda: dict(calls=0, tool_calls=0, cost_usd=0.0, tokens=0, llm_s=0.0)
        )
    )
    tools: dict[tuple[str, str], dict] = field(
        default_factory=lambda: defaultdict(
            lambda: dict(calls=0, failures=0, durations=[], long_waits=0, long_wait_s=0.0)
        )
    )
    hooks: dict[tuple[str, str], dict] = field(
        default_factory=lambda: defaultdict(lambda: dict(runs=0, total_ms=0.0, blocking=0))
    )
    rules: Counter = field(default_factory=Counter)


def _rules_read_by_bash(command: str) -> set[str]:
    """Return the rules a Bash command reads: every rule path in a segment that has a reader."""
    if any(x in command for x in NOT_A_READ):
        return set()  # quoted text in a commit message or issue body is not a read
    names: set[str] = set()
    for segment in BASH_SEGMENT.split(command):
        if BASH_READER.search(segment):
            names.update(RULE_PATH.findall(segment))
    return names


def _rules_read(leaf: Leaf) -> set[str]:
    """Return the on-demand rules a tool leaf reads: a `Read` of the file or a Bash reader."""
    if leaf.name == "Read" and (m := ON_DEMAND_RULE.search(leaf.file_path)):
        return {m[1]}
    return _rules_read_by_bash(leaf.command) if leaf.name == "Bash" else set()


def bash_group(command: str) -> str:
    """Return the group of a Bash command's first real program (`other` when unknown)."""
    command = command.lstrip()
    while prefix := BASH_PREFIX.match(command):
        command = command[prefix.end() :]
    program = BASH_PROGRAM.match(command)
    if not program:
        return "other"
    if ".ai-toolkit/scripts/" in program[0]:
        return "toolkit"
    return BASH_GROUPS.get(program[0].rsplit("/", 1)[-1], "other")


def mcp_group(name: str) -> str:
    """Return `mcp-<server>` for an `mcp__<server>__<tool>` name, else the name unchanged.

    The collector's raw JSONL names every MCP call `mcp_tool` with no server: `mcp-unknown`.
    """
    if name == "mcp_tool":
        return "mcp-unknown"
    return f"mcp-{name.split('__', 2)[1]}" if name.startswith("mcp__") else name


def attribute(leaves: list[Leaf], raw_records: list[dict], stepper: Stepper) -> Attribution:
    """Attribute leaves and raw hook records to the step they started in."""
    out = Attribution()
    for leaf in leaves:
        step = stepper.at(leaf.start)[0]
        agent = out.agents[(step, leaf.agent or "main")]
        if leaf.kind == "llm":
            agent["calls"] += 1
            agent["cost_usd"] += leaf.cost
            agent["tokens"] += leaf.tokens
            agent["llm_s"] += leaf.end - leaf.start
        elif leaf.kind == "tool":
            agent["tool_calls"] += 1
            name = f"bash-{bash_group(leaf.command)}" if leaf.name == "Bash" else mcp_group(leaf.name)
            tool = out.tools[(step, name)]
            tool["calls"] += 1
            tool["failures"] += leaf.failed
            tool["durations"].append(leaf.end - leaf.start - leaf.blocked_s)
            if leaf.blocked_s >= LONG_WAIT_S:
                tool["long_waits"] += 1
                tool["long_wait_s"] += leaf.blocked_s
            out.rules.update(_rules_read(leaf))
    for rec in raw_records:
        if rec["name"] != "claude_code.hook_execution_complete":
            continue
        hook = out.hooks[(stepper.at(rec["t"])[0], rec["attrs"]["hook_name"])]
        hook["runs"] += 1
        hook["total_ms"] += float(rec["attrs"].get("total_duration_ms", 0))
        hook["blocking"] += int(rec["attrs"].get("num_blocking", 0))
    return out


@dataclass(slots=True, frozen=True)
class RuleSize:
    name: str
    chars: int
    tokens: int
    scoped: bool
    globs: tuple[str, ...] = ()


def measure_rule_sizes(
    rules_dir: Path, read: Callable[[Path], str] = Path.read_text
) -> list[RuleSize]:
    """Estimate the tokens each rule file adds (chars / 4); `paths:` frontmatter marks it scoped.

    `read` returns a rule's text, so a caller can supply the text at another commit; the
    quoted globs under `paths:` (block or inline list) are kept for path matching.
    """
    sizes = []
    for path in sorted(rules_dir.glob("*.md")):
        text = read(path)
        head = text.split("---", 2)[1] if text.startswith("---") and text.count("---") >= 2 else ""
        paths = re.search(r"(?ms)^paths:(.*?)(?=^\S|\Z)", head)
        globs = tuple(re.findall(r"""['"]([^'"]+)['"]""", paths[1])) if paths else ()
        sizes.append(RuleSize(path.stem, len(text), len(text) // 4, "paths:" in head, globs))
    return sizes


@cache
def _glob_regex(glob: str) -> re.Pattern:
    """Compile a repo-relative glob (`**`, `*`, `?`, `{a,b}`) into a full-match regex."""
    out, depth = [], 0
    for token in re.split(r"(\*\*/|\*\*|\*|\?|\{|\}|,)", glob):
        depth += (token == "{") - (token == "}")
        out.append("|" if token == "," and depth else GLOB_TOKENS.get(token, re.escape(token)))
    return re.compile("".join(out))


def _matches_any(globs: tuple[str, ...], path: str) -> bool:
    """Tell whether a glob matches `path` or one of its suffixes starting after a `/`."""
    parts = path.strip("/").split("/")
    suffixes = ["/".join(parts[i:]) for i in range(len(parts))]
    return any(_glob_regex(g).fullmatch(s) for g in globs for s in suffixes)


def compute_rule_costs(
    leaves: list[Leaf], always_on: list[RuleSize], on_demand: list[RuleSize]
) -> dict[str, dict]:
    """Return each rule's `cost_usd` and `loaded_requests` over the LLM calls it was in context.

    Per Claude session (a re-dispatch starts a fresh context), an always-on rule loads at the
    first call, a path-scoped one when a Read/Edit/Write matches a glob, an on-demand one at its
    first read; it then stays until the session ends. Every call from there adds a request and,
    when its input size is known, the rule's share of the call's input cost.
    """
    by_claude: dict[str, list[Leaf]] = defaultdict(list)
    for leaf in leaves:
        by_claude[leaf.claude].append(leaf)
    costs: dict[str, dict] = {}
    for group in by_claude.values():
        calls = [lf for lf in group if lf.kind == "llm"]
        first_read: dict[str, float] = {}
        for lf in group:
            for name in _rules_read(lf):
                first_read[name] = min(lf.end, first_read.get(name, lf.end))
        loads = {r: -math.inf for r in always_on if not r.scoped}
        for rule in (r for r in always_on if r.scoped):
            hits = [
                lf.end
                for lf in group
                if lf.name in FILE_TOOLS and _matches_any(rule.globs, lf.file_path)
            ]
            if hits:
                loads[rule] = min(hits)
        loads |= {r: first_read[r.name] for r in on_demand if r.name in first_read}
        for rule, loaded_at in loads.items():
            for call in (c for c in calls if c.start >= loaded_at):
                total = costs.setdefault(rule.name, dict(cost_usd=0.0, loaded_requests=0))
                total["loaded_requests"] += 1
                if call.input_tokens > 0:
                    share = min(rule.tokens / call.input_tokens, 1)
                    total["cost_usd"] += share * call.input_cost
    return costs


def list_unused_rules(rules_dir: Path, used: dict[str, int]) -> list[str]:
    """Return the on-demand rule files in `rules_dir` that were never read."""
    return sorted(p.stem for p in rules_dir.glob("*.md") if p.stem not in used)


# --------------------------------------------------------------------------- reports


@dataclass(slots=True, kw_only=True)
class SessionReport:
    session: str
    repo: str
    issue: int | None
    title: str = ""
    gate_lane: str | None = None
    labels: list[str] = field(default_factory=list)
    landed: bool = False
    review_tdd_followed: bool | None = None  # None: no review judged this session
    review_tests_weakened: bool | None = None
    rule_blockers: Counter = field(default_factory=Counter)  # rule name -> times blamed as a blocker
    rule_warnings: Counter = field(default_factory=Counter)
    rule_costs: dict[str, dict] = field(default_factory=dict)  # rule name -> cost_usd, loaded_requests
    review_rounds: int = 0
    redispatches: int = 0
    blocked_comments: int = 0
    accounting: Accounting
    attribution: Attribution
    notes: list[str] = field(default_factory=list)
    started: float = 0.0
    environment: str | None = None  # the repo named by the session's own spans; None if unnamed
    raw_only: bool = False  # no Langfuse span: analysed from the raw JSONL alone


def _score(rep: SessionReport, name: str, value, data_type: str) -> dict:
    digest = hashlib.sha1(f"ai-toolkit-eval:{rep.session}:{name}".encode()).hexdigest()
    score = {
        "id": digest,
        "sessionId": rep.session,
        "name": name,
        "value": value,
        "dataType": data_type,
        "comment": f"evaluate.py, issue #{rep.issue}",
    }
    return score | {"environment": rep.environment} if rep.environment else score


def _component_measures(
    attribution: Attribution, on_demand_rules: Iterable[str], toolkit_agents: Iterable[str]
) -> dict[str, float]:
    """Sum each component's measures over the steps, keeping only the non-zero ones.

    A component that did not run, or never failed or waited, publishes nothing: a dashboard
    sums a missing point as zero, and the per-session score count stays bounded by what ran.
    The exceptions are always published so that an unused one shows up on the dashboard: an
    on-demand rule's reads and a toolkit agent's calls. A hook's mean time per run is
    published whenever the hook ran, even at 0 ms, and never otherwise.
    """
    always = [f"rule_{name}_reads" for name in on_demand_rules]
    always += [f"agent_{name}_calls" for name in toolkit_agents]
    totals: Counter = Counter(dict.fromkeys(always, 0))
    for prefix, table, measures in COMPONENT_SCORES:
        for (_step, name), stats in getattr(attribution, table).items():
            for measure in measures:
                totals[f"{prefix}_{name}_{measure}"] += stats[measure]
    for name, reads in attribution.rules.items():
        totals[f"rule_{name}_reads"] += reads
    for name in dict.fromkeys(name for _step, name in attribution.hooks):
        if runs := totals[f"hook_{name}_runs"]:
            always.append(f"hook_{name}_avg_ms")
            totals[f"hook_{name}_avg_ms"] = totals[f"hook_{name}_total_ms"] / runs
    return {name: v for name, v in totals.items() if v or name in always}


def build_scores(
    rep: SessionReport,
    *,
    on_demand_rules: Iterable[str] = (),
    toolkit_agents: Iterable[str] = (),
) -> list[dict]:
    """Build a session's Langfuse scores; ids are deterministic, so a re-run upserts.

    `on_demand_rules` names every on-demand rule, each scored `rule_<name>_reads` even at 0.
    `toolkit_agents` names every toolkit agent, each scored `agent_<name>_calls` even at 0.
    """
    acct = rep.accounting
    numeric = {
        "cost_usd_total": acct.cost_usd,
        "duration_active_s": acct.active_s,
        "waiting_s": acct.waiting_s,
        "other_s": acct.other_s,
        "review_rounds": rep.review_rounds,
        "redispatches": rep.redispatches,
    }
    for step, slot in acct.steps.items():
        numeric[f"step_{step}_cost_usd"] = slot.cost_usd
        numeric[f"step_{step}_active_s"] = slot.active_s
    numeric |= _component_measures(rep.attribution, on_demand_rules, toolkit_agents)
    for kind, counts in (("blockers", rep.rule_blockers), ("warnings", rep.rule_warnings)):
        numeric |= {f"rule_{name}_{kind}": n for name, n in counts.items() if n}
    for name, cost in rep.rule_costs.items():
        numeric |= {f"rule_{name}_{k}": v for k, v in cost.items() if v}
    scores = [_score(rep, n, round(float(v), 6), "NUMERIC") for n, v in numeric.items()]
    scores.append(_score(rep, "landed", int(rep.landed), "BOOLEAN"))
    for name in ("review_tdd_followed", "review_tests_weakened"):
        if (flag := getattr(rep, name)) is not None:
            scores.append(_score(rep, name, int(flag), "BOOLEAN"))
    scores.append(_score(rep, "gate_lane", rep.gate_lane or "unknown", "CATEGORICAL"))
    return scores


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect becomes an HTTPError instead of resending the key elsewhere


def make_http(base_url: str, public_key: str, secret_key: str, urlopen=None):
    """Return `http(method, path, body=None) -> dict` for Langfuse; keys go in a header only.

    The header is never copied to a redirect target and redirects are not followed.
    """
    urlopen = urlopen or urllib.request.build_opener(_NoRedirect).open
    auth = "Basic " + base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()

    def http(method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(base_url + path, data=data, method=method)
        request.add_unredirected_header("Authorization", auth)
        request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=60) as resp:
                text = resp.read().decode()
        except urllib.error.HTTPError as e:
            raise EvalError(f"Langfuse answered HTTP {e.code} on {method} {path}") from None
        except OSError as e:
            raise EvalError(
                f"Langfuse is not reachable at {base_url} ({type(e).__name__});"
                " run scripts/langfuse.sh up"
            ) from None
        return json.loads(text) if text else {}

    return http


def publish_scores(http, scores: list[dict]) -> None:
    """POST every score after a health check, so a stack that is down pushes nothing."""
    http("GET", "/api/public/health")
    for done, score in enumerate(scores):
        try:
            http("POST", "/api/public/scores", score)
        except EvalError as e:
            raise EvalError(
                f"{e}; {done}/{len(scores)} scores written, re-run to finish (idempotent)"
            ) from None


def _fold(
    tables: list[dict], sums: tuple[str, ...], lists: tuple[str, ...] = ()
) -> dict[str, dict]:
    """Merge (step, name) tables of many sessions into name rows with a per-step call count."""
    rows: dict[str, dict] = {}
    for table in tables:
        for (step, name), stats in table.items():
            row = rows.setdefault(
                name, {k: 0 for k in sums} | {k: [] for k in lists} | {"by_step": Counter()}
            )
            for k in sums:
                row[k] += stats[k]
            for k in lists:
                row[k].extend(stats[k])
            row["by_step"][step] += stats[sums[0]] + stats.get("tool_calls", 0)
    return rows


def _session_row(rep: SessionReport) -> dict:
    """One comparison row from an already-computed report; `top_step` is None without cost."""
    acct = rep.accounting
    top = max(acct.steps, key=lambda s: acct.steps[s].cost_usd, default=None)
    return {
        "repo": rep.repo,
        "issue": rep.issue,
        "gate_lane": rep.gate_lane,
        "landed": rep.landed,
        "cost_usd": acct.cost_usd,
        "active_min": acct.active_s / 60,
        "waiting_min": acct.waiting_s / 60,
        "review_rounds": rep.review_rounds,
        "redispatches": rep.redispatches,
        "top_step": top if top and acct.steps[top].cost_usd > 0 else None,
        "raw_only": rep.raw_only,
    }


def aggregate(
    reports: list[SessionReport], *, unused_rules: list[str], rule_sizes: list[RuleSize]
) -> dict:
    """Aggregate sessions (one row each, by start time), steps, agents, tools, hooks and rules."""
    steps = {s: StepTime() for s in STEPS}
    for rep in reports:
        for step, slot in rep.accounting.steps.items():
            for attr in ("active_s", "waiting_s", "other_s", "cost_usd"):
                setattr(steps[step], attr, getattr(steps[step], attr) + getattr(slot, attr))
    tools = _fold([r.attribution.tools for r in reports], ("calls", "failures"), ("durations",))
    for row in tools.values():
        row["p50_s"], row["p90_s"] = (
            percentile(row["durations"], 50),
            percentile(row["durations"], 90),
        )
        del row["durations"]
    return {
        "sessions": [_session_row(r) for r in sorted(reports, key=lambda r: r.started)],
        "steps": steps,
        "agents": _fold(
            [r.attribution.agents for r in reports],
            ("calls", "tool_calls", "cost_usd", "tokens", "llm_s"),
        ),
        "tools": tools,
        "hooks": _fold([r.attribution.hooks for r in reports], ("runs", "total_ms", "blocking")),
        "rules": sum((r.attribution.rules for r in reports), Counter()),
        "unused_rules": unused_rules,
        "rule_sizes": rule_sizes,
    }


def _table(headers: list[str], rows: list[list]) -> str:
    cells = [headers] + [
        [f"{c:.2f}" if isinstance(c, float) else str(c) for c in row] for row in rows
    ]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    return "\n".join(
        "  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in cells
    )


def _by_step(row: dict) -> str:
    return ", ".join(f"{s} {row['by_step'][s]}" for s in STEPS if row["by_step"][s])


def render_session(rep: SessionReport) -> str:
    """Render one session's breakdown as text."""
    acct, issue = rep.accounting, f"#{rep.issue}" if rep.issue else "unknown issue"
    lines = [
        f"== {issue} {rep.title} (session {rep.session}){' (raw only)' * rep.raw_only} ==",
        f"landed={str(rep.landed).lower()} review_rounds={rep.review_rounds}"
        f" redispatches={rep.redispatches} blocked_comments={rep.blocked_comments}"
        f" gate_lane={rep.gate_lane or 'unknown'}",
        f"cost_usd={acct.cost_usd:.2f} active={acct.active_s / 60:.1f}min"
        f" waiting={acct.waiting_s / 60:.1f}min other={acct.other_s / 60:.1f}min"
        f" (of which between dispatches {acct.between_dispatches_s / 60:.1f}min)",
        _table(
            ["step", "cost_usd", "active_s", "waiting_s", "other_s"],
            [[s, t.cost_usd, t.active_s, t.waiting_s, t.other_s] for s, t in acct.steps.items()],
        ),
        "implement active_s by next commit: "
        + ", ".join(f"{k} {v:.0f}" for k, v in acct.implement_parts.items()),
        *[f"note: {n}" for n in rep.notes],
    ]
    return "\n".join(lines)


def render_aggregates(agg: dict) -> str:
    """Render the per-session, per-step, agent, tool, hook and rule tables."""
    sessions = _table(
        [
            "repo",
            "issue",
            "gate_lane",
            "landed",
            "cost_usd",
            "active_min",
            "waiting_min",
            "review_rounds",
            "redispatches",
            "top_step",
            "raw_only",
        ],
        [
            [
                r["repo"],
                f"#{r['issue']}" if r["issue"] else "—",
                r["gate_lane"] or "unknown",
                str(r["landed"]).lower(),
                r["cost_usd"],
                f"{r['active_min']:.1f}",
                f"{r['waiting_min']:.1f}",
                r["review_rounds"],
                r["redispatches"],
                r["top_step"] or "—",
                str(r["raw_only"]).lower(),
            ]
            for r in agg["sessions"]
        ],
    )
    steps = _table(
        ["step", "cost_usd", "active_s", "waiting_s", "other_s"],
        [[s, t.cost_usd, t.active_s, t.waiting_s, t.other_s] for s, t in agg["steps"].items()],
    )
    agents = _table(
        ["agent", "llm_calls", "tool_calls", "cost_usd", "tokens", "llm_s", "by step"],
        [
            [n, r["calls"], r["tool_calls"], r["cost_usd"], r["tokens"], r["llm_s"], _by_step(r)]
            for n, r in sorted(agg["agents"].items(), key=lambda kv: -kv[1]["cost_usd"])
        ],
    )
    tools = _table(
        ["tool", "calls", "failures", "p50_s", "p90_s", "by step"],
        [
            [n, r["calls"], r["failures"], r["p50_s"], r["p90_s"], _by_step(r)]
            for n, r in sorted(agg["tools"].items(), key=lambda kv: -kv[1]["calls"])
        ],
    )
    hooks = _table(
        ["hook", "runs", "total_ms", "avg_ms", "blocking", "by step"],
        [
            [
                n,
                r["runs"],
                r["total_ms"],
                r["total_ms"] / max(r["runs"], 1),
                r["blocking"],
                _by_step(r),
            ]
            for n, r in sorted(agg["hooks"].items(), key=lambda kv: -kv[1]["total_ms"])
        ],
    )
    rules = _table(["on-demand rule", "reads"], [[n, c] for n, c in agg["rules"].most_common()])
    sizes = _table(
        ["always-on rule", "tokens/request"],
        [[r.name + (" (path-scoped)" if r.scoped else ""), r.tokens] for r in agg["rule_sizes"]],
    )
    return "\n\n".join(
        [
            f"== Aggregates over {len(agg['sessions'])} session(s) ==",
            "Sessions\n" + sessions,
            "Steps\n" + steps,
            "Agents\n" + agents,
            "Tools\n" + tools,
            "Hooks\n" + hooks,
            "On-demand rules\n" + rules,
            "Zero-use rules: " + (", ".join(agg["unused_rules"]) or "none"),
            "Always-on rules (cost only - usefulness not measurable here)\n" + sizes,
        ]
    )


def render_report(reports: list[SessionReport], agg: dict, skipped: dict[str, str] | None = None) -> str:
    """Render the full text report: one block per session, the aggregates, the skipped sessions.

    `skipped` maps the sessions that were not scored to their unmapped repo name.
    """
    blocks = [*(render_session(r) for r in reports), render_aggregates(agg)]
    if skipped:
        by_repo = ", ".join(f"{n} ({c})" for n, c in sorted(Counter(skipped.values()).items()))
        blocks.append(f"skipped: {len(skipped)} session(s) of unmapped repo(s): {by_repo}")
    return "\n\n".join(blocks)


def _jsonable(obj):
    if isinstance(obj, Accounting):
        return {
            **_jsonable(
                dict(
                    steps=obj.steps,
                    implement_parts=obj.implement_parts,
                    between_dispatches_s=obj.between_dispatches_s,
                )
            ),
            "cost_usd": obj.cost_usd,
            "active_s": obj.active_s,
            "waiting_s": obj.waiting_s,
            "other_s": obj.other_s,
        }
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _jsonable(getattr(obj, k)) for k in obj.__dataclass_fields__}
    if isinstance(obj, dict):
        return {"/".join(k) if isinstance(k, tuple) else k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonable(v) for v in obj]
    return obj


def render_json(reports: list[SessionReport], agg: dict, skipped: dict[str, str] | None = None) -> str:
    """Render the machine-readable report, with the skipped sessions as {session, repo}."""
    sessions = []
    for rep in reports:
        d = _jsonable(rep)
        d.update({k: d["accounting"][k] for k in ("cost_usd", "active_s", "waiting_s", "other_s")})
        sessions.append(d)
    skipped_rows = [{"session": s, "repo": r} for s, r in (skipped or {}).items()]
    report = {"sessions": sessions, "aggregates": _jsonable(agg), "skipped": skipped_rows}
    return json.dumps(report, indent=2)
