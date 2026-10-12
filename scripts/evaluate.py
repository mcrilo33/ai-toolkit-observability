#!/usr/bin/env python3
"""Join worker sessions to issues, segment the cycle, and score cost and time per step.

One run reads three sources, computes everything in memory, and only then writes scores:

* Langfuse (v4, events-only) observations: cost, tokens, tool/sub-agent spans, latency.
* The collector's raw JSONL: hooks, and the branch name when a tool_result carries it.
* `gh` and `git`: the issue footer, the coordinator's lifecycle comments, the commits.

A session is a Langfuse session, named by its `spoke_run_id`. A re-dispatch is a second
Claude session id inside the same `spoke_run_id`.

Time is accounted on the wall clock, never summed per event: the session is cut into
elementary segments and each one is `waiting` (a human was asked), `active` (a span ran) or
`other` (nothing is known). A segment's step is a function of its time (see `Stepper`).
"""

from __future__ import annotations

import argparse
import base64
import bisect
import datetime as dt
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

STEPS = ("plan", "implement", "review", "ship")
BASE_REF = "origin/main"
PART_OF_COMMIT = {"test": "red", "feat": "green", "fix": "green", "refactor": "refactor"}
INF = float("inf")
LANGFUSE_URL = "http://127.0.0.1:3000"
KEYCHAIN_SERVICE = "ai-toolkit-langfuse-LANGFUSE_INIT_PROJECT_{}_KEY"
RAW_EVENTS = {"claude_code.hook_execution_complete", "claude_code.tool_result"}


class EvalError(Exception):
    """A failure the operator can act on (stack down, missing tool, bad input)."""


def parse_time(value: str) -> float:
    """Return epoch seconds for an ISO-8601 timestamp, `Z` suffix included."""
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


# --------------------------------------------------------------------------- leaves


@dataclass(slots=True, frozen=True)
class Leaf:
    """One span that did work: an LLM request, a tool call, or a wait for the user."""

    kind: str  # llm | tool | blocked
    name: str
    start: float
    end: float
    cost: float = 0.0
    tokens: int = 0
    claude: str = ""
    agent: str | None = None
    command: str = ""
    file_path: str = ""
    failed: bool = False
    blocked_s: float = 0.0


def _attr(obs: dict, key: str, default=None):
    return obs["metadata"].get(f"attributes.{key}", default)


def _find_agent(obs: dict, by_id: dict[str, dict]) -> str | None:
    """Name the sub-agent type that owns `obs`, found by walking up its parents."""
    node = obs
    for _ in range(32):
        name = node["name"]
        if name.startswith("sub-agent:") and name != "sub-agent:llm" and node is not obs:
            return name.removeprefix("sub-agent:")
        node = by_id.get(node.get("parentObservationId") or "")
        if node is None:
            return None
    return None


def _leaf_kind(obs: dict) -> str | None:
    name = obs["name"]
    if obs["type"] == "GENERATION":
        return "llm"
    if name == "claude_code.tool.blocked_on_user":
        return "blocked"
    if name.startswith(("tool:", "sub-agent:")):
        return "tool"
    return None


def build_leaves(observations: list[dict]) -> list[Leaf]:
    """Turn Langfuse observations into leaves, marking failed tools and their blocked time."""
    by_id = {o["id"]: o for o in observations}
    failed = {o["parentObservationId"] for o in observations if o["level"] == "ERROR"}
    blocked: Counter[str] = Counter()
    for o in observations:
        if o["name"] == "claude_code.tool.blocked_on_user":
            blocked[o.get("parentObservationId")] += o["latency"] or 0.0
    leaves = []
    for o in observations:
        if (kind := _leaf_kind(o)) is None or not o.get("endTime"):
            continue
        tool = (
            o["name"].removeprefix("tool:")
            if o["name"].startswith("tool:")
            else _attr(o, "tool_name", "Agent")
        )
        leaves.append(
            Leaf(
                kind=kind,
                name=o["name"] if kind != "tool" else tool,
                start=parse_time(o["startTime"]),
                end=parse_time(o["endTime"]),
                cost=o.get("totalCost") or 0.0,
                tokens=(o.get("usageDetails") or {}).get("total", 0),
                claude=_attr(o, "session.id", ""),
                agent=_find_agent(o, by_id),
                command=_attr(o, "full_command", ""),
                file_path=_attr(o, "file_path", ""),
                failed=o["id"] in failed,
                blocked_s=blocked[o["id"]],
            )
        )
    return leaves


