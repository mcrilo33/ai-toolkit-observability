import datetime as dt
import itertools
import json
import subprocess
import urllib.error
from pathlib import Path

import evaluate as ev
import pytest
from evaluate_report import bash_group

T0 = dt.datetime(2026, 10, 6, 14, 52, tzinfo=dt.UTC).timestamp()
SPOKE = "8065ac0d-8af2-42c4-b495-c661f02002ec"
OTHER_SPOKE = "11111111-2222-3333-4444-555555555555"
CWD_SLUG = "owner/ai-toolkit-observability"
CLAUDE_A = "3defdb84-59b3-4c9a-8c52-84c3ae0de791"
CLAUDE_B = "d74fa1e6-85c6-4e6e-a7bb-34e36625950b"
_ids = itertools.count(1)


def iso(offset_s: float) -> str:
    return (
        dt.datetime.fromtimestamp(T0 + offset_s, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
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
    "vcs,paths,forced,expected,tool_input",
    [
        pytest.param(
            ["1-feat-self-hosted-langfuse-v4-stack-on-lo"], [], None, 1, "", id="vcs-name"
        ),
        pytest.param(
            [],
            ["cd /w/orca/workspaces/ai-toolkit-observability/2-feat-eval; ls"],
            None,
            2,
            "",
            id="worktree-path",
        ),
        pytest.param(
            [],
            [],
            None,
            1,
            '{"file_path":"/w/orca/workspaces/ai-toolkit-observability/1-feat-x/.ai-toolkit/task.md"}',
            id="worktree-path-in-raw-tool-input",
        ),
        pytest.param(
            [],
            [
                "/w/orca/workspaces/ai-toolkit-observability/2-feat-eval/x",
                "/w/orca/workspaces/ai-toolkit-observability/2-feat-eval/y",
            ],
            7,
            7,
            "",
            id="forced-wins",
        ),
        pytest.param([], ["no path here"], None, None, "", id="unknown"),
    ],
)
def test_find_issue_follows_the_join_chain(vcs, paths, forced, expected, tool_input):
    observations = [make_obs("tool:Bash", 1, 2, full_command=p) for p in paths]
    raw = [{"name": "claude_code.tool_result", "attrs": {"vcs.ref.head.name": v}} for v in vcs]
    raw += [{"name": "claude_code.tool_result", "attrs": {"tool_input": tool_input}}] * bool(
        tool_input
    )

    assert ev.find_issue(observations, raw, "ai-toolkit-observability", forced) == expected


def test_find_issue_ignores_a_session_of_another_repo():
    observations = [make_obs("tool:Bash", 1, 2, full_command="/w/workspaces/ai-toolkit/2-feat-x/a")]
    observations[0]["metadata"]["repo"] = "ai-toolkit"
    branch = {"name": "claude_code.tool_result", "attrs": {"vcs.ref.head.name": "2-feat-x"}}

    assert ev.find_issue(observations, [branch], "ai-toolkit-observability", None) is None


def test_parse_lifecycle_reads_waits_gate_and_landing_from_comments():
    comments = [
        comment(180, 'Gate answered "approve" by answer.sh; please double-check: WARN'),
        comment(300, "A permission request needs a human (message msg_a, tool: Bash). Reply"),
        comment(1000, "A permission request needs a human (message msg_b, tool: Write). Reply"),
        comment(1100, "Permission answered by the human: allow (tool: Write)"),
        comment(1200, "Permission answered by the human: allow (tool: Edit)"),
        comment(2000, "A gate question needs a human (message msg_c), human: stuck. Reply"),
        comment(2600, "A gate question needs a human (message msg_d), human: stuck again. Reply"),
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
    assert not any(w[1] == 300 or w[2] == 1200 for w in waits)


REJECTION = "round {n}: address: the independent review requested changes. Blockers: {blockers}"
TDD_BLOCKER = "(1) tdd_followed is false: add the failing test that proves the behavior change"
WEAKENED_BLOCKER = "(2) tests_weakened: the diff weakens or skips tests"


def test_parse_lifecycle_records_each_review_rejection_with_its_two_flags():
    comments = [
        comment(100, REJECTION.format(n=1, blockers=f"{TDD_BLOCKER} {WEAKENED_BLOCKER}")),
        comment(200, REJECTION.format(n=2, blockers="(1) the diff drops a docstring")),
        comment(300, "round 3 (attended, decided by the user): tdd_followed is false: x"),
        comment(400, "blocked: review still rejects after 2 rounds"),
        comment(500, "Free text: tests_weakened: the diff weakens or skips tests"),
        comment(600, REJECTION.format(n=3, blockers="(1) a quote of tests_weakened: in prose")),
    ]

    life = ev.parse_lifecycle(comments)

    assert [(r.at - T0, r.tdd_failed, r.tests_weakened) for r in life.reviews] == [
        (100, True, True),
        (200, False, False),
        (600, False, False),
    ]


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


def raw_agent_result(offset, tool_use_id, subagent_type, claude=CLAUDE_A):
    attrs = {"tool_use_id": tool_use_id, "tool_name": "Agent", "session.id": claude}
    attrs["tool_parameters"] = json.dumps({"subagent_type": subagent_type})
    return {"t": T0 + offset, "name": "claude_code.tool_result", "attrs": attrs}


def test_count_dispatches_counts_review_rounds_and_redispatches():
    review_a = make_obs("sub-agent:code-review", 10, 10.01, subagent_type="code-review")
    review_b = make_obs(
        "sub-agent:code-review", 900, 900.01, claude=CLAUDE_B, subagent_type="code-review"
    )
    other = make_obs("sub-agent:planner", 20, 20.01, subagent_type="planner")

    counts = ev.count_dispatches(
        [review_a, review_b, other, make_obs("tool:Bash", 1, 2, claude=CLAUDE_B)], [], ev.Lifecycle()
    )

    assert counts == {"review_rounds": 2, "redispatches": 1}


def test_count_dispatches_reads_the_raw_jsonl_and_the_blocked_comments_too():
    seen = make_obs("sub-agent:code-review", 10, 10.01, subagent_type="code-review")
    raw = [
        raw_agent_result(11, "toolu_a", "code-review"),
        raw_agent_result(500, "toolu_b", "code-review", claude=CLAUDE_B),
        raw_agent_result(501, "toolu_c", "planner", claude="claude-c"),
    ]
    life = ev.parse_lifecycle([comment(60, "blocked: worker_done failed")])

    counts = ev.count_dispatches([seen], raw, life)

    assert counts == {"review_rounds": 2, "redispatches": 2}
    assert ev.count_dispatches([seen], [], ev.parse_lifecycle([comment(60, "blocked: x")] * 3))[
        "redispatches"
    ] == 3


def test_is_landed_trusts_the_comment_and_checks_git_when_it_can():
    calls = []

    def run(args):
        calls.append(args)
        return ""

    life = ev.parse_lifecycle([comment(10, "landed in abc1234")])

    assert ev.is_landed(life, run) is True
    assert calls[0][:2] == ["git", "merge-base"]
    assert ev.is_landed(ev.parse_lifecycle([]), run) is False


@pytest.mark.parametrize(
    "exit_code,landed",
    [pytest.param(1, False, id="not-an-ancestor"), pytest.param(128, True, id="sha-unknown")],
)
def test_is_landed_lets_git_contradict_the_comment_only_when_it_knows_the_sha(exit_code, landed):
    def run(args):
        raise subprocess.CalledProcessError(exit_code, args)

    assert ev.is_landed(ev.parse_lifecycle([comment(10, "landed in abc1234")]), run) is landed


def test_load_commits_keeps_only_this_issue_in_time_order():
    log = f"{int(T0 + 20)}\tfeat(x): b (#2)\n{int(T0 + 5)}\ttest: a (#2)\n"
    log += f"{int(T0 + 9)}\tfeat: other (#20)\n{int(T0 + 7)}\tMerge branch (#2)\n"

    commits = ev.load_commits(lambda args: log, 2)

    assert [(c.kind, c.ts - T0) for c in commits] == [("test", 5), ("feat", 20)]


def test_build_leaves_keeps_the_cost_of_a_generation_without_an_end_time():
    gen = make_obs("claude_code.llm_request", 5, 6, type_="GENERATION", cost=0.3)
    gen["endTime"] = None

    (leaf,) = ev.build_leaves([gen])

    assert (leaf.cost, leaf.end - leaf.start) == (0.3, 0)


def raw_api_request(offset, request_id, cost, duration_ms):
    attrs = {"request_id": request_id, "cost_usd": cost, "duration_ms": str(duration_ms)}
    attrs |= {"input_tokens": "100", "output_tokens": "10", "session.id": CLAUDE_A}
    return {"t": T0 + offset, "name": "claude_code.api_request", "attrs": attrs}


def raw_tool_result(offset, tool_use_id, tool, tool_input="{}", duration_ms=4):
    attrs = {"tool_use_id": tool_use_id, "tool_name": tool, "tool_input": tool_input}
    attrs |= {"duration_ms": str(duration_ms), "success": "true", "session.id": CLAUDE_A}
    return {"t": T0 + offset, "name": "claude_code.tool_result", "attrs": attrs}


def test_build_leaves_fills_spans_langfuse_never_received_from_raw_without_duplicates():
    seen_llm = make_obs(
        "claude_code.llm_request", 100, 110, type_="GENERATION", cost=0.5, request_id="req_seen"
    )
    seen_tool = make_obs("tool:Bash", 120, 121, tool_use_id="toolu_seen")
    rule = '{"file_path":"/w/.ai-toolkit/rules/workflow.md"}'
    raw = [
        raw_api_request(10, "req_seen", 0.01, 2000),
        raw_api_request(20, "req_new", 0.25, 3000),
        raw_tool_result(5, "toolu_seen", "Bash"),
        raw_tool_result(12, "toolu_new", "Read", rule),
    ]

    leaves = ev.build_leaves([seen_llm, seen_tool], raw)

    filled = {lf.name: lf for lf in leaves if lf.source == "raw"}
    assert set(filled) == {"claude_code.llm_request", "Read"} and len(leaves) == 4
    llm = filled["claude_code.llm_request"]
    assert (llm.cost, llm.start, llm.end) == (0.25, T0 + 17, T0 + 20)
    assert filled["Read"].file_path.endswith("rules/workflow.md")


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
        make_obs(
            "tool:Bash", 5, 6, full_command="sed -n 1,60p .ai-toolkit/rules/workflow.md | cut -c1-9"
        ),
        make_obs("tool:Bash", 7, 8, full_command="ls .ai-toolkit/rules/; cat README.md"),
        make_obs(
            "tool:Bash", 8, 9, full_command='git commit -m "docs: x; cat .ai-toolkit/rules/c.md"'
        ),
        make_obs(
            "tool:Bash",
            9,
            10,
            full_command="cat .ai-toolkit/rules/a.md .ai-toolkit/rules/b.md && gh issue view 2",
        ),
        make_obs(
            "tool:Bash", 8, 9, full_command='git commit -m "cat .ai-toolkit/rules/issue-hygiene.md"'
        ),
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
    assert ("plan", "Bash") not in stats.tools
    bash = [v for (step, name), v in stats.tools.items() if name.startswith("bash-")]
    assert (sum(v["calls"] for v in bash), sum(v["failures"] for v in bash)) == (11, 1)
    assert stats.tools[("plan", "bash-read")]["calls"] == 3
    other = stats.tools[("plan", "bash-other")]
    assert ev.percentile(other["durations"], 90) == 10
    assert stats.hooks[("plan", "PreToolUse:Bash")] == {"runs": 2, "total_ms": 100.0, "blocking": 1}
    assert dict(stats.rules) == {"bug-triage": 1, "workflow": 1, "a": 1, "b": 1}


@pytest.mark.parametrize(
    "command,group",
    [
        ("cd x && git status", "git"),
        ("FOO=1 timeout 60 pytest -q", "test"),
        ("cd /w; BAR=2 env gh pr view 3", "gh"),
        ("grep a f | wc -l", "read"),
        ("/w/.ai-toolkit/scripts/foo.sh --x", "toolkit"),
        ("orca orchestration send --type worker_done", "orca"),
        ("python3 scripts/x.py", "python"),
        ("frobnicate --now", "other"),
        ("", "other"),
    ],
)
def test_bash_group_classifies_a_command_by_its_first_real_program(command, group):
    assert bash_group(command) == group


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
        repo="ai-toolkit-observability",
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
    changed = make_report(review_rounds=9, landed=False)
    changed.accounting.steps["plan"].cost_usd = 5.0

    assert first == second
    assert [s["id"] for s in ev.build_scores(changed)] == [s["id"] for s in first]
    assert not {s["id"] for s in ev.build_scores(make_report(session="other"))} & {
        s["id"] for s in first
    }
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


def test_build_scores_carry_the_environment_only_when_the_session_has_one():
    tagged = ev.build_scores(make_report(environment="ai-toolkit"))
    untagged = ev.build_scores(make_report())

    assert {s["environment"] for s in tagged} == {"ai-toolkit"}
    assert not any("environment" in s for s in untagged)
    assert [s["id"] for s in tagged] == [s["id"] for s in untagged]


@pytest.mark.parametrize(
    "tdd_followed,tests_weakened,expected",
    [
        pytest.param(False, True, {"review_tdd_followed": 0, "review_tests_weakened": 1}, id="hit"),
        pytest.param(True, False, {"review_tdd_followed": 1, "review_tests_weakened": 0}, id="ok"),
        pytest.param(None, None, {}, id="no-verdict"),
    ],
)
def test_build_scores_publish_the_review_flags_only_when_a_rejection_judged_the_session(
    tdd_followed, tests_weakened, expected
):
    rep = make_report(
        review_tdd_followed=tdd_followed, review_tests_weakened=tests_weakened, environment="repo"
    )
    scores = ev.build_scores(rep)
    flags = {s["name"]: s for s in scores if s["name"].startswith("review_t")}

    assert {n: s["value"] for n, s in flags.items()} == expected
    assert all(s["dataType"] == "BOOLEAN" and s["environment"] == "repo" for s in flags.values())
    assert [s["id"] for s in scores] == [s["id"] for s in ev.build_scores(rep)]


def test_analyse_session_takes_the_rejections_posted_during_its_spans_and_the_one_after():
    comments = [
        comment(100, REJECTION.format(n=1, blockers=WEAKENED_BLOCKER)),
        comment(900, REJECTION.format(n=2, blockers="(1) a naming nit")),
        comment(1200, REJECTION.format(n=3, blockers=TDD_BLOCKER)),
        comment(1250, "blocked: review still rejects after 2 rounds"),
        comment(1500, REJECTION.format(n=1, blockers=WEAKENED_BLOCKER)),
        comment(2000, "landed in abc1234"),
    ]
    issue = {"number": 1, "title": "t", "body": "Gate: plan", "labels": [], "comments": comments}

    def verdict(*spans):
        obs = [make_obs("tool:Bash", start, end) for start, end in spans]
        rep = ev.analyse_session(SPOKE, "ai-toolkit-observability", obs, [], issue, [], lambda *_: "")
        return rep.review_tdd_followed, rep.review_tests_weakened

    assert verdict((0, 60)) == (True, True)  # only the first rejection
    assert verdict((200, 300), (500, 800)) == (True, False)  # only the second
    assert verdict((0, 60), (200, 800)) == (True, True)  # a re-dispatch: rounds 1 and 2
    assert verdict((1000, 1100)) == (False, False)
    assert verdict((1210, 1240)) == (None, None)  # a blocked: verdict closes the window
    assert verdict((1300, 1400)) == (True, True)
    assert verdict((1600, 1700)) == (None, None)  # approved and landed


def test_build_scores_publish_component_scores_summed_over_steps_without_zeros():
    review = make_obs("sub-agent:code-review", 500, 500.01)
    slow = make_obs("tool:WebFetch", 100, 140)
    quick = make_obs("tool:Grep", 150, 151)
    failing = make_obs("tool:Bash", 30, 31)
    observations = [
        make_obs("sub-agent:llm", 500, 510, type_="GENERATION", parent=review["id"], cost=0.4),
        make_obs("claude_code.llm_request", 0, 5, type_="GENERATION", cost=0.1),
        review,
        make_obs("tool:Read", 1, 2, file_path="/w/.ai-toolkit/rules/bug-triage.md"),
        failing,
        make_obs("claude_code.tool.execution", 30, 31, parent=failing["id"], level="ERROR"),
        make_obs("tool:Bash", 2000, 2001),
        slow,
        make_obs("claude_code.tool.blocked_on_user", 105, 135, parent=slow["id"]),
        quick,
        make_obs("claude_code.tool.blocked_on_user", 150, 150.5, parent=quick["id"]),
    ]
    raw = [
        hook_record(1, "PreToolUse:Bash", 40),
        hook_record(2, "PreToolUse:Bash", 60, blocking="1"),
        hook_record(3, "SessionStart", 7),
    ]
    attribution = run_attribution(
        ev.build_leaves(observations), raw, [comment(1000, "Gate answered")]
    )

    scores = ev.build_scores(make_report(attribution=attribution))

    published = {s["name"]: s["value"] for s in scores}
    kinds = {"agent", "tool", "hook", "rule"}
    components = {n: v for n, v in published.items() if n.split("_")[0] in kinds}
    assert components == pytest.approx(
        {
            "agent_main_cost_usd": 0.1,
            "agent_main_calls": 1,
            "agent_main_llm_s": 5,
            "agent_code-review_cost_usd": 0.4,
            "agent_code-review_calls": 1,
            "agent_code-review_llm_s": 10,
            "tool_Agent_calls": 1,
            "tool_Read_calls": 1,
            "tool_bash-other_calls": 2,
            "tool_bash-other_failures": 1,
            "tool_WebFetch_calls": 1,
            "tool_WebFetch_long_waits": 1,
            "tool_WebFetch_long_wait_s": 30,
            "tool_Grep_calls": 1,
            "hook_PreToolUse:Bash_runs": 2,
            "hook_PreToolUse:Bash_total_ms": 100,
            "hook_PreToolUse:Bash_blocking": 1,
            "hook_SessionStart_runs": 1,
            "hook_SessionStart_total_ms": 7,
            "rule_bug-triage_reads": 1,
        }
    )
    assert [s for s in scores if s["name"] not in components] == ev.build_scores(make_report())
    assert all(s["dataType"] == "NUMERIC" for s in scores if s["name"] in components)
    assert [s["id"] for s in scores] == [
        s["id"] for s in ev.build_scores(make_report(attribution=attribution))
    ]
    assert len({s["id"] for s in scores}) == len(scores)


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
        pytest.param(urllib.error.HTTPError("u", 401, "no", {}, None), "HTTP 401", id="http-error"),
    ],
)
def test_make_http_wraps_failures_without_leaking_the_key(failure, message):
    def fake_open(request, timeout):
        assert request.get_header("Authorization").startswith("Basic ")
        assert "Authorization" not in request.headers  # never copied to a redirect target
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


