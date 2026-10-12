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

T0 = dt.datetime(2026, 10, 6, 14, 52, tzinfo=dt.UTC).timestamp()
SPOKE = "8065ac0d-8af2-42c4-b495-c661f02002ec"
CLAUDE_A = "3defdb84-59b3-4c9a-8c52-84c3ae0de791"
CLAUDE_B = "d74fa1e6-85c6-4e6e-a7bb-34e36625950b"
_ids = itertools.count(1)


def iso(offset_s: float) -> str:
    return (
        dt.datetime.fromtimestamp(T0 + offset_s, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[
            :-3
        ]
        + "Z"
    )


def make_obs(
    name,
    start,
    end,
    *,
    type_="TOOL",
    parent=None,
    claude=CLAUDE_A,
    cost=None,
    level="DEFAULT",
    **attrs,
):
    """A Langfuse v4 observation as /api/public/v2/observations returns it."""
    meta = {"repo": "ai-toolkit-observability", "attributes.session.id": claude}
    meta.update({f"attributes.{k}": v for k, v in attrs.items()})
    return {
        "id": f"o{next(_ids)}",
        "parentObservationId": parent,
        "type": type_,
        "name": name,
        "startTime": iso(start),
        "endTime": iso(end),
        "level": level,
        "sessionId": SPOKE,
        "latency": end - start,
        "totalCost": cost,
        "metadata": meta,
        "usageDetails": {"total": 100} if cost else {},
    }


def comment(offset_s, body):
    return {"createdAt": iso(offset_s), "body": body}


@pytest.mark.parametrize(
    "vcs,paths,forced,expected",
    [
        pytest.param(["1-feat-self-hosted-langfuse-v4-stack-on-lo"], [], None, 1, id="vcs-name"),
        pytest.param(
            [],
            ["cd /w/orca/workspaces/ai-toolkit-observability/2-feat-eval; ls"],
            None,
            2,
            id="worktree-path",
        ),
        pytest.param(
            [],
            [
                "/w/orca/workspaces/ai-toolkit-observability/2-feat-eval/x",
                "/w/orca/workspaces/ai-toolkit-observability/2-feat-eval/y",
            ],
            7,
            7,
            id="forced-wins",
        ),
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
    review_llm = make_obs(
        "sub-agent:llm", 500, 600, type_="GENERATION", parent=review_agent["id"], cost=0.5
    )
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
    assert acct.steps["ship"].other_s == pytest.approx(
        10
    )  # the 710-720 gap sits inside the ship window
    assert acct.implement_parts["red"] == pytest.approx(60)
    assert acct.implement_parts["green"] == pytest.approx(0)


def test_count_dispatches_counts_review_rounds_and_redispatches():
    review_a = make_obs("sub-agent:code-review", 10, 10.01, subagent_type="code-review")
    review_b = make_obs(
        "sub-agent:code-review", 900, 900.01, claude=CLAUDE_B, subagent_type="code-review"
    )
    other = make_obs("sub-agent:planner", 20, 20.01, subagent_type="planner")

    counts = ev.count_dispatches(
        [review_a, review_b, other, make_obs("tool:Bash", 1, 2, claude=CLAUDE_B)]
    )

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


def run_attribution(leaves, raw, comments=(), commits=()):
    life = ev.parse_lifecycle(list(comments))
    return ev.attribute(leaves, raw, ev.Stepper(leaves, life, list(commits)))


def hook_record(offset, name, ms, blocking="0"):
    return {
        "t": T0 + offset,
        "name": "claude_code.hook_execution_complete",
        "attrs": {"hook_name": name, "total_duration_ms": str(ms), "num_blocking": blocking},
    }


def test_attribute_counts_agents_tools_hooks_and_rules_per_step():
    review = make_obs("sub-agent:code-review", 500, 500.01)
    observations = [
        make_obs("sub-agent:llm", 500, 510, type_="GENERATION", parent=review["id"], cost=0.4),
        make_obs("claude_code.llm_request", 0, 5, type_="GENERATION", cost=0.1),
        review,
        make_obs("tool:Read", 1, 2, file_path="/w/.ai-toolkit/rules/bug-triage.md"),
        make_obs("tool:Read", 3, 4, file_path="/w/README.md"),
        *[make_obs("tool:Bash", 10 + i, 10 + i + d) for i, d in enumerate([1, 2, 3, 4, 10])],
    ]
    failing = make_obs("tool:Bash", 30, 31)
    observations += [
        failing,
        make_obs("claude_code.tool.execution", 30, 31, parent=failing["id"], level="ERROR"),
    ]
    raw = [
        hook_record(1, "PreToolUse:Bash", 40),
        hook_record(2, "PreToolUse:Bash", 60, blocking="1"),
    ]

    stats = run_attribution(ev.build_leaves(observations), raw, [comment(1000, "Gate answered")])

    assert stats.agents[("review", "code-review")]["cost_usd"] == pytest.approx(0.4)
    assert stats.agents[("plan", "main")]["calls"] == 1
    bash = stats.tools[("plan", "Bash")]
    assert (bash["calls"], bash["failures"]) == (6, 1)
    assert ev.percentile(bash["durations"], 90) == 10
    assert stats.hooks[("plan", "PreToolUse:Bash")] == {"runs": 2, "total_ms": 100.0, "blocking": 1}
    assert dict(stats.rules) == {"bug-triage": 1}


def test_measure_rule_sizes_marks_path_scoped_rules(tmp_path):
    (tmp_path / "a.md").write_text("x" * 400)
    (tmp_path / "b.md").write_text("---\npaths:\n  - '**/*.py'\n---\n" + "y" * 80)

    sizes = {r.name: r for r in ev.measure_rule_sizes(tmp_path)}

    assert (sizes["a"].tokens, sizes["a"].scoped) == (100, False)
    assert sizes["b"].scoped is True


def test_list_unused_rules_lists_rule_files_never_read(tmp_path):
    (tmp_path / "bug-triage.md").write_text("x")
    (tmp_path / "workflow.md").write_text("x")

    assert ev.list_unused_rules(tmp_path, {"bug-triage": 3}) == ["workflow"]


def make_report(**kw):
    acct = ev.Accounting(
        {s: ev.StepTime() for s in ev.STEPS},
        {"red": 0.0, "green": 0.0, "refactor": 0.0, "other": 0.0},
    )
    acct.steps["implement"].active_s = 60.0
    base = dict(
        session=SPOKE,
        issue=1,
        title="t",
        gate_lane="plan",
        landed=True,
        review_rounds=2,
        redispatches=1,
        accounting=acct,
        attribution=ev.Attribution(),
        notes=[],
        labels=[],
    )
    return ev.SessionReport(**{**base, **kw})


def test_build_scores_is_idempotent_and_typed():
    first, second = ev.build_scores(make_report()), ev.build_scores(make_report())

    assert first == second
    by_name = {s["name"]: s for s in first}
    assert len({s["id"] for s in first}) == len(first)
    assert (by_name["landed"]["dataType"], by_name["landed"]["value"]) == ("BOOLEAN", 1)
    assert (by_name["gate_lane"]["dataType"], by_name["gate_lane"]["value"]) == (
        "CATEGORICAL",
        "plan",
    )
    assert by_name["step_implement_active_s"]["value"] == 60.0
    assert {
        "cost_usd_total",
        "duration_active_s",
        "waiting_s",
        "review_rounds",
        "redispatches",
    } <= set(by_name)
    assert all(s["sessionId"] == SPOKE for s in first)


class FakeHttp:
    def __init__(self, down=False):
        self.calls, self.down = [], down

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.down:
            raise ev.EvalError(
                "Langfuse is not reachable at http://127.0.0.1:3000; run scripts/langfuse.sh up"
            )
        return {"id": body["id"]} if body else {"status": "OK"}


def test_publish_scores_posts_every_score_after_a_health_check():
    http = FakeHttp()

    ev.publish_scores(http, ev.build_scores(make_report()))

    assert http.calls[0][:2] == ("GET", "/api/public/health")
    assert {c[0] for c in http.calls[1:]} == {"POST"}
    assert len(http.calls) > 5


def test_publish_scores_with_the_stack_down_pushes_nothing():
    http = FakeHttp(down=True)

    with pytest.raises(ev.EvalError, match="not reachable"):
        ev.publish_scores(http, ev.build_scores(make_report()))

    assert [c[0] for c in http.calls] == ["GET"]


@pytest.mark.parametrize(
    "failure,message",
    [
        pytest.param(OSError("refused"), "not reachable", id="connection-refused"),
        pytest.param(
            ev.urllib.error.HTTPError("u", 401, "no", {}, None), "HTTP 401", id="http-error"
        ),
    ],
)
def test_make_http_wraps_failures_without_leaking_the_key(failure, message):
    def fake_open(request, timeout):
        assert request.get_header("Authorization").startswith("Basic ")
        raise failure

    http = ev.make_http("http://127.0.0.1:3000", "pk-lf-x", "sk-lf-secret", fake_open)

    with pytest.raises(ev.EvalError, match=message) as exc:
        http("GET", "/api/public/health")
    assert "sk-lf-secret" not in str(exc.value)


def test_render_report_has_the_four_component_tables_and_the_zero_use_list():
    rep = make_report(notes=["raw JSONL rotated away: hooks and branch name unavailable"])
    rep.attribution.rules["bug-triage"] = 2
    agg = ev.aggregate(
        [rep], unused_rules=["workflow"], rule_sizes=[ev.RuleSize("guidelines", 400, 100, False)]
    )

    text = ev.render_report([rep], agg)

    for heading in (
        "Agents",
        "Tools",
        "Hooks",
        "On-demand rules",
        "Zero-use rules: workflow",
        "cost only - usefulness not measurable here",
    ):
        assert heading in text
    assert "raw JSONL rotated away" in text
    assert "landed=true" in text and "review_rounds=2" in text