def count_dispatches(observations: list[dict]) -> dict[str, int]:
    """Count in-spoke review rounds and re-dispatches (extra Claude sessions) from telemetry."""
    sessions = {_attr(o, "session.id") for o in observations} - {None}
    reviews = sum(1 for o in observations if o["name"] == "sub-agent:code-review")
    return {"review_rounds": reviews, "redispatches": max(len(sessions) - 1, 0)}


# --------------------------------------------------------------------------- join


def find_issue(observations, raw_records, repo, forced=None) -> int | None:
    """Return the issue number of a session, or None when it cannot be told.

    Order: the explicit override, then `vcs.ref.head.name` in the raw JSONL, then the
    `<n>-<slug>` worktree directory in tool commands and file paths. The `repo` resource
    attribute must match, so a session of another project is never joined.
    """
    if forced is not None:
        return forced
    repos = {o["metadata"].get("repo") for o in observations} - {None}
    if repos and repo not in repos:
        return None
    votes = Counter()
    for rec in raw_records:
        if m := re.match(r"(\d+)-", rec["attrs"].get("vcs.ref.head.name", "")):
            votes[int(m[1])] += 1
    if not votes:
        path_re = re.compile(rf"/workspaces/{re.escape(repo)}/(\d+)-[a-z]")
        for o in observations:
            for text in (_attr(o, "full_command", ""), _attr(o, "file_path", "")):
                votes.update(int(m[1]) for m in path_re.finditer(text))
    return votes.most_common(1)[0][0] if votes else None


# --------------------------------------------------------------------------- lifecycle


@dataclass(slots=True, frozen=True)
class Wait:
    start: float
    end: float
    kind: str  # permission | gate
    answered: bool


@dataclass(slots=True)
class Lifecycle:
    gate_answered_at: float | None = None
    waits: list[Wait] = field(default_factory=list)
    blocked_count: int = 0
    landed_sha: str | None = None


def _tool_of(body: str) -> str:
    m = re.search(r"tool: (\w+)", body)
    return m[1] if m else ""


def parse_lifecycle(comments: list[dict]) -> Lifecycle:
    """Read the coordinator loop's own comments into waits, the gate answer and the landing.

    A permission wait runs from its request comment to the "Permission answered" comment for
    the same tool (the latest open request wins); a request nobody answered is not a wait here,
    because the
    telemetry's blocked-on-user spans already cover the ones that really blocked. A gate
    question runs until the gate is answered, the worker reports blocked, or it lands.
    """
    life = Lifecycle()
    open_permissions: list[tuple[float, str]] = []
    gate_open: float | None = None

    def close_gate(at: float, answered: bool) -> None:
        nonlocal gate_open
        if gate_open is not None:
            life.waits.append(Wait(gate_open, at, "gate", answered))
            gate_open = None

    for c in sorted(comments, key=lambda c: c["createdAt"]):
        at, body = parse_time(c["createdAt"]), c["body"]
        if body.startswith("Gate answered"):
            life.gate_answered_at = life.gate_answered_at or at
            close_gate(at, True)
        elif body.startswith("A permission request needs a human"):
            open_permissions.append((at, _tool_of(body)))
        elif body.startswith("Permission answered") and open_permissions:
            tool = _tool_of(body)
            i = next(
                (
                    i
                    for i in reversed(range(len(open_permissions)))
                    if open_permissions[i][1] == tool
                ),
                -1,
            )
            life.waits.append(Wait(open_permissions.pop(i)[0], at, "permission", True))
        elif body.startswith("A gate question needs a human"):
            gate_open = at
        elif body.startswith("blocked:"):
            life.blocked_count += 1
            close_gate(at, False)
        elif m := re.match(r"landed in ([0-9a-f]{7,40})", body):
            life.landed_sha = m[1]
            close_gate(at, False)
    if gate_open is not None:
        life.waits.append(Wait(gate_open, INF, "gate", False))
    return life