def sessions_rows(*reports):
    agg = ev.aggregate(list(reports), unused_rules=[], rule_sizes=[])
    return agg, agg["sessions"]


def test_sessions_table_orders_rows_by_start_time_and_picks_the_costliest_step():
    late = make_report(session="late", issue=2, started=200.0)
    late.accounting.steps["review"].cost_usd = 3.0
    late.accounting.steps["implement"].cost_usd = 1.0
    late.accounting.steps["implement"].active_s = 120.0
    late.accounting.steps["implement"].waiting_s = 30.0
    early = make_report(session="early", issue=1, started=100.0, landed=False)
    early.accounting.steps["implement"].cost_usd = 2.0

    agg, rows = sessions_rows(late, early)

    assert [r["issue"] for r in rows] == [1, 2]
    assert rows[1] == dict(
        repo="ai-toolkit-observability",
        issue=2,
        gate_lane="plan",
        landed=True,
        cost_usd=4.0,
        active_min=pytest.approx(2.0),
        waiting_min=pytest.approx(0.5),
        review_rounds=2,
        redispatches=1,
        top_step="review",
    )
    assert rows[0]["landed"] is False and rows[0]["top_step"] == "implement"
    text = ev.render_report([early, late], agg)
    table = text.split("Sessions\n")[1].split("\n\n")[0].splitlines()
    assert table[0].split() == [
        "repo", "issue", "gate_lane", "landed", "cost_usd", "active_min", "waiting_min",
        "review_rounds", "redispatches", "top_step",
    ]  # fmt: skip
    assert table[1].split()[:4] == ["ai-toolkit-observability", "#1", "plan", "false"]
    assert table[2].split()[1:] == ["#2", "plan", "true", "4.00", "2.0", "0.5", "2", "1", "review"]


