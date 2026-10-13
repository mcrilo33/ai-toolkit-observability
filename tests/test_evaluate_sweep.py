import datetime as dt
import os
import plistlib
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "evaluate_sweep.sh"
LABEL = "com.ai-toolkit.evaluate-sweep"

PYTHON_STUB = """#!/usr/bin/env bash
# Fake python3: records cwd and argv, optionally prints a line and fails.
printf '%s|%s\\n' "$PWD" "$*" >> "$FAKE_PY_LOG"
[ -z "${FAKE_PY_SAYS:-}" ] || echo "$FAKE_PY_SAYS" >&2
exit "${FAKE_PY_RC:-0}"
"""

LAUNCHCTL_STUB = """#!/usr/bin/env bash
# Fake launchctl: `loaded` in $FAKE_LAUNCHD is the one bit of state; every call is logged.
printf '%s\\n' "$*" >> "$FAKE_LAUNCHD/calls"
case "$1" in
  bootstrap) [ -n "${FAKE_LAUNCHCTL_REFUSES:-}" ] || touch "$FAKE_LAUNCHD/loaded" ;;
  bootout) [ -f "$FAKE_LAUNCHD/loaded" ] && rm "$FAKE_LAUNCHD/loaded" || exit 3 ;;
  print) [ -f "$FAKE_LAUNCHD/loaded" ] || exit 113 ;;
  *) echo "unexpected launchctl call: $*" >&2; exit 1 ;;
esac
"""


def _stub(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def sweep(tmp_path):
    """Run scripts/evaluate_sweep.sh against a stubbed python3 and launchctl, HOME in tmp_path."""
    bindir, home, launchd = tmp_path / "bin", tmp_path / "home", tmp_path / "launchd"
    for d in (bindir, home, launchd):
        d.mkdir()
    _stub(bindir / "python3", PYTHON_STUB)
    _stub(bindir / "launchctl", LAUNCHCTL_STUB)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(home),
        "FAKE_PY_LOG": str(tmp_path / "python.log"),
        "FAKE_LAUNCHD": str(launchd),
    }

    def run(*args, **extra):
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env={**env, **extra},
            capture_output=True,
            text=True,
            timeout=60,
            cwd=tmp_path,
        )

    run.home = home
    run.log = home / ".ai-toolkit" / "evaluate-sweep.log"
    run.plist = home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    run.python_calls = lambda: (
        (tmp_path / "python.log").read_text().splitlines()
        if (tmp_path / "python.log").exists()
        else []
    )
    run.launchctl_calls = lambda: (
        (launchd / "calls").read_text().splitlines() if (launchd / "calls").exists() else []
    )
    run.loaded = lambda: (launchd / "loaded").exists()
    return run


def _seven_days_ago():
    return (dt.datetime.now(dt.UTC) - dt.timedelta(days=7)).date()


@pytest.mark.parametrize("args", [("bogus",), ("install", "extra"), ("--since", "2026-10-01")])
def test_unknown_arguments_exit_2_with_usage_and_touch_nothing(sweep, args):
    r = sweep(*args)
    assert r.returncode == 2
    assert "usage:" in r.stderr and "install|uninstall" in r.stderr
    assert not sweep.python_calls() and not sweep.launchctl_calls()


def test_no_args_runs_evaluate_once_over_the_last_seven_days_from_the_repo_root(sweep):
    before = _seven_days_ago()
    r = sweep()
    after = _seven_days_ago()
    assert r.returncode == 0, r.stderr
    [call] = sweep.python_calls()
    cwd, argv = call.split("|")
    assert Path(cwd).resolve() == REPO
    script, flag, since = argv.split(" ")
    assert (script, flag) == (str(REPO / "scripts" / "evaluate.py"), "--since")
    assert dt.date.fromisoformat(since) in {before, after}


def test_two_sweeps_issue_the_same_command(sweep):
    assert sweep().returncode == 0 and sweep().returncode == 0
    first, second = sweep.python_calls()
    assert first == second


def test_a_failed_sweep_exits_non_zero_and_logs_why(sweep):
    r = sweep(FAKE_PY_RC="2", FAKE_PY_SAYS="Langfuse is not reachable")
    assert r.returncode == 2
    log = sweep.log.read_text()
    assert "Langfuse is not reachable" in log
    assert "failed" in log and "exit 2" in log


def test_every_sweep_appends_to_the_log_under_dot_ai_toolkit(sweep):
    sweep()
    sweep(FAKE_PY_RC="1")
    log = sweep.log.read_text()
    assert log.count("sweep start") == 2 and log.count("sweep done") == 1
    assert log.count("sweep failed") == 1


def test_install_writes_the_agent_and_loads_it(sweep):
    r = sweep("install")
    assert r.returncode == 0, r.stderr
    agent = plistlib.loads(sweep.plist.read_bytes())
    assert agent["Label"] == LABEL
    assert agent["ProgramArguments"] == ["/bin/bash", str(SCRIPT)]
    assert Path(agent["WorkingDirectory"]).resolve() == REPO
    assert agent["StartCalendarInterval"]["Hour"] in range(24)
    uid = str(os.getuid())
    calls = sweep.launchctl_calls()
    assert f"bootstrap gui/{uid} {sweep.plist}" in calls
    assert calls[-1] == f"print gui/{uid}/{LABEL}"
    assert sweep.loaded() and not sweep.python_calls()


def test_the_agent_carries_no_secret(sweep):
    sweep("install")
    agent = plistlib.loads(sweep.plist.read_bytes())
    assert set(agent.get("EnvironmentVariables", {})) <= {"PATH"}
    text = sweep.plist.read_text()
    assert "sk-lf" not in text and "pk-lf" not in text and "__" not in text


def test_install_twice_leaves_one_loaded_agent(sweep):
    assert sweep("install").returncode == 0
    first = sweep.plist.read_bytes()
    assert sweep("install").returncode == 0
    assert sweep.plist.read_bytes() == first and sweep.loaded()
    assert len(list(sweep.plist.parent.iterdir())) == 1


def test_install_fails_when_the_agent_does_not_show_up_loaded(sweep):
    r = sweep("install", FAKE_LAUNCHCTL_REFUSES="1")
    assert r.returncode != 0 and "not loaded" in r.stderr


def test_uninstall_unloads_and_removes_the_agent(sweep):
    sweep("install")
    r = sweep("uninstall")
    assert r.returncode == 0, r.stderr
    assert not sweep.plist.exists() and not sweep.loaded()
    assert f"bootout gui/{os.getuid()}/{LABEL}" in sweep.launchctl_calls()
    assert sweep.launchctl_calls()[-1] == f"print gui/{os.getuid()}/{LABEL}"


def test_uninstall_without_an_agent_is_a_no_op(sweep):
    r = sweep("uninstall")
    assert r.returncode == 0, r.stderr
    assert not sweep.plist.exists() and not sweep.loaded()