def is_landed(life: Lifecycle, run) -> bool:
    """Return True when the landing comment exists and git does not contradict it.

    Git only contradicts it when the SHA is known and not an ancestor of the base branch; an
    unknown SHA (not fetched) leaves the comment as the evidence.
    """
    if not life.landed_sha:
        return False
    try:
        run(["git", "merge-base", "--is-ancestor", life.landed_sha, BASE_REF])
    except subprocess.CalledProcessError as e:
        return e.returncode != 1
    return True


@dataclass(slots=True, frozen=True)
class Commit:
    ts: float
    kind: str
    issue: int


def load_commits(run, issue: int) -> list[Commit]:
    """Return the conventional commits that carry `#<issue>`, oldest first."""
    out = run(["git", "log", "--all", "--format=%ct%x09%s", f"--grep=#{issue}"])
    commits = []
    for line in out.splitlines():
        ts, _, subject = line.partition("\t")
        m = re.match(r"(\w+)(?:\([^)]*\))?!?:", subject)
        if m and re.search(rf"#{issue}\b", subject):
            commits.append(Commit(float(ts), m[1], issue))
    return sorted(commits, key=lambda c: c.ts)


# --------------------------------------------------------------------------- time


def _merge(intervals) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for a, b in sorted(i for i in intervals if i[1] > i[0]):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def _inside(merged: list[tuple[float, float]], t: float) -> bool:
    i = bisect.bisect_right(merged, (t, INF)) - 1
    return i >= 0 and merged[i][0] <= t < merged[i][1]


class Stepper:
    """Answer "which step is time t in?" from the gate answer, review spans and pushes.

    Review wins (the in-spoke code-review agent's own spans), then plan (before the gate
    answer), then ship (each push command, plus the last push before `worker_done` through
    `worker_done`), else implement. Implement time is labelled by the kind of the next
    commit: `test` red, `feat`/`fix` green, `refactor` refactor, anything else other.
    """

    def __init__(self, leaves: list[Leaf], life: Lifecycle, commits: list[Commit]):
        self.gate = life.gate_answered_at
        self.commits = sorted(commits, key=lambda c: c.ts)
        self._times = [c.ts for c in self.commits]
        self.review = _merge((lf.start, lf.end) for lf in leaves if lf.agent == "code-review")
        self.ship = _merge(self._ship_intervals(leaves))

    @staticmethod
    def _ship_intervals(leaves: list[Leaf]):
        tools = [lf for lf in leaves if lf.kind == "tool"]
        yield from ((lf.start, lf.end) for lf in tools if "git push" in lf.command)
        for claude in {lf.claude for lf in tools}:
            mine = [lf for lf in tools if lf.claude == claude]
            done = [lf for lf in mine if "worker_done" in lf.command]
            pushes = [lf for lf in mine if "git push" in lf.command]
            if not done:
                continue
            last_done = max(done, key=lambda lf: lf.end)
            before = [p for p in pushes if p.start < last_done.start]
            if before:
                yield max(p.start for p in before), last_done.end

    def boundaries(self) -> list[float]:
        points = [self.gate, *self._times] if self.gate else list(self._times)
        return points + [p for iv in self.review + self.ship for p in iv]

    def at(self, t: float) -> tuple[str, str]:
        """Return (step, implement part) for time t; the part is "" outside implement."""
        if _inside(self.review, t):
            return "review", ""
        if self.gate and t < self.gate:
            return "plan", ""
        if _inside(self.ship, t):
            return "ship", ""
        i = bisect.bisect_right(self._times, t)
        kind = self.commits[i].kind if i < len(self.commits) else ""
        return "implement", PART_OF_COMMIT.get(kind, "other")