def test_sessions_table_keeps_a_raw_only_session_with_dashes_for_what_is_missing():
    bare = make_report(issue=None, gate_lane=None, landed=False, started=0.0)
    bare.accounting.steps["implement"].active_s = 0.0

    agg, rows = sessions_rows(bare)

    assert rows[0]["issue"] is None and rows[0]["gate_lane"] is None
    assert rows[0]["top_step"] is None and rows[0]["cost_usd"] == 0.0
    line = ev.render_report([bare], agg).split("Sessions\n")[1].splitlines()[1].split()
    assert line[1:4] == ["—", "unknown", "false"] and line[-1] == "—"


def raw_line(session, name, attrs, offset):
    """One OTLP log line as the collector's file exporter writes it."""
    pairs = [{"key": k, "value": {"stringValue": v}} for k, v in attrs.items()]
    record = {
        "timeUnixNano": str(int((T0 + offset) * 1e9)),
        "body": {"stringValue": name},
        "attributes": pairs,
    }
    resource = [{"key": "spoke_run_id", "value": {"stringValue": session}}]
    entry = {
        "resourceLogs": [
            {"resource": {"attributes": resource}, "scopeLogs": [{"logRecords": [record]}]}
        ]
    }
    return json.dumps(entry)


def test_load_raw_reads_rotated_files_and_keeps_only_wanted_sessions(tmp_path):
    hook = {"hook_name": "PreToolUse:Bash", "total_duration_ms": "40"}
    (tmp_path / "logs.jsonl").write_text(
        raw_line(SPOKE, "claude_code.hook_execution_complete", hook, 5)
        + "\n"
        + raw_line("other-session", "claude_code.hook_execution_complete", hook, 6)
        + "\nnot json\n"
    )
    (tmp_path / "logs-2026-10-04T17-06-15.005-size.jsonl").write_text(
        raw_line(SPOKE, "claude_code.tool_result", {"vcs.ref.head.name": "1-feat-x"}, 1)
        + "\n"
        + raw_line(SPOKE, "claude_code.api_request", {"model": "m"}, 2)
        + "\n"
        + raw_line(SPOKE, "claude_code.user_prompt", {"prompt": "x"}, 3)
        + "\n"
    )

    batch = json.loads(raw_line("other-session", "claude_code.user_prompt", {}, 8))
    late = json.loads(raw_line(SPOKE, "claude_code.api_request", {}, 9))  # no record-level id
    batch["resourceLogs"] += late["resourceLogs"]
    (tmp_path / "logs-batched.jsonl").write_text(json.dumps(batch) + "\n")

    raw = ev.load_raw(tmp_path, {SPOKE})

    assert sorted(r["name"] for r in raw[SPOKE]) == [
        "claude_code.api_request",
        "claude_code.api_request",
        "claude_code.hook_execution_complete",
        "claude_code.tool_result",
    ]
    assert "other-session" not in raw


