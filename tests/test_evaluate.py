import datetime as dt
import itertools
import json
import subprocess
import urllib.error

import evaluate as ev
import pytest

T0 = dt.datetime(2026, 10, 6, 14, 52, tzinfo=dt.UTC).timestamp()
SPOKE = "8065ac0d-8af2-42c4-b495-c661f02002ec"
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
    bash = stats.tools[("plan", "Bash")]
    assert (bash["calls"], bash["failures"]) == (11, 1)
    assert ev.percentile(bash["durations"], 90) == 4
    assert stats.hooks[("plan", "PreToolUse:Bash")] == {"runs": 2, "total_ms": 100.0, "blocking": 1}
    assert dict(stats.rules) == {"bug-triage": 1, "workflow": 1, "a": 1, "b": 1}


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
            return json.dumps(self.issue)
        if args[:3] == ["gh", "repo", "view"]:
            return "ai-toolkit-observability\n"
        if args[:2] == ["git", "log"]:
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
    assert "raw JSONL covers only" in out
    assert "PreToolUse:Bash" in out and "bug-triage" in out and "Zero-use rules: workflow" in out
    assert len(first) == 16 and world.posted_ids() == sorted(first * 2)
    assert World.KEY not in out and not any(World.KEY in " ".join(a) for a in world.run_calls)


def test_main_keeps_trace_numbers_when_the_raw_jsonl_rotated_away(tmp_path, capsys):
    world = World(tmp_path, with_raw=False)

    assert world.main("--json") == 0

    report = json.loads(capsys.readouterr().out)
    assert any("raw JSONL" in n for n in report["sessions"][0]["notes"])
    assert report["sessions"][0]["cost_usd"] == pytest.approx(0.75)
    assert len(world.posted_ids()) == 16


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

    assert len(world.posted_ids()) == (16 if scored else 0)
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
        pytest.param(["--session", SPOKE, "--issue", "1"], 16, id="forced-session-bypasses-repo"),
    ],
)
def test_main_repo_filter_yields_to_an_explicit_session(tmp_path, extra, posted):
    world = World(tmp_path)
    for o in world.observations:
        o["metadata"]["repo"] = "some-other-repo"

    assert world.main(*extra) == 0

    assert len(world.posted_ids()) == posted


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