@dataclass(slots=True)
class StepTime:
    active_s: float = 0.0
    waiting_s: float = 0.0
    other_s: float = 0.0
    cost_usd: float = 0.0


@dataclass(slots=True)
class Accounting:
    steps: dict[str, StepTime]
    implement_parts: dict[str, float]
    between_dispatches_s: float = 0.0

    def _sum(self, attr: str) -> float:
        return sum(getattr(s, attr) for s in self.steps.values())

    active_s = property(lambda self: self._sum("active_s"))
    waiting_s = property(lambda self: self._sum("waiting_s"))
    other_s = property(lambda self: self._sum("other_s"))
    cost_usd = property(lambda self: self._sum("cost_usd"))


def _dispatch_gaps(leaves: list[Leaf]) -> list[tuple[float, float]]:
    ranges = sorted(
        (min(lf.start for lf in g), max(lf.end for lf in g))
        for claude in {lf.claude for lf in leaves}
        if (g := [lf for lf in leaves if lf.claude == claude])
    )
    return [(a_end, b_start) for (_, a_end), (b_start, _) in pairwise(ranges) if b_start > a_end]


def account_time(leaves: list[Leaf], life: Lifecycle, commits: list[Commit]) -> Accounting:
    """Split the session's wall clock into waiting, active and other, per step.

    Waiting is the union of the blocked-on-user spans and the comment waits; it wins over
    active, so a tool span that sat in a human's queue (`orca ... ask`) is not work.
    """
    acct = Accounting(
        {s: StepTime() for s in STEPS}, dict.fromkeys(("red", "green", "refactor", "other"), 0.0)
    )
    if not leaves:
        return acct
    stepper = Stepper(leaves, life, commits)
    t0, t1 = min(lf.start for lf in leaves), max(lf.end for lf in leaves)
    waits = _merge(
        [(lf.start, lf.end) for lf in leaves if lf.kind == "blocked"]
        + [(w.start, w.end) for w in life.waits]
    )
    active = _merge((lf.start, lf.end) for lf in leaves if lf.kind in ("llm", "tool"))
    gaps = _merge(_dispatch_gaps(leaves))
    cuts = {t0, t1} | {
        p for p in stepper.boundaries() + [p for iv in waits + active for p in iv] if t0 < p < t1
    }
    for a, b in pairwise(sorted(cuts)):
        mid, length = (a + b) / 2, b - a
        step, part = stepper.at(mid)
        slot = acct.steps[step]
        if _inside(waits, mid):
            slot.waiting_s += length
        elif _inside(active, mid):
            slot.active_s += length
            if part:
                acct.implement_parts[part] += length
        else:
            slot.other_s += length
            acct.between_dispatches_s += length if _inside(gaps, mid) else 0.0
    for leaf in leaves:
        acct.steps[stepper.at(leaf.start)[0]].cost_usd += leaf.cost
    return acct


# --------------------------------------------------------------------------- attribution

ON_DEMAND_RULE = re.compile(r"\.ai-toolkit/rules/([\w-]+)\.md$")


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