def test_fetch_observations_follows_the_cursor():
    pages = {
        None: {"data": [{"id": "a"}], "meta": {"cursor": "c1"}},
        "c1": {"data": [{"id": "b"}], "meta": {}},
    }
    seen = []

    def http(method, path, body=None):
        seen.append(path)
        return pages["c1" if "cursor=c1" in path else None]

    assert [o["id"] for o in ev.fetch_observations(http)] == ["a", "b"]
    assert "fromStartTime" not in seen[0]


# The World session: 16 session-level scores plus its component scores (12 with the raw hooks).
WORLD_SCORES, WORLD_SCORES_NO_RAW = 28, 26


class World:
    """Canned Langfuse, gh, git and Keychain answers for `main`."""

    KEY = "sk-lf-very-secret"

    def __init__(self, tmp_path, *, down=False, with_raw=True):
        review = make_obs("sub-agent:code-review", 100, 100.01, subagent_type="code-review")
        self.observations = [
            make_obs(
                "tool:Bash",
                0,
                5,
                full_command="cd /w/orca/workspaces/ai-toolkit-observability/1-feat-x",
            ),
            make_obs("claude_code.llm_request", 10, 20, type_="GENERATION", cost=0.5),
            make_obs("tool:Read", 30, 31, file_path="/w/.ai-toolkit/rules/bug-triage.md"),
            review,
            make_obs("sub-agent:llm", 100, 130, type_="GENERATION", parent=review["id"], cost=0.25),
            make_obs("tool:Bash", 200, 205, full_command="git push -u origin HEAD"),
            make_obs(
                "tool:Bash", 210, 215, full_command="orca orchestration send --type worker_done"
            ),
        ]
        self.issue = {
            "number": 1,
            "title": "feat: x",
            "state": "CLOSED",
            "labels": [{"name": "enhancement"}],
            "body": "## Problem\n\nScope: a\nGate: plan\n",
            "comments": [comment(60, 'Gate answered "approve"'), comment(300, "landed in abc1234")],
        }
        self.issue_of = {CWD_SLUG: self.issue}
        self.http_calls, self.run_calls, self.down = [], [], down
        self.raw_dir, self.rules_dir, self.always_dir = (
            tmp_path / "raw",
            tmp_path / "rules",
            tmp_path / "always",
        )
        for d in (self.raw_dir, self.rules_dir, self.always_dir):
            d.mkdir()
        (self.rules_dir / "bug-triage.md").write_text("x")
        (self.rules_dir / "workflow.md").write_text("x")
        (self.always_dir / "guidelines.md").write_text("g" * 400)
        if with_raw:
            hook = {"hook_name": "PreToolUse:Bash", "total_duration_ms": "40"}
            early = {"request_id": "req_early", "cost_usd": "0.1", "duration_ms": "2000"}
            (self.raw_dir / "logs.jsonl").write_text(
                raw_line(SPOKE, "claude_code.hook_execution_complete", hook, 12)
                + "\n"
                + raw_line(SPOKE, "claude_code.api_request", early, -50)
                + "\n"
            )

    def http(self, method, path, body=None):
        self.http_calls.append((method, path, body))
        if self.down:
            raise ev.EvalError(
                "Langfuse is not reachable at http://127.0.0.1:3000; run scripts/langfuse.sh up"
            )
        return {"data": self.observations, "meta": {}} if "/v2/observations" in path else {}

    def run(self, args):
        self.run_calls.append(args)
        if args[:3] == ["gh", "issue", "view"]:
            return json.dumps(self.issue_of[args[args.index("-R") + 1]])
        if args[:3] == ["gh", "repo", "view"]:
            return json.dumps({"name": "ai-toolkit-observability", "nameWithOwner": CWD_SLUG})
        if "log" in args:
            return f"{int(T0 + 150)}\tfeat: x (#1)\n"
        return self.KEY if args[0] == "security" else ""

    def main(self, *extra):
        argv = [
            "--raw-dir",
            str(self.raw_dir),
            "--rules-dir",
            str(self.rules_dir),
            "--always-on-dir",
            str(self.always_dir),
            *extra,
        ]
        return ev.main(argv, http=self.http, run=self.run)

    def posted_ids(self):
        return sorted(c[2]["id"] for c in self.http_calls if c[0] == "POST")


