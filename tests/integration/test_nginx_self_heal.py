from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import uuid
from http.client import RemoteDisconnected
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest


ROOT = Path(__file__).resolve().parents[2]


def test_nginx_config_error_self_heals_with_action_trail():
    require_docker()
    project = f"dic-itest-{uuid.uuid4().hex[:8]}"
    agent_port = free_port()
    nginx_port = free_port()
    env = os.environ.copy()
    env.update(
        {
            "COMPOSE_PROJECT_NAME": project,
            "AGENT_PORT": str(agent_port),
            "NGINX_PORT": str(nginx_port),
        }
    )
    compose = ["docker", "compose", "-p", project]

    try:
        run(compose + ["down", "--volumes"], env=env, check=False)
        run(compose + ["up", "-d", "--build"], env=env)
        wait_json(f"http://127.0.0.1:{agent_port}/healthz", timeout_s=60)
        wait_json(f"http://127.0.0.1:{nginx_port}/health", timeout_s=60)

        run(
            compose
            + [
                "exec",
                "-T",
                "agent",
                "sh",
                "-c",
                (
                    "printf '%s\n' 'server {' '    listen 80;' "
                    "'    definitely_invalid_directive on;' '}' > /nginx_conf/site.conf"
                ),
            ],
            env=env,
        )
        run(compose + ["restart", "nginx"], env=env, check=False)

        incident = wait_incident_terminal(
            f"http://127.0.0.1:{agent_port}",
            incident_type="NGINX_CONFIG_ERROR",
            timeout_s=90,
        )
        actions = get_json(f"http://127.0.0.1:{agent_port}/actions?incident_id={incident['id']}")
        health = get_json(f"http://127.0.0.1:{nginx_port}/health")

        assert incident["status"] == "RESOLVED"
        assert health["status"] == "ok"
        assert [action["tool"] for action in actions] == [
            "render_known_good_nginx_config",
            "nginx_configtest",
            "atomic_replace",
            "restart_container",
            "verify_health_stable",
        ]
        assert all(action["status"] == "SUCCEEDED" for action in actions)
    except Exception:
        dump_debug(compose, env, agent_port)
        raise
    finally:
        run(compose + ["down", "--volumes"], env=env, check=False)


def require_docker() -> None:
    try:
        subprocess.run(
            ["docker", "version"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=True,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        pytest.skip("Docker is not available")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run(
    command: list[str],
    *,
    env: dict[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        timeout=240,
        check=check,
    )


def get_json(url: str) -> object:
    with urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_json(url: str, timeout_s: float) -> object:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return get_json(url)
        except (
            URLError,
            TimeoutError,
            json.JSONDecodeError,
            RemoteDisconnected,
            ConnectionResetError,
            ConnectionError,
        ) as exc:
            last_error = exc
            time.sleep(0.5)
    raise TimeoutError(f"{url} did not become ready: {last_error}")


def wait_incident_terminal(
    base_url: str, *, incident_type: str, timeout_s: float
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            incidents = get_json(f"{base_url}/incidents")
            for incident in incidents:
                if incident["type"] != incident_type:
                    continue
                if incident["status"] in {"RESOLVED", "FAILED", "NEEDS_HUMAN"}:
                    return incident
        except (
            URLError,
            TimeoutError,
            json.JSONDecodeError,
            RemoteDisconnected,
            ConnectionResetError,
            ConnectionError,
        ):
            pass
        time.sleep(1)
    raise TimeoutError(f"{incident_type} did not reach terminal status")


def dump_debug(compose: list[str], env: dict[str, str], agent_port: int) -> None:
    print("\n--- docker compose ps ---")
    print(run(compose + ["ps"], env=env, check=False).stdout)
    print("\n--- docker compose logs ---")
    print(run(compose + ["logs", "--no-color", "--tail=200"], env=env, check=False).stdout)
    try:
        print("\n--- incidents ---")
        print(json.dumps(get_json(f"http://127.0.0.1:{agent_port}/incidents"), indent=2))
        print("\n--- actions ---")
        print(json.dumps(get_json(f"http://127.0.0.1:{agent_port}/actions"), indent=2))
    except Exception as exc:
        print(f"Could not fetch API debug output: {exc}")
