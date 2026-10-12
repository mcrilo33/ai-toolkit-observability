import datetime as dt
import importlib.util
import itertools
import sys

import pytest

from conftest import REPO

_spec = importlib.util.spec_from_file_location("evaluate", REPO / "scripts" / "evaluate.py")
ev = importlib.util.module_from_spec(_spec)
sys.modules["evaluate"] = ev
_spec.loader.exec_module(ev)

T0 = dt.datetime(2026, 10, 6, 14, 52, tzinfo=dt.timezone.utc).timestamp()
SPOKE = "8065ac0d-8af2-42c4-b495-c661f02002ec"
CLAUDE_A = "3defdb84-59b3-4c9a-8c52-84c3ae0de791"
CLAUDE_B = "d74fa1e6-85c6-4e6e-a7bb-34e36625950b"
_ids = itertools.count(1)


def iso(offset_s: float) -> str:
    return dt.datetime.fromtimestamp(T0 + offset_s, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def make_obs(name, start, end, *, type_="TOOL", parent=None, claude=CLAUDE_A, cost=None, level="DEFAULT", **attrs):
    """A Langfuse v4 observation as /api/public/v2/observations returns it."""
    meta = {"repo": "ai-toolkit-observability", "attributes.session.id": claude}
    meta.update({f"attributes.{k}": v for k, v in attrs.items()})
    return {
        "id": f"o{next(_ids)}", "parentObservationId": parent, "type": type_, "name": name,
        "startTime": iso(start), "endTime": iso(end), "level": level, "sessionId": SPOKE,
        "latency": end - start, "totalCost": cost, "metadata": meta,
        "usageDetails": {"total": 100} if cost else {},
    }


def comment(offset_s, body):
    return {"createdAt": iso(offset_s), "body": body}


@pytest.mark.parametrize(
    "vcs,paths,forced,expected",
    [
        pytest.param(["1-feat-self-hosted-langfuse-v4-stack-on-lo"], [], None, 1, id="vcs-name"),
        pytest.param([], ["cd /w/orca/workspaces/ai-toolkit-observability/2-feat-eval; ls"], None, 2, id="worktree-path"),
        pytest.param([], ["/w/orca/workspaces/ai-toolkit-observability/2-feat-eval/x", "/w/orca/workspaces/ai-toolkit-observability/2-feat-eval/y"], 7, 7, id="forced-wins"),
        pytest.param([], ["no path here"], None, None, id="unknown"),
    ],
)
def test_find_issue_follows_the_join_chain(vcs, paths, forced, expected):
    observations = [make_obs("tool:Bash", 1, 2, full_command=p) for p in paths]
    raw = [{"name": "claude_code.tool_result", "attrs": {"vcs.ref.head.name": v}} for v in vcs]

    assert ev.find_issue(observations, raw, "ai-toolkit-observability", forced) == expected


def test_find_issue_ignores_a_session_of_another_repo():
    observations = [make_obs("tool:Bash", 1, 2, full_command="/w/workspaces/ai-toolkit/2-feat-x/a")]
    observations[0]["metadata"]["repo"] = "ai-toolkit"

    assert ev.find_issue(observations, [], "ai-toolkit-observability", None) is None


def test_parse_lifecycle_reads_waits_gate_and_landing_from_comments():
    comments = [
        comment(180, 'Gate answered "approve" by answer.sh; please double-check: WARN'),
        comment(300, "A permission request needs a human (message msg_a, tool: Bash). Reply"),
        comment(1000, "A permission request needs a human (message msg_b, tool: Write). Reply"),
        comment(1100, "Permission answered by the human: allow (tool: Write)"),
        comment(2000, "A gate question needs a human (message msg_c), human: stuck. Reply"),
        comment(3700, "blocked: worker_done failed: AC6 is NOT met"),
        comment(5000, "landed in 974fa117e6ac32bd436749dad21f2e3654930daf"),
    ]

    life = ev.parse_lifecycle(comments)

    assert life.gate_answered_at == T0 + 180
    assert life.blocked_count == 1
    assert life.landed_sha == "974fa117e6ac32bd436749dad21f2e3654930daf"
    waits = [(w.kind, w.start - T0, w.end - T0, w.answered) for w in life.waits]
    assert ("permission", 1000, 1100, True) in waits
    assert ("gate", 2000, 3700, False) in waits
    assert not any(w[1] == 300 for w in waits)


def build_account(leaves, comments, commits=()):
    life = ev.parse_lifecycle(comments)
    return ev.account_time(leaves, life, list(commits))


def test_account_time_does_not_charge_an_unanswered_ask_to_implement():
    observations = [
        make_obs("tool:Bash", 0, 100, full_command="gh issue view 1"),
        make_obs("tool:Bash", 200, 1700, full_command="orca orchestration ask --question q"),
        make_obs("tool:Bash", 1700, 1760, full_command="pytest"),
    ]
    comments = [
        comment(150, 'Gate answered "approve" by answer.sh'),
        comment(200, "A gate question needs a human (message msg_c), human: x. Reply"),
        comment(1700, "blocked: stopped"),
    ]

    acct = build_account(ev.build_leaves(observations), comments)

    assert acct.waiting_s == pytest.approx(1500)
    assert acct.steps["implement"].waiting_s == pytest.approx(1500)
    assert acct.steps["implement"].active_s == pytest.approx(60)
    assert acct.steps["plan"].active_s == pytest.approx(100)
    assert acct.other_s == pytest.approx(100)  # 100-200: no event, no wait


def test_account_time_splits_blocked_on_user_out_of_a_tool_span():
    tool = make_obs("tool:Write", 0, 400)
    blocked = make_obs("claude_code.tool.blocked_on_user", 10, 390, parent=tool["id"])

    acct = build_account(ev.build_leaves([tool, blocked]), [])

    assert acct.waiting_s == pytest.approx(380)
    assert acct.active_s == pytest.approx(20)


def test_account_time_marks_review_ship_and_commit_substeps():
    review_agent = make_obs("sub-agent:code-review", 500, 500.01)
    review_llm = make_obs("sub-agent:llm", 500, 600, type_="GENERATION", parent=review_agent["id"], cost=0.5)
    observations = [
        make_obs("tool:Bash", 0, 50, full_command="x"),
        make_obs("tool:Bash", 200, 260, full_command="pytest"),
        review_agent,
        review_llm,
        make_obs("tool:Bash", 700, 710, full_command="git push -u origin HEAD"),
        make_obs("tool:Bash", 720, 730, full_command="orca orchestration send --type worker_done"),
    ]
    commits = [ev.Commit(T0 + 450, "test", 1), ev.Commit(T0 + 650, "feat", 1)]

    acct = build_account(ev.build_leaves(observations), [comment(100, "Gate answered")], commits)

    assert acct.steps["plan"].active_s == pytest.approx(50)
    assert acct.steps["review"].active_s == pytest.approx(100)
    assert acct.steps["review"].cost_usd == pytest.approx(0.5)
    assert acct.steps["ship"].active_s == pytest.approx(10 + 10)
    assert acct.steps["ship"].other_s == pytest.approx(10)  # the 710-720 gap sits inside the ship window
    assert acct.implement_parts["red"] == pytest.approx(60)
    assert acct.implement_parts["green"] == pytest.approx(0)


def test_count_dispatches_counts_review_rounds_and_redispatches():
    review_a = make_obs("sub-agent:code-review", 10, 10.01, subagent_type="code-review")
    review_b = make_obs("sub-agent:code-review", 900, 900.01, claude=CLAUDE_B, subagent_type="code-review")
    other = make_obs("sub-agent:planner", 20, 20.01, subagent_type="planner")

    counts = ev.count_dispatches([review_a, review_b, other, make_obs("tool:Bash", 1, 2, claude=CLAUDE_B)])

    assert counts == {"review_rounds": 2, "redispatches": 1}


def test_is_landed_trusts_the_comment_and_checks_git_when_it_can():
    calls = []

    def run(args):
        calls.append(args)
        return ""

    life = ev.parse_lifecycle([comment(10, "landed in abc1234")])

    assert ev.is_landed(life, run) is True
    assert calls[0][:2] == ["git", "merge-base"]
    assert ev.is_landed(ev.parse_lifecycle([]), run) is False
