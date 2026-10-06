import re

import pytest

EXPECTED_SECRETS = {
    "LANGFUSE_INIT_PROJECT_PUBLIC_KEY",
    "LANGFUSE_INIT_PROJECT_SECRET_KEY",
    "LANGFUSE_INIT_USER_PASSWORD",
    "POSTGRES_PASSWORD",
    "CLICKHOUSE_PASSWORD",
    "REDIS_AUTH",
    "MINIO_ROOT_PASSWORD",
    "ENCRYPTION_KEY",
    "SALT",
    "NEXTAUTH_SECRET",
}


def stored(lf):
    return {p.name: p.read_text().strip() for p in lf.keychain.iterdir()}


@pytest.mark.parametrize("args", [(), ("bogus",)])
def test_unknown_or_missing_subcommand_exits_2_with_usage(lf, args):
    r = lf(*args)
    assert r.returncode == 2
    assert "usage:" in r.stderr and "up|down|status" in r.stderr
    assert lf.keychain.exists() and not stored(lf)


def test_up_generates_and_stores_every_secret(lf):
    r = lf("up")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "up"
    values = stored(lf)
    assert len(values) == len(EXPECTED_SECRETS)
    assert all(v for v in values.values())
    assert values["ai-toolkit-langfuse-LANGFUSE_INIT_PROJECT_PUBLIC_KEY"].startswith("pk-lf-")
    assert values["ai-toolkit-langfuse-LANGFUSE_INIT_PROJECT_SECRET_KEY"].startswith("sk-lf-")
    assert re.fullmatch(r"[0-9a-f]{64}", values["ai-toolkit-langfuse-ENCRYPTION_KEY"])


def test_secrets_reach_docker_through_the_environment_only(lf):
    r = lf("up")
    assert r.returncode == 0, r.stderr
    values = stored(lf)
    env = lf.docker_env()
    for name in EXPECTED_SECRETS:
        assert env[name] == values[f"ai-toolkit-langfuse-{name}"]
    log = lf.docker_log()
    for secret in values.values():
        assert secret not in log, "a secret leaked into docker argv"
        assert secret not in r.stdout + r.stderr, "a secret leaked into the output"


def test_second_up_reuses_the_keychain_secrets(lf):
    lf("up")
    first = stored(lf)
    r = lf("up")
    assert r.returncode == 0, r.stderr
    assert stored(lf) == first
    assert lf.docker_env()["POSTGRES_PASSWORD"] == first["ai-toolkit-langfuse-POSTGRES_PASSWORD"]


def test_up_refuses_when_volumes_exist_but_the_keychain_is_empty(lf):
    r = lf("up", FAKE_VOLUMES="ai-toolkit-langfuse_langfuse_postgres_data")
    assert r.returncode != 0
    assert "Keychain" in r.stderr
    assert not stored(lf)
    assert "up -d" not in lf.docker_log()


def test_up_refuses_when_docker_is_unreachable_and_keychain_is_empty(lf):
    r = lf("up", FAKE_DOCKER_DOWN="1")
    assert r.returncode != 0
    assert "not reachable" in r.stderr
    assert not stored(lf), "must not generate secrets it cannot check against the volumes"


@pytest.mark.parametrize(
    ("mem", "warns"),
    [(8 * 1024**3, True), (14 * 1024**3, True), (16745533440, False), (32 * 1024**3, False)],
)
def test_up_warns_below_16_gib_but_does_not_fail(lf, mem, warns):
    r = lf("up", FAKE_DOCKER_MEM=str(mem))
    assert r.returncode == 0, r.stderr
    assert ("16 GB" in r.stderr) is warns


def test_down_keeps_the_volumes(lf):
    lf("up")
    r = lf("down")
    assert r.returncode == 0, r.stderr
    down = [line for line in lf.docker_log().splitlines() if " down" in line]
    assert down
    assert not any(re.search(r"(^|\s)(-v|--volumes)(\s|$)", line) for line in down)


def test_down_works_without_keychain_secrets(lf):
    r = lf("down")
    assert r.returncode == 0, r.stderr
    assert not stored(lf), "down must not create secrets"


def test_status_up_and_down(lf):
    up = lf("status")
    assert (up.returncode, up.stdout.strip()) == (0, "up")
    down = lf("status", FAKE_CURL_FAIL="1")
    assert (down.returncode, down.stdout.strip()) == (1, "down")