def test_main_reports_one_session_and_a_rerun_posts_the_same_score_ids(tmp_path, capsys):
    world = World(tmp_path)

    assert world.main() == 0
    first = world.posted_ids()
    assert world.main() == 0

    out = capsys.readouterr().out
    assert "landed=true" in out and "review_rounds=1" in out and "gate_lane=plan" in out
    assert "gap-filled from the raw JSONL" in out and "1 LLM request" in out
    assert "raw JSONL covers only" in out and "lower bound" in out
    assert "PreToolUse:Bash" in out and "bug-triage" in out and "Zero-use rules: workflow" in out
    assert len(first) == WORLD_SCORES and world.posted_ids() == sorted(first * 2)
    assert {c[2]["environment"] for c in world.http_calls if c[0] == "POST"} == {
        "ai-toolkit-observability"
    }
    assert World.KEY not in out and not any(World.KEY in " ".join(a) for a in world.run_calls)


def test_main_notes_a_blocked_comment_without_a_second_claude_session(tmp_path, capsys):
    world = World(tmp_path)
    world.issue["comments"].append(comment(70, "blocked: worker_done failed"))

    assert world.main("--json") == 0

    notes = json.loads(capsys.readouterr().out)["sessions"][0]["notes"]
    assert any("0 re-dispatch(es) in telemetry vs 1 blocked comment(s)" in n for n in notes)