def make_http(base_url: str, public_key: str, secret_key: str, urlopen=urllib.request.urlopen):
    """Return `http(method, path, body=None) -> dict` for Langfuse; keys go in a header only."""
    auth = "Basic " + base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()

    def http(method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(base_url + path, data=data, method=method)
        request.add_header("Authorization", auth)
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
            row["by_step"][step] += stats[sums[0]] + (
                stats["tool_calls"] if "tool_calls" in stats else 0
            )
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
        f" redispatches={rep.redispatches} blocked_comments={rep.blocked_comments} gate_lane={rep.gate_lane or 'unknown'}",
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


# --------------------------------------------------------------------------- loading


def run_command(args: list[str]) -> str:
    """Run a command and return its stdout; CalledProcessError carries a non-zero exit."""
    try:
        return subprocess.run(args, capture_output=True, text=True, check=True, timeout=120).stdout
    except FileNotFoundError:
        raise EvalError(f"`{args[0]}` is not installed or not on PATH") from None


def load_credentials(run) -> tuple[str, str]:
    """Read the Langfuse project key pair from the macOS Keychain (never from argv or env)."""
    keys = []
    for part in ("PUBLIC", "SECRET"):
        service = KEYCHAIN_SERVICE.format(part)
        try:
            keys.append(
                run(
                    [
                        "security",
                        "find-generic-password",
                        "-a",
                        os.environ.get("USER", ""),
                        "-s",
                        service,
                        "-w",
                    ]
                ).strip()
            )
        except subprocess.CalledProcessError:
            raise EvalError(
                f"Keychain item {service} is missing; run scripts/langfuse.sh up"
            ) from None
    return keys[0], keys[1]


def fetch_observations(http, *, since: str | None = None, until: str | None = None) -> list[dict]:
    """Page through Langfuse v4's /api/public/v2/observations (the legacy endpoints are off)."""
    query = "limit=1000&fields=core,basic,usage,metadata"
    query += f"&fromStartTime={since}" if since else ""
    query += f"&toStartTime={until}" if until else ""
    out, cursor = [], None
    while True:
        suffix = f"&cursor={urllib.parse.quote(cursor, safe='')}" if cursor else ""
        page = http("GET", f"/api/public/v2/observations?{query}{suffix}")
        out += page.get("data", [])
        if not (cursor := page.get("meta", {}).get("cursor")):
            return out


def load_raw(raw_dir: Path, wanted: set[str]) -> dict[str, list[dict]]:
    """Read hook and branch records of `wanted` sessions from every collector JSONL file."""
    out: dict[str, list[dict]] = defaultdict(list)
    for path in sorted(raw_dir.glob("logs*.jsonl")):
        for line in path.read_text(errors="replace").splitlines():
            if not any(w in line for w in wanted):
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            for resource_logs in entry.get("resourceLogs", []):
                res = {
                    a["key"]: next(iter(a["value"].values()))
                    for a in resource_logs["resource"]["attributes"]
                }
                for scope in resource_logs["scopeLogs"]:
                    for rec in scope["logRecords"]:
                        name = rec["body"].get("stringValue")
                        attrs = {
                            a["key"]: next(iter(a["value"].values()), "") for a in rec["attributes"]
                        }
                        if name in RAW_EVENTS and res.get("spoke_run_id") in wanted:
                            out[res["spoke_run_id"]].append(
                                {"t": int(rec["timeUnixNano"]) / 1e9, "name": name, "attrs": attrs}
                            )
    return out


def load_issue(run, number: int) -> dict:
    """Fetch an issue with its comments through `gh`."""
    fields = "number,title,state,body,labels,comments"
    try:
        return json.loads(run(["gh", "issue", "view", str(number), "--json", fields]))
    except subprocess.CalledProcessError as e:
        raise EvalError(
            f"gh could not read issue #{number}: {(e.stderr or '').strip()[:200]}"
        ) from None


# --------------------------------------------------------------------------- pipeline


def analyse_session(session, observations, raw, issue, commits, run) -> SessionReport:
    """Build the report of one session from its observations, raw records, issue and commits."""
    leaves = build_leaves(observations)
    life = parse_lifecycle(issue["comments"]) if issue else Lifecycle()
    stepper = Stepper(leaves, life, commits)
    counts = count_dispatches(observations)
    footer = re.search(r"(?m)^Gate:\s*(\S+)", issue["body"]) if issue else None
    notes = []
    if not raw:
        notes.append(
            "raw JSONL missing for this session (rotated away?): hooks and the branch "
            "name are unavailable; cost, tools, agents and rules come from Langfuse"
        )
    if issue and not life.gate_answered_at:
        notes.append("no gate-answer comment: the plan step is empty")
    if issue and counts["redispatches"] != life.blocked_count:
        notes.append(
            f"{counts['redispatches']} re-dispatch(es) in telemetry vs "
            f"{life.blocked_count} blocked comment(s)"
        )
    if not issue:
        notes.append("issue unknown: pass --issue N --session ID; no scores are published")
    return SessionReport(
        session=session,
        issue=issue["number"] if issue else None,
        title=issue["title"] if issue else "",
        gate_lane=footer[1] if footer else None,
        labels=[l["name"] for l in issue["labels"]] if issue else [],
        landed=is_landed(life, run),
        blocked_comments=life.blocked_count,
        **counts,
        accounting=account_time(leaves, life, commits),
        attribution=attribute(leaves, raw, stepper),
        notes=notes,
        started=min((lf.start for lf in leaves), default=0.0),
    )


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


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Score toolkit worker sessions by issue and cycle step."
    )
    ap.add_argument("--issue", type=int, action="append", help="only this issue (repeatable)")
    ap.add_argument(
        "--session",
        action="append",
        help="only this spoke_run_id (repeatable); with one --issue it also forces the join",
    )
    ap.add_argument("--since", help="ISO date or time: sessions starting after it")
    ap.add_argument("--until", help="ISO date or time: sessions starting before it")
    ap.add_argument("--repo", help="repository name (default: the current gh repository)")
    ap.add_argument("--url", default=LANGFUSE_URL, help="Langfuse base URL")
    ap.add_argument("--raw-dir", type=Path, default=Path.home() / ".ai-toolkit" / "otel-raw")
    ap.add_argument("--rules-dir", type=Path, default=Path(".ai-toolkit/rules"))
    ap.add_argument("--always-on-dir", type=Path, default=Path(".claude/rules/ai-toolkit"))
    ap.add_argument("--no-publish", action="store_true", help="print the report, push no scores")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    return ap.parse_args(argv)


