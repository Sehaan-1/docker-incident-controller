from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from agent.observer.docker_socket import DockerSocketClient, DockerTCPClient, parse_tcp_docker_host, build_docker_client
from agent.observer.health import UrllibHealthClient


DEFAULT_NGINX_CONF_DIR = Path("/nginx_conf")
DEFAULT_RUNTIME_DIR = Path("/runtime")
DEFAULT_KNOWN_GOOD_NGINX_CONFIG_PATH = Path("/workspace/services/nginx/site.conf")


class VerificationFailed(RuntimeError):
    pass


def render_known_good_nginx_config(target: str) -> dict[str, Any]:
    target_path = assert_within_dir(target, nginx_conf_dir())
    source_path = Path(
        os.environ.get("KNOWN_GOOD_NGINX_CONFIG_PATH", str(DEFAULT_KNOWN_GOOD_NGINX_CONFIG_PATH))
    )
    content = source_path.read_text(encoding="utf-8")
    target_path.write_text(content, encoding="utf-8")
    return {"target": str(target_path), "bytes_written": len(content.encode("utf-8"))}


def nginx_configtest(config_path: str) -> dict[str, Any]:
    candidate_path = assert_within_dir(config_path, nginx_conf_dir())
    volume_name = os.environ.get("NGINX_CONF_VOLUME")
    network_name = os.environ.get("DOCKER_NETWORK")
    image = os.environ.get("NGINX_CONFIGTEST_IMAGE", "nginx:1.27-alpine")
    if not volume_name:
        raise RuntimeError("NGINX_CONF_VOLUME is required for nginx_configtest")
    if not network_name:
        raise RuntimeError("DOCKER_NETWORK is required for nginx_configtest")

    candidate_name = candidate_path.name
    command = [
        "sh",
        "-c",
        (
            "set -eu; "
            "mkdir -p /tmp/conf.d; "
            f"cp /mnt/nginx_conf/{candidate_name} /tmp/conf.d/site.conf; "
            "printf 'events {}\\nhttp { include /etc/nginx/mime.types; include /tmp/conf.d/*.conf; }\\n' "
            "> /tmp/nginx.conf; "
            "nginx -t -c /tmp/nginx.conf"
        ),
    ]
    docker = build_docker_client()
    container_name = f"dic-nginx-configtest-{uuid.uuid4().hex[:12]}"
    container_id = docker.create_container(
        image=image,
        name=container_name,
        command=command,
        binds=[f"{volume_name}:/mnt/nginx_conf:ro"],
        network_mode=network_name,
    )
    try:
        docker.start_container(container_id)
        exit_code = docker.wait_container(container_id, timeout_s=20)
        logs = docker.container_logs(container_id, tail=100)
        if exit_code != 0:
            raise RuntimeError(f"nginx config test failed with exit_code={exit_code}: {logs}")
        return {"exit_code": exit_code, "logs": logs.strip()}
    finally:
        docker.remove_container(container_id, force=True)


def atomic_replace(src: str, dst: str) -> dict[str, Any]:
    root = nginx_conf_dir()
    src_path = assert_within_dir(src, root)
    dst_path = assert_within_dir(dst, root)
    if not src_path.exists():
        raise FileNotFoundError(str(src_path))
    os.replace(src_path, dst_path)
    return {"src": str(src_path), "dst": str(dst_path)}


def restart_container(name: str) -> dict[str, Any]:
    if name not in {"nginx", "app"}:
        raise ValueError(f"container restart is not allowlisted for: {name}")
    docker = build_docker_client()
    container_id = docker.find_labeled_container_by_role(
        name,
        project=os.environ.get("SANDBOX_PROJECT", "docker-incident-controller"),
    )
    docker.restart_container(container_id, timeout_s=10)
    return {"name": name, "container_id": container_id}


def write_runtime_flags(flags: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {"crash_on_start"}
    unexpected = sorted(set(flags) - allowed_keys)
    if unexpected:
        raise ValueError(f"runtime flag keys are not allowlisted: {unexpected}")

    target = assert_within_dir(runtime_dir() / "flags.json", runtime_dir())
    tmp = target.with_name(f"{target.name}.tmp")
    tmp.write_text(json.dumps(flags, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return {"path": str(target), "flags": flags}


def verify_health_stable(
    url: str,
    stable_window_s: int,
    max_wait_s: int,
    poll_interval_s: float = 1.0,
) -> dict[str, Any]:
    if url not in {"http://nginx/health", os.environ.get("HEALTH_URL")}:
        raise ValueError(f"health verification URL is not allowlisted: {url}")
    client = UrllibHealthClient()
    deadline = time.monotonic() + max_wait_s
    stable_since: float | None = None
    last_result: dict[str, Any] | None = None

    while time.monotonic() < deadline:
        result = client.get(url, timeout_s=min(2.0, poll_interval_s))
        last_result = {
            "ok": result.ok,
            "status_code": result.status_code,
            "body": result.body,
            "error": result.error,
        }
        now = time.monotonic()
        if result.ok:
            stable_since = stable_since or now
            stable_for = now - stable_since
            if stable_for >= stable_window_s:
                return {
                    "url": url,
                    "stable_window_s": stable_window_s,
                    "stable_for_s": round(stable_for, 3),
                    "last_result": last_result,
                }
        else:
            stable_since = None
        time.sleep(poll_interval_s)

    raise VerificationFailed(
        json.dumps(
            {
                "failure_reason": "verification_failed",
                "url": url,
                "stable_window_s": stable_window_s,
                "max_wait_s": max_wait_s,
                "last_result": last_result,
            },
            sort_keys=True,
        )
    )


def noop(**kwargs: Any) -> dict[str, Any]:
    """A no-op tool used for demonstration of dynamic planner steps (e.g., rollback)."""
    return {"status": "noop_executed", "params": kwargs}




def nginx_conf_dir() -> Path:
    return Path(os.environ.get("NGINX_CONF_DIR", str(DEFAULT_NGINX_CONF_DIR))).resolve()


def runtime_dir() -> Path:
    return Path(os.environ.get("RUNTIME_DIR", str(DEFAULT_RUNTIME_DIR))).resolve()


def assert_within_dir(path: str | Path, root: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    root_resolved = root.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise ValueError(f"path {path!r} resolves to {resolved} which is outside {root_resolved}")
    return resolved