def test_main_keeps_trace_numbers_when_the_raw_jsonl_rotated_away(tmp_path, capsys):
    world = World(tmp_path, with_raw=False)

    assert world.main("--json") == 0

    report = json.loads(capsys.readouterr().out)
    (row,) = report["aggregates"]["sessions"]
    assert row["issue"] == 1 and row["cost_usd"] == pytest.approx(0.75)
    assert row["top_step"] in ev.STEPS
    assert any("raw JSONL" in n for n in report["sessions"][0]["notes"])
    assert any("lower bound" in n for n in report["sessions"][0]["notes"])
    assert report["sessions"][0]["cost_usd"] == pytest.approx(0.75)
    assert len(world.posted_ids()) == WORLD_SCORES_NO_RAW


def test_main_with_the_stack_down_fails_clearly_and_posts_nothing(tmp_path, capsys):
    world = World(tmp_path, down=True)

    assert world.main() == 2

    assert "not reachable" in capsys.readouterr().err
    assert world.posted_ids() == []


def test_load_credentials_asks_the_keychain_by_service_name_only():
    calls = []

    def run(args):
        calls.append(args)
        return World.KEY + "\n"

    assert ev.load_credentials(run) == (World.KEY, World.KEY)
    assert all(a[:3] == ["security", "find-generic-password", "-a"] for a in calls)
    assert not any(World.KEY in " ".join(a) for a in calls)