def _select_sessions(args, observations, raw, repo) -> dict[str, int | None]:
    """Map each session of this repository to its issue number (None when unknown)."""
    by_session = defaultdict(list)
    for o in observations:
        if o.get("sessionId") and (not args.session or o["sessionId"] in args.session):
            by_session[o["sessionId"]].append(o)
    forced = args.issue[0] if args.session and args.issue and len(args.issue) == 1 else None
    joined = {
        s: find_issue(obs, raw.get(s, []), repo, forced)
        for s, obs in by_session.items()
        if repo in {o["metadata"].get("repo") for o in obs}
    }
    return {s: n for s, n in joined.items() if not args.issue or n in args.issue}


def _iso(value: str | None) -> str | None:
    return value if value is None or "T" in value else f"{value}T00:00:00Z"


def main(argv: list[str] | None = None, *, http=None, run=None) -> int:
    """Run the evaluation; returns 0 on success and 2 on an operator-actionable error."""
    args, run = parse_args(argv), run or run_command
    try:
        repo = args.repo or run(["gh", "repo", "view", "--json", "name", "-q", ".name"]).strip()
        http = http or make_http(args.url, *load_credentials(run))
        observations = fetch_observations(http, since=_iso(args.since), until=_iso(args.until))
        raw = load_raw(args.raw_dir, {o["sessionId"] for o in observations if o.get("sessionId")})
        selected = _select_sessions(args, observations, raw, repo)
        issues = {n: load_issue(run, n) for n in set(selected.values()) - {None}}
        commits = {n: load_commits(run, n) for n in issues}
        reports = sorted(
            (
                analyse_session(
                    s,
                    [o for o in observations if o.get("sessionId") == s],
                    raw.get(s, []),
                    issues.get(n),
                    commits.get(n, []),
                    run,
                )
                for s, n in selected.items()
            ),
            key=lambda r: r.started,
        )
        agg = aggregate(
            reports,
            unused_rules=list_unused_rules(
                args.rules_dir, sum((r.attribution.rules for r in reports), Counter())
            ),
            rule_sizes=measure_rule_sizes(args.always_on_dir),
        )
        print(render_json(reports, agg) if args.json else render_report(reports, agg))
        if not args.no_publish:
            publish_scores(http, [s for r in reports if r.issue for s in build_scores(r)])
    except EvalError as e:
        print(f"evaluate.py: error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
