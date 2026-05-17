from __future__ import annotations

from datetime import UTC, datetime

from agent.observer.docker_socket import decode_docker_log_bytes
from agent.observer.observer import observe_once


NOW = datetime(2026, 5, 16, tzinfo=UTC)


class FakeDockerClient:
    def list_labeled_containers(self, label: str | list[str] = "sre.demo=true"):
        return [{"Id": "app-id"}]

    def inspect_container(self, container_id: str):
        return {
            "Name": "/demo-app",
            "RestartCount": 4,
            "Config": {"Image": "demo-app", "Labels": {"sre.demo.role": "app"}},
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
        return "crash_on_start flag is enabled"


class FakeHealthClient:
    def get(self, url: str, timeout_s: float = 2.0):
        return type(
            "HealthResult",
            (),
            {"ok": False, "status_code": 504, "body": "timeout", "error": "gateway timeout"},
        )()


def test_observe_once_collects_container_log_and_health():
    observed = observe_once(
        NOW,
        FakeDockerClient(),
        FakeHealthClient(),
        health_url="http://nginx/health",
    )

    assert [(observation.source, observation.kind) for observation in observed.observations] == [
        ("app", "container"),
        ("app", "log"),
        ("nginx", "health"),
    ]
    assert observed.observations[0].payload["restart_count"] == 4


def test_decode_docker_multiplexed_logs():
    payload = b"\x01\x00\x00\x00\x00\x00\x00\x06hello\n\x02\x00\x00\x00\x00\x00\x00\x06error\n"

    assert decode_docker_log_bytes(payload) == "hello\nerror\n"


# ---------------------------------------------------------------------------
# Resilience tests — WEAKNESS 2: observer survives non-404 Docker API errors
# ---------------------------------------------------------------------------


class FlakyDockerClient:
    """
    Returns one healthy container and one container that 500s on inspect.
    After the fix, observe_once should still collect observations for
    the healthy container and skip the flaky one gracefully.
    """

    def __init__(self):
        self.inspect_calls = []

    def list_labeled_containers(self, label=None):
        return [{"Id": "healthy-01"}, {"Id": "flaky-01"}]

    def inspect_container(self, container_id):
        self.inspect_calls.append(container_id)
        if container_id == "flaky-01":
            from agent.observer.docker_socket import DockerAPIError

            raise DockerAPIError(
                "GET", f"/containers/{container_id}/json", 500, "daemon overloaded"
            )
        return {
            "Name": f"/{container_id}",
            "RestartCount": 0,
            "Config": {"Image": "test:latest", "Labels": {"sre.demo.role": "app"}},
            "State": {
                "Status": "running",
                "Running": True,
                "Restarting": False,
                "ExitCode": 0,
                "Error": "",
                "StartedAt": "",
                "FinishedAt": "",
            },
        }

    def container_logs(self, container_id, tail=100):
        return ""


def test_observe_once_survives_docker_500_on_inspect():
    now = datetime(2026, 5, 17, tzinfo=UTC)

    class FakeHealth:
        def get(self, url, timeout_s=2.0):
            return type("R", (), {"ok": True, "status_code": 200, "body": "ok", "error": None})()

    bundle = observe_once(
        now,
        docker_client=FlakyDockerClient(),
        health_client=FakeHealth(),
        health_url="http://nginx/health",
    )
    # Must still have observations for the healthy container + the health check.
    # healthy-01 has label sre.demo.role=app so observer resolves source → "app".
    sources = [obs.source for obs in bundle.observations]
    assert "app" in sources  # healthy-01 was processed under its role label
    assert "nginx" in sources  # health check still ran
    # Confirm flaky-01 did NOT produce an observation (only 1 container + health)
    container_obs = [obs for obs in bundle.observations if obs.kind == "container"]
    assert len(container_obs) == 1


def test_observe_once_survives_docker_500_on_logs():
    now = datetime(2026, 5, 17, tzinfo=UTC)

    class FlakyLogClient:
        def list_labeled_containers(self, label=None):
            return [{"Id": "c1"}]

        def inspect_container(self, container_id):
            return {
                "Name": f"/{container_id}",
                "RestartCount": 0,
                "Config": {"Image": "test:latest", "Labels": {"sre.demo.role": "nginx"}},
                "State": {
                    "Status": "running",
                    "Running": True,
                    "Restarting": False,
                    "ExitCode": 0,
                    "Error": "",
                    "StartedAt": "",
                    "FinishedAt": "",
                },
            }

        def container_logs(self, container_id, tail=100):
            from agent.observer.docker_socket import DockerAPIError

            raise DockerAPIError("GET", "/containers/c1/logs", 500, "log stream broken")

    class FakeHealth:
        def get(self, url, timeout_s=2.0):
            return type("R", (), {"ok": True, "status_code": 200, "body": "ok", "error": None})()

    bundle = observe_once(
        now,
        docker_client=FlakyLogClient(),
        health_client=FakeHealth(),
        health_url="http://nginx/health",
    )
    sources = [obs.source for obs in bundle.observations]
    assert "nginx" in sources  # health check ran
    # container observation present; log observation absent (gracefully skipped)
    kinds = []
    for obs in bundle.observations:
        if obs.source in {"nginx", "c1"}:
            kinds.append(obs.kind)
    assert "container" in kinds
    assert "log" not in kinds  # log was skipped