def test_load_credentials_names_the_missing_item_not_the_key():
    def run(args):
        raise subprocess.CalledProcessError(44, args, output=World.KEY, stderr="not found")

    with pytest.raises(ev.EvalError, match="LANGFUSE_INIT_PROJECT_PUBLIC_KEY") as exc:
        ev.load_credentials(run)
    assert World.KEY not in str(exc.value)


@pytest.mark.parametrize(
    "window,scored",
    [
        pytest.param(["--since", "2026-10-06T14:55:00Z"], False, id="started-before-since"),
        pytest.param(["--until", "2026-10-06T14:53:00Z"], True, id="started-before-until"),
        pytest.param(["--since", "2026-10-06T14:00:00+02:00"], True, id="offset-is-honoured"),
    ],
)
def test_main_window_selects_sessions_and_never_clips_their_spans(tmp_path, window, scored):
    world = World(tmp_path)

    assert world.main(*window) == 0

    assert len(world.posted_ids()) == (WORLD_SCORES if scored else 0)
    assert not any("fromStartTime" in c[1] or "toStartTime" in c[1] for c in world.http_calls)


def test_main_rejects_a_window_it_cannot_parse(tmp_path, capsys):
    world = World(tmp_path)

    assert world.main("--since", "yesterday") == 2

    assert "--since" in capsys.readouterr().err
    assert world.posted_ids() == []


@pytest.mark.parametrize(
    "extra,posted",
    [
        pytest.param([], 0, id="other-repo-skipped"),
        pytest.param(
            ["--session", SPOKE, "--issue", "1"], WORLD_SCORES, id="forced-session-bypasses-repo"
        ),
    ],
)
def test_main_repo_filter_yields_to_an_explicit_session(tmp_path, extra, posted):
    world = World(tmp_path)
    for o in world.observations:
        o["metadata"]["repo"] = "some-other-repo"

    assert world.main(*extra) == 0

    assert len(world.posted_ids()) == posted
    assert not any("environment" in c[2] for c in world.http_calls if c[0] == "POST")


def add_session(world, session, repo, title, *, path_repo=None):
    """A second worker session of `repo`, in issue 1's worktree of `path_repo`, with its own issue."""
    for o in [
        make_obs("tool:Bash", 0, 5, full_command=f"cd /w/workspaces/{path_repo or repo}/1-feat-y"),
        make_obs("claude_code.llm_request", 10, 20, type_="GENERATION", cost=0.5),
    ]:
        o["sessionId"] = session
        o["metadata"]["repo"] = repo
        world.observations.append(o)
    world.issue_of[f"owner/{repo}"] = {**world.issue, "title": title}


