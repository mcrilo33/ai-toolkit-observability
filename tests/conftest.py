import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "langfuse.sh"

SECURITY_STUB = """#!/usr/bin/env bash
# Fake macOS `security`: one file per service in $FAKE_KEYCHAIN.
case "$1" in
  find-generic-password)
    while [ $# -gt 0 ]; do [ "$1" = -s ] && svc="$2"; shift; done
    [ -f "$FAKE_KEYCHAIN/$svc" ] && cat "$FAKE_KEYCHAIN/$svc" || exit 44 ;;
  -i)
    while read -r line; do
      set -- $line
      [ "$1" = add-generic-password ] || exit 1
      while [ $# -gt 0 ]; do
        case "$1" in -s) svc="$2" ;; -w) pw="$2" ;; esac; shift
      done
      printf '%s\\n' "$pw" > "$FAKE_KEYCHAIN/$svc"
    done ;;
  *) echo "unexpected security call: $*" >&2; exit 1 ;;
esac
"""

DOCKER_STUB = """#!/usr/bin/env bash
# Fake docker: records argv, and the environment of every `compose` call.
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  info) echo "${FAKE_DOCKER_MEM:-34359738368}" ;;
  volume) [ -z "${FAKE_DOCKER_DOWN:-}" ] || exit 1
    [ -n "${FAKE_VOLUMES:-}" ] && echo "$FAKE_VOLUMES" ;;
  compose) env > "$FAKE_DOCKER_ENV" ;;
esac
exit 0
"""

CURL_STUB = """#!/usr/bin/env bash
[ -z "${FAKE_CURL_FAIL:-}" ]
"""

SLEEP_STUB = "#!/usr/bin/env bash\nexit 0\n"


@pytest.fixture
def lf(tmp_path):
    """Run scripts/langfuse.sh against stubbed security/docker/curl."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (
        ("security", SECURITY_STUB),
        ("docker", DOCKER_STUB),
        ("curl", CURL_STUB),
        ("sleep", SLEEP_STUB),
    ):
        path = bindir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    keychain = tmp_path / "keychain"
    keychain.mkdir()
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "FAKE_KEYCHAIN": str(keychain),
        "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
        "FAKE_DOCKER_ENV": str(tmp_path / "docker.env"),
    }

    def run(*args, **extra):
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env={**env, **extra},
            capture_output=True,
            text=True,
            timeout=60,
        )

    run.keychain = keychain
    run.docker_log = lambda: (tmp_path / "docker.log").read_text()
    run.docker_env = lambda: dict(
        line.split("=", 1)
        for line in (tmp_path / "docker.env").read_text().splitlines()
        if "=" in line
    )
    return run
