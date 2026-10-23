"""Leaves, the issue join, the coordinator's lifecycle comments and wall-clock accounting.

Time is accounted on the wall clock, never summed per event: the session is cut into
elementary segments and each one is `waiting` (a human was asked), `active` (a span ran) or
`other` (nothing is known). A segment's step is a function of its time (see `Stepper`).
A session is a Langfuse session, named by its `spoke_run_id`; a re-dispatch is a second
Claude session id inside the same `spoke_run_id`.
"""

from __future__ import annotations

import bisect
import datetime as dt
import json
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from itertools import pairwise

STEPS = ("plan", "implement", "review", "ship")
BASE_REF = "origin/main"
PART_OF_COMMIT = {"test": "red", "feat": "green", "fix": "green", "refactor": "refactor"}
INF = float("inf")


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
    source: str = "langfuse"  # "raw" when only the collector's JSONL had the span


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


def _leaves_from_raw(raw_records: list[dict], seen: set[str]) -> list[Leaf]:
    """Build leaves from raw api_request/tool_result events whose id Langfuse never received."""
    leaves = []
    for rec in raw_records:
        a, ended = rec["attrs"], rec["t"]
        started = ended - float(a.get("duration_ms", 0)) / 1000
        if rec["name"] == "claude_code.api_request" and a.get("request_id") not in seen:
            counts = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
            leaves.append(
                Leaf(
                    "llm",
                    "claude_code.llm_request",
                    started,
                    ended,
                    cost=float(a.get("cost_usd", 0)),
                    tokens=sum(int(a.get(k, 0)) for k in counts),
                    claude=a.get("session.id", ""),
                    source="raw",
                )
            )
        elif rec["name"] == "claude_code.tool_result" and a.get("tool_use_id") not in seen:
            tool_input = _parse_json_object(a.get("tool_input", ""))
            leaves.append(
                Leaf(
                    "tool",
                    a.get("tool_name", ""),
                    started,
                    ended,
                    claude=a.get("session.id", ""),
                    command=str(tool_input.get("command", "")),
                    file_path=str(tool_input.get("file_path", "")),
                    failed=a.get("success") == "false",
                    source="raw",
                )
            )
    return leaves


def _parse_json_object(text: str) -> dict:
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def build_leaves(observations: list[dict], raw_records: list[dict] = ()) -> list[Leaf]:
    """Turn Langfuse observations into leaves, marking failed tools and their blocked time.

    Spans only the raw JSONL has (Langfuse was not receiving yet, or dropped them) are added
    as `source="raw"` leaves; they carry no sub-agent attribution.
    """
    by_id = {o["id"]: o for o in observations}
    failed = {o["parentObservationId"] for o in observations if o["level"] == "ERROR"}
    blocked: Counter[str] = Counter()
    for o in observations:
        if o["name"] == "claude_code.tool.blocked_on_user":
            blocked[o.get("parentObservationId")] += o["latency"] or 0.0
    leaves = []
    for o in observations:
        if (kind := _leaf_kind(o)) is None:
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
                end=parse_time(o.get("endTime") or o["startTime"]),
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
    seen = {_attr(o, "request_id") or _attr(o, "tool_use_id") for o in observations} - {None}
    return leaves + _leaves_from_raw(list(raw_records), seen)


def count_claude_sessions(observations: list[dict], raw_records: list[dict]) -> int:
    """Count the distinct Claude session ids seen in Langfuse and the raw JSONL."""
    sessions = {_attr(o, "session.id") for o in observations}
    sessions |= {rec["attrs"].get("session.id") for rec in raw_records}
    return len(sessions - {None, ""})


def count_dispatches(
    observations: list[dict], raw_records: list[dict], life: "Lifecycle"
) -> dict[str, int]:
    """Count in-spoke review rounds and re-dispatches from every source that has them.

    Langfuse and the raw JSONL describe the same events, so review rounds take the larger of
    the two (a span either side is missing never lowers it): `code-review` agent spans or Agent
    tool results. Re-dispatches are extra Claude session ids, or the coordinator's `blocked:`
    comments when those are more.
    """
    reviews = sum(1 for o in observations if o["name"] == "sub-agent:code-review")
    raw_reviews = sum(
        1
        for rec in raw_records
        if rec["name"] == "claude_code.tool_result"
        and rec["attrs"].get("tool_name") == "Agent"
        and _parse_json_object(rec["attrs"].get("tool_parameters", "")).get("subagent_type")
        == "code-review"
    )
    sessions = count_claude_sessions(observations, raw_records)
    return {
        "review_rounds": max(reviews, raw_reviews),
        "redispatches": max(sessions - 1, life.blocked_count, 0),
    }


