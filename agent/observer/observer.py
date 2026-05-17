from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from agent.observer.docker_socket import DockerAPIError

_logger = logging.getLogger("agent.observer")


class DockerClient(Protocol):
    def list_labeled_containers(
        self,
        label: str | list[str] = "sre.demo=true",
    ) -> list[dict[str, Any]]: ...

    def inspect_container(self, container_id: str) -> dict[str, Any]: ...

    def container_logs(self, container_id: str, tail: int = 100) -> str: ...


class HealthClient(Protocol):
    def get(self, url: str, timeout_s: float = 2.0) -> Any: ...


@dataclass(frozen=True)
class Observation:
    ts: datetime
    source: str
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ObservationsBundle:
    ts: datetime
    observations: list[Observation]

    def by_source_kind(self, source: str, kind: str) -> list[Observation]:
        return [
            observation
            for observation in self.observations
            if observation.source == source and observation.kind == kind
        ]


def observe_once(
    now: datetime,
    docker_client: DockerClient,
    health_client: HealthClient,
    *,
    health_url: str,
    label: str | list[str] = "sre.demo=true",
    log_tail: int = 100,
    http_timeout_s: float = 2.0,
) -> ObservationsBundle:
    observations: list[Observation] = []
    containers = docker_client.list_labeled_containers(label)
    total_containers = len(containers)
    container_observations_count = 0

    for container in containers:
        container_id = container["Id"]
        try:
            inspect = docker_client.inspect_container(container_id)
        except DockerAPIError as exc:
            if exc.status == 404:
                continue
            _logger.warning(
                "skipping container %s due to Docker API error: %s",
                container_id,
                exc,
            )
            continue
        except Exception:
            _logger.exception(
                "unexpected error inspecting container %s; skipping",
                container_id,
            )
            continue
        labels = inspect.get("Config", {}).get("Labels") or {}
        source = labels.get("sre.demo.role") or container_name(inspect)
        state = inspect.get("State", {})
        observations.append(
            Observation(
                ts=now,
                source=source,
                kind="container",
                payload={
                    "id": container_id,
                    "name": container_name(inspect),
                    "image": inspect.get("Config", {}).get("Image"),
                    "labels": labels,
                    "restart_count": inspect.get("RestartCount", 0),
                    "state": {
                        "status": state.get("Status"),
                        "running": state.get("Running"),
                        "restarting": state.get("Restarting"),
                        "exit_code": state.get("ExitCode"),
                        "error": state.get("Error"),
                        "started_at": state.get("StartedAt"),
                        "finished_at": state.get("FinishedAt"),
                    },
                },
            )
        )
        container_observations_count += 1

        if source in {"nginx", "app"}:
            try:
                log_text = docker_client.container_logs(container_id, log_tail)
            except DockerAPIError as exc:
                if exc.status == 404:
                    continue
                _logger.warning(
                    "skipping logs for container %s due to Docker API error: %s",
                    container_id,
                    exc,
                )
                continue
            except Exception:
                _logger.exception(
                    "unexpected error reading logs for container %s; skipping",
                    container_id,
                )
                continue
            observations.append(
                Observation(
                    ts=now,
                    source=source,
                    kind="log",
                    payload={"tail": log_tail, "text": log_text},
                )
            )

    skipped = total_containers - container_observations_count
    if skipped:
        _logger.warning(
            "observation pass skipped %d/%d containers due to API errors",
            skipped,
            total_containers,
        )

    health = health_client.get(health_url, timeout_s=http_timeout_s)
    observations.append(
        Observation(
            ts=now,
            source="nginx",
            kind="health",
            payload={
                "url": health_url,
                "ok": health.ok,
                "status_code": health.status_code,
                "body": health.body,
                "error": health.error,
            },
        )
    )
    return ObservationsBundle(ts=now, observations=observations)


def container_name(inspect: dict[str, Any]) -> str:
    name = inspect.get("Name") or ""
    return name.removeprefix("/")
