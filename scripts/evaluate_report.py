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
from dataclasses import dataclass, field
from pathlib import Path

from evaluate_time import STEPS, Accounting, EvalError, Leaf, Stepper, StepTime

ON_DEMAND_RULE = re.compile(r"\.ai-toolkit/rules/([\w-]+)\.md$")
# Workers load rules with Bash far more often than with Read: cat/sed/head/grep on the file.
BASH_READER = re.compile(r"\b(?:cat|sed|head|tail|grep|rg|less|bat|awk|nl)\b")
BASH_SEGMENT = re.compile(r"&&|\|\||[;|\n]")
RULE_PATH = re.compile(r"\.ai-toolkit/rules/([\w-]+)\.md")
NOT_A_READ = ("git commit", "gh issue", "gh pr", "orca orchestration")


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
        default_factory=lambda: defaultdict(lambda: dict(calls=0, failures=0, durations=[]))
    )
    hooks: dict[tuple[str, str], dict] = field(
        default_factory=lambda: defaultdict(lambda: dict(runs=0, total_ms=0.0, blocking=0))
    )
    rules: Counter = field(default_factory=Counter)


def _rules_read_by_bash(command: str) -> set[str]:
    """Return the rules a Bash command reads: every rule path in a segment that has a reader."""
    names: set[str] = set()
    for segment in BASH_SEGMENT.split(command):
        if BASH_READER.search(segment) and not any(x in segment for x in NOT_A_READ):
            names.update(RULE_PATH.findall(segment))
    return names


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
            tool = out.tools[(step, leaf.name)]
            tool["calls"] += 1
            tool["failures"] += leaf.failed
            tool["durations"].append(leaf.end - leaf.start - leaf.blocked_s)
            if leaf.name == "Read" and (m := ON_DEMAND_RULE.search(leaf.file_path)):
                out.rules[m[1]] += 1
            elif leaf.name == "Bash":
                out.rules.update(_rules_read_by_bash(leaf.command))
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


def measure_rule_sizes(rules_dir: Path) -> list[RuleSize]:
    """Estimate the tokens each rule file adds (chars / 4); `paths:` frontmatter marks it scoped."""
    sizes = []
    for path in sorted(rules_dir.glob("*.md")):
        text = path.read_text()
        head = text.split("---", 2)[1] if text.startswith("---") and text.count("---") >= 2 else ""
        sizes.append(RuleSize(path.stem, len(text), len(text) // 4, "paths:" in head))
    return sizes


def list_unused_rules(rules_dir: Path, used: dict[str, int]) -> list[str]:
    """Return the on-demand rule files in `rules_dir` that were never read."""
    return sorted(p.stem for p in rules_dir.glob("*.md") if p.stem not in used)


# --------------------------------------------------------------------------- reports


@dataclass(slots=True, kw_only=True)
class SessionReport:
    session: str
    issue: int | None
    title: str = ""
    gate_lane: str | None = None
    labels: list[str] = field(default_factory=list)
    landed: bool = False
    review_rounds: int = 0
    redispatches: int = 0
    blocked_comments: int = 0
    accounting: Accounting
    attribution: Attribution
    notes: list[str] = field(default_factory=list)
    started: float = 0.0


def _score(session: str, name: str, value, data_type: str, issue: int | None) -> dict:
    digest = hashlib.sha1(f"ai-toolkit-eval:{session}:{name}".encode()).hexdigest()
    return {
        "id": digest,
        "sessionId": session,
        "name": name,
        "value": value,
        "dataType": data_type,
        "comment": f"evaluate.py, issue #{issue}",
    }


def build_scores(rep: SessionReport) -> list[dict]:
    """Build a session's Langfuse scores; ids are deterministic, so a re-run upserts."""
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
    scores = [
        _score(rep.session, n, round(float(v), 6), "NUMERIC", rep.issue) for n, v in numeric.items()
    ]
    scores.append(_score(rep.session, "landed", int(rep.landed), "BOOLEAN", rep.issue))
    scores.append(
        _score(rep.session, "gate_lane", rep.gate_lane or "unknown", "CATEGORICAL", rep.issue)
    )
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


def aggregate(
    reports: list[SessionReport], *, unused_rules: list[str], rule_sizes: list[RuleSize]
) -> dict:
    """Aggregate steps, agents, tools, hooks and rules across sessions."""
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
        "sessions": len(reports),
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
        f"== {issue} {rep.title} (session {rep.session}) ==",
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
    """Render the per-step, agent, tool, hook and rule tables."""
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
            f"== Aggregates over {agg['sessions']} session(s) ==",
            "Steps\n" + steps,
            "Agents\n" + agents,
            "Tools\n" + tools,
            "Hooks\n" + hooks,
            "On-demand rules\n" + rules,
            "Zero-use rules: " + (", ".join(agg["unused_rules"]) or "none"),
            "Always-on rules (cost only - usefulness not measurable here)\n" + sizes,
        ]
    )


def render_report(reports: list[SessionReport], agg: dict) -> str:
    """Render the full text report: one block per session, then the aggregates."""
    return "\n\n".join([*(render_session(r) for r in reports), render_aggregates(agg)])


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


def render_json(reports: list[SessionReport], agg: dict) -> str:
    """Render the machine-readable report."""
    sessions = []
    for rep in reports:
        d = _jsonable(rep)
        d.update({k: d["accounting"][k] for k in ("cost_usd", "active_s", "waiting_s", "other_s")})
        sessions.append(d)
    return json.dumps({"sessions": sessions, "aggregates": _jsonable(agg)}, indent=2)
