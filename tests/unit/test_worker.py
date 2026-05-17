from __future__ import annotations

from agent.core.worker import run_once
from agent.storage.sqlite_store import SQLiteStore


class FakeDockerClient:
    def __init__(self, logs: str = ""):
        self.logs = logs

    def list_labeled_containers(self, label: str | list[str] = "sre.demo=true"):
        return [{"Id": "nginx-id"}]

    def inspect_container(self, container_id: str):
        return {
            "Name": "/docker-incident-controller-nginx-1",
            "RestartCount": 1,
            "Config": {"Image": "nginx:1.27-alpine", "Labels": {"sre.demo.role": "nginx"}},
            "State": {
                "Status": "restarting",
                "Running": False,
                "Restarting": True,
                "ExitCode": 1,
                "Error": "",
                "StartedAt": "2026-05-16T00:00:00Z",
                "FinishedAt": "2026-05-16T00:00:01Z",
            },
        }

    def container_logs(self, container_id: str, tail: int = 100):
        return self.logs


class FakeHealthClient:
    def get(self, url: str, timeout_s: float = 2.0):
        return type(
            "HealthResult",
            (),
            {"ok": False, "status_code": None, "body": None, "error": "connection refused"},
        )()


def test_run_once_creates_real_detected_incident(tmp_path):
    store = SQLiteStore(tmp_path / "incidents.sqlite3")

    result = run_once(
        store,
        docker_client=FakeDockerClient("nginx: [emerg] unknown directive"),
        health_client=FakeHealthClient(),
        health_url="http://nginx/health",
    )

    incidents = store.list_incidents()
    assert result["created_count"] == 1
    assert len(incidents) == 1
    assert incidents[0].type == "NGINX_CONFIG_ERROR"


def test_run_once_dedupes_active_incidents(tmp_path):
    store = SQLiteStore(tmp_path / "incidents.sqlite3")
    docker_client = FakeDockerClient("nginx: [emerg] unknown directive")
    health_client = FakeHealthClient()

    first = run_once(
        store,
        docker_client=docker_client,
        health_client=health_client,
        health_url="http://nginx/health",
    )
    second = run_once(
        store,
        docker_client=docker_client,
        health_client=health_client,
        health_url="http://nginx/health",
    )

    assert first["created_count"] == 1
    assert second["created_count"] == 0
    assert len(store.list_incidents()) == 1
