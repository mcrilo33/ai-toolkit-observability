import re
import shutil
import subprocess

import pytest
import yaml

from conftest import REPO

COMPOSE = REPO / "compose.yaml"
SERVICES = {"langfuse-web", "langfuse-worker", "postgres", "clickhouse", "redis", "minio"}
SECRETS = {
    "POSTGRES_PASSWORD",
    "CLICKHOUSE_PASSWORD",
    "REDIS_AUTH",
    "MINIO_ROOT_PASSWORD",
    "ENCRYPTION_KEY",
    "SALT",
    "NEXTAUTH_SECRET",
    "LANGFUSE_INIT_PROJECT_PUBLIC_KEY",
    "LANGFUSE_INIT_PROJECT_SECRET_KEY",
    "LANGFUSE_INIT_USER_PASSWORD",
}


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE.read_text())


def test_has_the_six_mandatory_services(compose):
    assert set(compose["services"]) == SERVICES


def test_only_the_web_ui_is_published_on_loopback(compose):
    published = {
        name: svc["ports"] for name, svc in compose["services"].items() if "ports" in svc
    }
    assert published == {"langfuse-web": ["127.0.0.1:3000:3000"]}


def test_no_service_uses_host_networking_or_exposes_ports(compose):
    for name, svc in compose["services"].items():
        assert svc.get("network_mode") != "host", name
        assert "expose" not in svc, name


def test_every_image_is_pinned(compose):
    for name, svc in compose["services"].items():
        image = svc["image"]
        pinned = "@sha256:" in image or re.search(r":\d+\.\d+", image)
        assert pinned, f"{name}: {image} is not pinned to a major.minor(.patch) tag or a digest"
        assert not image.endswith(":latest"), name


def test_langfuse_is_v4(compose):
    for name in ("langfuse-web", "langfuse-worker"):
        assert re.search(r":4\.\d+\.\d+$", compose["services"][name]["image"])


def test_state_lives_in_named_volumes(compose):
    declared = set(compose["volumes"])
    mounts = [
        v.split(":")[0]
        for svc in compose["services"].values()
        for v in svc.get("volumes", [])
    ]
    assert mounts, "no volume mounted"
    assert set(mounts) <= declared, "bind mount or undeclared volume"
    for stateful in ("postgres", "clickhouse", "redis", "minio"):
        assert compose["services"][stateful].get("volumes"), stateful


def test_secrets_have_no_default_and_are_required():
    text = COMPOSE.read_text()
    for name in SECRETS:
        refs = re.findall(r"\$\{" + name + r"([^}]*)\}", text)
        assert refs, f"{name} is not referenced"
        assert all(r.startswith(":?") for r in refs), f"{name} must be ${{{name}:?}}"


def test_no_secret_reaches_a_command_line(compose):
    for name, svc in compose["services"].items():
        argv = " ".join(
            str(part)
            for key in ("command", "entrypoint")
            for part in [svc.get(key, "")]
        ) + " ".join(map(str, svc.get("healthcheck", {}).get("test", [])))
        for secret in SECRETS:
            assert f"${{{secret}" not in argv, f"{name} passes {secret} in argv"


def test_redis_password_is_not_a_server_argument(compose):
    command = " ".join(compose["services"]["redis"]["command"])
    assert "--requirepass" not in command
    assert "requirepass $$REDIS_AUTH" in command


def test_headless_init_creates_the_ai_toolkit_project(compose):
    env = compose["services"]["langfuse-web"]["environment"]
    assert env["LANGFUSE_INIT_PROJECT_ID"] == "ai-toolkit"
    assert env["LANGFUSE_INIT_PROJECT_NAME"] == "ai-toolkit"


@pytest.mark.parametrize("service", ["langfuse-web", "langfuse-worker"])
def test_langfuse_runs_single_node_without_telemetry(compose, service):
    env = compose["services"][service]["environment"]
    assert str(env["TELEMETRY_ENABLED"]).lower() == "false"
    assert str(env["CLICKHOUSE_CLUSTER_ENABLED"]).lower() == "false", "needs ZooKeeper otherwise"


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_compose_config_is_valid():
    env = {
        "PATH": __import__("os").environ["PATH"],
        **{name: "x" for name in SECRETS},
    }
    result = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config", "--quiet"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