def test_parse_repo_reads_name_slug_and_checkout(tmp_path):
    assert ev.parse_repo(f"tk=owner/tk={tmp_path}") == ("tk", ev.RepoRef("owner/tk", tmp_path))


def test_parse_repo_expands_a_tilde_in_the_checkout(monkeypatch, tmp_path):
    (tmp_path / "tk").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))

    assert ev.parse_repo("tk=owner/tk=~/tk")[1].path == tmp_path / "tk"


@pytest.mark.parametrize(
    "value",
    [
        "ai-toolkit",
        "ai-toolkit=owner/ai-toolkit",
        "=owner/tk=/p",
        "tk=owner=/p",
        "tk=owner/tk=",
        "tk=owner/tk=/no/such/checkout",
    ],
)
def test_main_rejects_a_malformed_repo_mapping_before_any_call(tmp_path, capsys, value):
    world = World(tmp_path)

    assert world.main("--repo", value) == 2

    assert value in capsys.readouterr().err
    assert world.http_calls == [] and world.posted_ids() == []


def test_main_routes_each_session_to_the_gh_and_git_of_its_own_repo(tmp_path, capsys):
    world = World(tmp_path)
    add_session(world, OTHER_SPOKE, "ai-toolkit", "feat: toolkit work")
    checkout = tmp_path / "tk"
    checkout.mkdir()

    assert world.main("--repo", f"ai-toolkit=owner/ai-toolkit={checkout}") == 0

    out = capsys.readouterr().out
    assert len(world.posted_ids()) == 48  # two sessions of issue #1 in two repos do not collide
    assert "feat: toolkit work" in out and "feat: x" in out
    views = [a[a.index("-R") + 1] for a in world.run_calls if a[:3] == ["gh", "issue", "view"]]
    assert sorted(views) == ["owner/ai-toolkit", CWD_SLUG]
    git_dirs = sorted(a[2] for a in world.run_calls if a[:2] == ["git", "-C"])
    assert git_dirs == sorted([".", ".", str(checkout), str(checkout)])  # log + merge-base each
    sessions = out.split("Sessions\n")[1].split("\n\n")[0].splitlines()
    assert sorted(line.split()[0] for line in sessions[1:]) == [
        "ai-toolkit", "ai-toolkit-observability",
    ]  # fmt: skip
    posted = {(c[2]["sessionId"], c[2]["environment"]) for c in world.http_calls if c[0] == "POST"}
    assert posted == {(SPOKE, "ai-toolkit-observability"), (OTHER_SPOKE, "ai-toolkit")}


@pytest.mark.parametrize(
    "repo,shown",
    [
        pytest.param("some-other-repo", "some-other-repo", id="named"),
        pytest.param(None, "unknown", id="no-repo-attribute"),
    ],
)
def test_main_reports_a_session_of_an_unmapped_repo_as_skipped(tmp_path, capsys, repo, shown):
    world = World(tmp_path)
    for o in world.observations:
        o["metadata"]["repo"] = repo
        if repo is None:
            del o["metadata"]["repo"]

    assert world.main() == 0

    assert f"skipped: 1 session(s) of unmapped repo(s): {shown} (1)" in capsys.readouterr().out
    assert world.posted_ids() == []


@pytest.mark.parametrize(
    "window,skipped",
    [
        pytest.param([], [{"session": SPOKE, "repo": "some-other-repo"}], id="in-window"),
        pytest.param(["--since", "2026-10-06T14:55:00Z"], [], id="outside-window-is-not-reported"),
    ],
)
def test_main_json_lists_skipped_sessions_inside_the_window_only(tmp_path, capsys, window, skipped):
    world = World(tmp_path)
    for o in world.observations:
        o["metadata"]["repo"] = "some-other-repo"

    assert world.main("--json", *window) == 0

    assert json.loads(capsys.readouterr().out)["skipped"] == skipped


def test_main_turns_a_failing_gh_into_a_clear_error_and_posts_nothing(tmp_path, capsys):
    world = World(tmp_path)
    original = world.run

    def failing(args):
        if args[:3] == ["gh", "issue", "view"]:
            raise subprocess.CalledProcessError(1, args, stderr="HTTP 401: bad credentials")
        return original(args)

    assert ev.main(["--raw-dir", str(world.raw_dir)], http=world.http, run=failing) == 2

    assert "bad credentials" in capsys.readouterr().err
    assert world.posted_ids() == []


def test_load_raw_survives_a_record_without_optional_otlp_fields(tmp_path):
    entry = json.loads(raw_line(SPOKE, "claude_code.tool_result", {}, 1))
    del entry["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["attributes"]
    (tmp_path / "logs.jsonl").write_text(json.dumps(entry) + "\n")

    raw = ev.load_raw(tmp_path, {SPOKE})

    assert [(r["name"], r["attrs"]) for r in raw[SPOKE]] == [("claude_code.tool_result", {})]
