#!/usr/bin/env python3
"""Join worker sessions to issues, segment the cycle, and score cost and time per step.

One run reads three sources, computes everything in memory, and only then writes scores:

* Langfuse (v4, events-only) observations: cost, tokens, tool/sub-agent spans, latency.
* The collector's raw JSONL: hooks, the branch name, and the spans Langfuse never received.
* `gh` and `git`: the issue footer, the coordinator's lifecycle comments, the commits.

The pure logic lives in `evaluate_time` (join, lifecycle, wall-clock accounting) and
`evaluate_report` (attribution, scores, Langfuse client, rendering); this file loads the
sources and drives the pipeline. The names the tests use are re-exported here.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

from evaluate_report import (
    Attribution,
    RuleSize,
    SessionReport,
    aggregate,
    attribute,
    build_scores,
    list_unused_rules,
    make_http,
    measure_rule_sizes,
    percentile,
    publish_scores,
    render_json,
    render_report,
)
from evaluate_time import (
    STEPS,
    Accounting,
    Commit,
    EvalError,
    Lifecycle,
    Stepper,
    StepTime,
    account_time,
    build_leaves,
    count_claude_sessions,
    count_dispatches,
    find_issue,
    is_landed,
    load_commits,
    parse_lifecycle,
    parse_time,
)

__all__ = [
    "STEPS",
    "Accounting",
    "Attribution",
    "Commit",
    "EvalError",
    "Lifecycle",
    "RuleSize",
    "SessionReport",
    "StepTime",
    "Stepper",
    "account_time",
    "aggregate",
    "attribute",
    "build_leaves",
    "build_scores",
    "count_claude_sessions",
    "count_dispatches",
    "fetch_observations",
    "find_issue",
    "is_landed",
    "list_unused_rules",
    "load_raw",
    "main",
    "make_http",
    "measure_rule_sizes",
    "parse_lifecycle",
    "percentile",
    "publish_scores",
    "render_report",
]

LANGFUSE_URL = "http://127.0.0.1:3000"
RAW_SLACK_S = 120  # tolerated gap between the raw files and the session edges
KEYCHAIN_SERVICE = "ai-toolkit-langfuse-LANGFUSE_INIT_PROJECT_{}_KEY"
RAW_EVENTS = {
    "claude_code.hook_execution_complete",
    "claude_code.tool_result",
    "claude_code.api_request",
}

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


def fetch_observations(http) -> list[dict]:
    """Page through all of Langfuse v4's /api/public/v2/observations (legacy endpoints are off).

    No time bounds: a window must pick whole sessions, never clip a session's spans.
    """
    query = "limit=1000&fields=core,basic,usage,metadata"
    out, cursor = [], None
    while True:
        suffix = f"&cursor={urllib.parse.quote(cursor, safe='')}" if cursor else ""
        page = http("GET", f"/api/public/v2/observations?{query}{suffix}")
        out += page.get("data", [])
        if not (cursor := page.get("meta", {}).get("cursor")):
            return out


SPOKE_ID = re.compile(r'"spoke_run_id"\s*,\s*"value"\s*:\s*\{\s*"stringValue"\s*:\s*"([^"]+)"')


def load_raw(raw_dir: Path, wanted: set[str]) -> dict[str, list[dict]]:
    """Read hook, tool and request records of `wanted` sessions from every collector file.

    A line may batch several resources (sessions); each resource names its own session.
    """
    out: dict[str, list[dict]] = defaultdict(list)
    for path in sorted(raw_dir.glob("logs*.jsonl")):
        with path.open(errors="replace") as lines:
            for line in lines:
                if wanted.isdisjoint(SPOKE_ID.findall(line)):
                    continue
                for session, record in _parse_raw_line(line):
                    if session in wanted:
                        out[session].append(record)
    return out


def _parse_raw_line(line: str) -> list[tuple[str, dict]]:
    try:
        entry = json.loads(line)
    except ValueError:
        return []
    records = []
    for resource_logs in entry.get("resourceLogs", []):
        resource = resource_logs.get("resource", {}).get("attributes", [])
        session = next(
            (a["value"].get("stringValue") for a in resource if a["key"] == "spoke_run_id"), None
        )
        for scope in resource_logs.get("scopeLogs", []):
            for rec in scope.get("logRecords", []):
                name = rec.get("body", {}).get("stringValue")
                if session is None or name not in RAW_EVENTS:
                    continue
                attrs = {
                    a["key"]: next(iter(a["value"].values()), "") for a in rec.get("attributes", [])
                }
                try:
                    t = int(rec["timeUnixNano"]) / 1e9
                except (KeyError, ValueError):
                    continue
                records.append((session, {"t": t, "name": name, "attrs": attrs}))
    return records


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


def _describe_gap_fill(filled: list) -> str:
    clock = lambda t: dt.datetime.fromtimestamp(t, dt.UTC).strftime("%H:%M:%S")  # noqa: E731
    llm = [lf for lf in filled if lf.kind == "llm"]
    return (
        f"gap-filled from the raw JSONL (Langfuse had no span for them): {len(llm)} LLM "
        f"request(s) costing {sum(lf.cost for lf in llm):.2f} USD and {len(filled) - len(llm)} "
        f"tool call(s), {clock(min(lf.start for lf in filled))} to "
        f"{clock(max(lf.end for lf in filled))} UTC; they have no sub-agent attribution"
    )


def _describe_partial_raw(raw: list[dict], leaves: list) -> str | None:
    """Say when the raw JSONL covers only part of the session (the files rotate by size)."""
    own = [lf for lf in leaves if lf.source == "langfuse"] or leaves
    if not raw or not own:
        return None
    clock = lambda t: dt.datetime.fromtimestamp(t, dt.UTC).strftime("%H:%M:%S")  # noqa: E731
    lo, hi = min(r["t"] for r in raw), max(r["t"] for r in raw)
    first, last = min(lf.start for lf in own), max(lf.end for lf in own)
    if lo <= first + RAW_SLACK_S and hi >= last - RAW_SLACK_S:
        return None
    return (
        f"raw JSONL covers only {clock(lo)}-{clock(hi)} UTC of the session's {clock(first)}-"
        f"{clock(last)}: hooks and gap-filled spans are incomplete outside it"
    )


def analyse_session(session, observations, raw, issue, commits, run) -> SessionReport:
    """Build the report of one session from its observations, raw records, issue and commits."""
    leaves = build_leaves(observations, raw)
    life = parse_lifecycle(issue["comments"]) if issue else Lifecycle()
    stepper = Stepper(leaves, life, commits)
    counts = count_dispatches(observations, raw, life)
    footer = re.search(r"(?m)^Gate:\s*(\S+)", issue["body"]) if issue else None
    notes = []
    if not raw:
        notes.append(
            "raw JSONL missing for this session (rotated away?): hooks and the branch "
            "name are unavailable; cost, tools, agents and rules come from Langfuse"
        )
    filled = [lf for lf in leaves if lf.source == "raw"]
    if filled:
        notes.append(_describe_gap_fill(filled))
    if partial := _describe_partial_raw(raw, leaves):
        notes.append(partial)
    if not raw or filled or partial:
        notes.append(
            "review_rounds and redispatches are lower bounds: they count only the spans and "
            "sessions that Langfuse or the raw JSONL still have"
        )
    if issue and not life.gate_answered_at:
        notes.append("no gate-answer comment: the plan step is empty")
    in_telemetry = max(count_claude_sessions(observations, raw) - 1, 0)
    if issue and in_telemetry != life.blocked_count:
        notes.append(
            f"{in_telemetry} re-dispatch(es) in telemetry vs "
            f"{life.blocked_count} blocked comment(s)"
        )
    if not issue:
        notes.append("issue unknown: pass --issue N --session ID; no scores are published")
    return SessionReport(
        session=session,
        issue=issue["number"] if issue else None,
        title=issue["title"] if issue else "",
        gate_lane=footer[1] if footer else None,
        labels=[lb["name"] for lb in issue["labels"]] if issue else [],
        landed=is_landed(life, run),
        blocked_comments=life.blocked_count,
        **counts,
        accounting=account_time(leaves, life, commits),
        attribution=attribute(leaves, raw, stepper),
        notes=notes,
        started=min((lf.start for lf in leaves), default=0.0),
    )


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
    ap.add_argument("--url", default=LANGFUSE_URL, help="Langfuse base URL")
    ap.add_argument("--raw-dir", type=Path, default=Path.home() / ".ai-toolkit" / "otel-raw")
    ap.add_argument("--rules-dir", type=Path, default=Path(".ai-toolkit/rules"))
    ap.add_argument("--always-on-dir", type=Path, default=Path(".claude/rules/ai-toolkit"))
    ap.add_argument("--no-publish", action="store_true", help="print the report, push no scores")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    return ap.parse_args(argv)


def _to_epoch(flag: str, value: str | None) -> float | None:
    """Parse a --since/--until value (a date or an ISO time; no zone means UTC)."""
    if value is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise EvalError(f"{flag} {value!r} is not an ISO date or time (e.g. 2026-10-01)") from None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)).timestamp()


def _select_sessions(args, observations, raw, repo) -> dict[str, int | None]:
    """Map each wanted session to its issue number (None when unknown).

    The window and the repo check pick whole sessions by their first span; an explicit
    `--session` bypasses both. A session is always scored from all of its spans.
    """
    since, until = _to_epoch("--since", args.since), _to_epoch("--until", args.until)
    by_session = defaultdict(list)
    for o in observations:
        if o.get("sessionId") and (not args.session or o["sessionId"] in args.session):
            by_session[o["sessionId"]].append(o)
    forced = args.issue[0] if args.session and args.issue and len(args.issue) == 1 else None
    joined = {}
    for session, obs in by_session.items():
        started = min(parse_time(o["startTime"]) for o in obs)
        in_window = (since is None or started >= since) and (until is None or started <= until)
        in_repo = repo in {o["metadata"].get("repo") for o in obs}
        if args.session or (in_window and in_repo):
            joined[session] = find_issue(obs, raw.get(session, []), repo, forced)
    return {s: n for s, n in joined.items() if not args.issue or n in args.issue}


def main(argv: list[str] | None = None, *, http=None, run=None) -> int:
    """Run the evaluation; returns 0 on success and 2 on an operator-actionable error."""
    args, run = parse_args(argv), run or run_command
    try:
        repo = run(["gh", "repo", "view", "--json", "name", "-q", ".name"]).strip()
        http = http or make_http(args.url, *load_credentials(run))
        observations = fetch_observations(http)
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
    except subprocess.CalledProcessError as e:
        print(
            f"evaluate.py: error: `{e.cmd[0]}` failed: {(e.stderr or '').strip()[:200]}",
            file=sys.stderr,
        )
        return 2
    except (subprocess.TimeoutExpired, ValueError) as e:
        print(
            f"evaluate.py: error: unreadable answer from gh, git or Langfuse ({type(e).__name__})",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