# --------------------------------------------------------------------------- join


def find_issue(observations, raw_records, repo, forced=None) -> int | None:
    """Return the issue number of a session, or None when it cannot be told.

    Order: the explicit override, then `vcs.ref.head.name` in the raw JSONL, then the
    `<n>-<slug>` worktree directory in tool commands and file paths (Langfuse or raw). The
    `repo` resource attribute must match, so a session of another project is never joined.
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
        texts = [_attr(o, "full_command", "") + _attr(o, "file_path", "") for o in observations]
        texts += [str(rec["attrs"].get("tool_input", "")) for rec in raw_records]
        for text in texts:
            votes.update(int(m[1]) for m in path_re.finditer(text))
    return votes.most_common(1)[0][0] if votes else None


# --------------------------------------------------------------------------- lifecycle


@dataclass(slots=True, frozen=True)
class Wait:
    start: float
    end: float
    kind: str  # permission | gate
    answered: bool


@dataclass(slots=True, frozen=True)
class Review:
    """A review outcome: when it was posted, its flags and the rule names it blamed.

    A `review-verdict` comment gives every field; an old coordinator rejection gives the flags
    only, with no rule names.
    """

    at: float
    tdd_failed: bool
    tests_weakened: bool
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(slots=True)
class Lifecycle:
    gate_answered_at: float | None = None
    waits: list[Wait] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)
    verdicts: list[float] = field(default_factory=list)  # blocked:, attended round, landed
    blocked_count: int = 0
    landed_sha: str | None = None


REJECTION = re.compile(r"round \d+: address: the independent review requested changes")
TDD_BLOCKER = re.compile(r"\(\d+\) tdd_followed is false:")
WEAKENED_BLOCKER = re.compile(r"\(\d+\) tests_weakened:")


def _parse_verdict(body: str, at: float) -> Review | None:
    """Return the review that a `review-verdict: {...}` comment records, or None if malformed."""
    try:
        v = json.loads(body.removeprefix("review-verdict: "))
        if v["v"] != 1:
            return None
        blockers, warnings = v["blockers"], v["warnings"]
        if not (isinstance(blockers, list) and isinstance(warnings, list)):
            return None
        if not all(isinstance(n, str) for n in (*blockers, *warnings)):
            return None
        return Review(
            at, not v["tdd_followed"], bool(v["tests_weakened"]), tuple(blockers), tuple(warnings)
        )
    except (ValueError, TypeError, KeyError):
        return None


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
    after_verdict = False  # the last review comment was a review-verdict

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
            matches = [i for i, (_, t) in enumerate(open_permissions) if tool in ("", t)]
            if matches:
                life.waits.append(
                    Wait(open_permissions.pop(matches[-1])[0], at, "permission", True)
                )
        elif body.startswith("A gate question needs a human"):
            gate_open = at if gate_open is None else gate_open  # a re-ask keeps the first time
        elif body.startswith("review-verdict: "):
            if review := _parse_verdict(body, at):
                life.reviews.append(review)
                after_verdict = True
        elif REJECTION.match(body):
            # the coordinator's copy of a verdict is that same review: counted once
            if not after_verdict:
                # review.sh's numbered blocker lines, never free text
                life.reviews.append(
                    Review(at, bool(TDD_BLOCKER.search(body)), bool(WEAKENED_BLOCKER.search(body)))
                )
            after_verdict = False
        elif body.startswith("round "):
            life.verdicts.append(at)  # an attended round: decided by the user, no flags
        elif body.startswith("blocked:"):
            life.blocked_count += 1
            life.verdicts.append(at)
            close_gate(at, False)
        elif m := re.match(r"landed in ([0-9a-f]{7,40})", body):
            life.landed_sha = m[1]
            life.verdicts.append(at)
            close_gate(at, False)
    if gate_open is not None:
        life.waits.append(Wait(gate_open, INF, "gate", False))
    return life


def judged_reviews(life: Lifecycle, start: float, end: float) -> list[Review]:
    """Return the reviews that judged a session spanning `start` to `end`.

    A re-dispatch continues the same session, so every review posted during its spans
    counts, plus the first one after them (the review of its last push) unless another
    verdict (blocked, attended round, landed) comes first: that one judged the session.
    """
    judged = [r for r in life.reviews if start < r.at <= end]
    last = next((r for r in life.reviews if r.at > end), None)
    closed_at = next((t for t in life.verdicts if t > end), INF)
    return judged + [last] * (last is not None and last.at < closed_at)


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
