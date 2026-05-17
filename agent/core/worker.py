from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import Sequence
from threading import Event

from agent.core.logs import setup_logging
from agent.observer.docker_socket import DockerSocketClient, DockerTCPClient, parse_tcp_docker_host, build_docker_client
from agent.observer.health import UrllibHealthClient
from agent.observer.observer import DockerClient, HealthClient
from agent.storage.sqlite_store import SQLiteStore
from agent.pipeline.orchestrator import RemediationOrchestrator
from agent.planner.registry import PlannerRegistry
from agent.planner.strategies.nginx import NginxConfigErrorPlanner
from agent.planner.strategies.app_crash import AppCrashLoopPlanner
from agent.planner.strategies.retry_aware import RetryAwarePlanner

logger = logging.getLogger("agent.worker")

DEFAULT_HEALTH_URL = "http://nginx/health"
DEFAULT_DOCKER_SOCKET_PATH = "/var/run/docker.sock"
DEFAULT_POLL_INTERVAL_S = 2.0
DEFAULT_SANDBOX_PROJECT = "docker-incident-controller"


def default_health_url() -> str:
    return os.environ.get("HEALTH_URL", DEFAULT_HEALTH_URL)

def default_poll_interval_s() -> float:
    return float(os.environ.get("AGENT_POLL_INTERVAL_S", DEFAULT_POLL_INTERVAL_S))

def default_label_filters() -> list[str]:
    project = os.environ.get("SANDBOX_PROJECT", DEFAULT_SANDBOX_PROJECT)
    return ["sre.demo=true", f"com.docker.compose.project={project}"]

def build_orchestrator(
    store: SQLiteStore,
    docker_client: DockerClient | None = None,
    health_client: HealthClient | None = None,
    health_url: str | None = None,
) -> RemediationOrchestrator:
    registry = PlannerRegistry()
    registry.register(RetryAwarePlanner(NginxConfigErrorPlanner()))
    registry.register(RetryAwarePlanner(AppCrashLoopPlanner()))

    return RemediationOrchestrator(
        observer_docker_client=docker_client or build_docker_client(),
        observer_health_client=health_client or UrllibHealthClient(),
        store=store,
        planner_registry=registry,
        health_url=health_url or default_health_url(),
        label_filters=default_label_filters(),
    )

def recover_startup_state(store: SQLiteStore) -> None:
    recovered = store.mark_in_progress_needs_human()
    if recovered:
        logger.error(
            "Recovered %s incident(s) left IN_PROGRESS; marked NEEDS_HUMAN",
            recovered,
        )

def run_once(
    store: SQLiteStore,
    docker_client: DockerClient | None = None,
    health_client: HealthClient | None = None,
    *,
    health_url: str | None = None,
) -> dict[str, object]:
    store.initialize()
    recover_startup_state(store)
    
    orchestrator = build_orchestrator(store, docker_client, health_client, health_url)
    incidents = orchestrator.run_pass()
    
    return {
        "created_incidents": [
            {
                "id": incident.id,
                "type": incident.type,
                "status": incident.status.value,
                "summary": incident.summary,
            }
            for incident in incidents
        ],
        "created_count": len(incidents),
    }

def run_polling_loop(
    store: SQLiteStore,
    *,
    stop_event: Event | None = None,
    docker_client: DockerClient | None = None,
    health_client: HealthClient | None = None,
    poll_interval_s: float | None = None,
    health_url: str | None = None,
) -> None:
    store.initialize()
    recover_startup_state(store)
    
    orchestrator = build_orchestrator(store, docker_client, health_client, health_url)
    stop_event = stop_event or Event()
    poll_interval_s = poll_interval_s or default_poll_interval_s()
    
    orchestrator.run_polling_loop(stop_event, poll_interval_s)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Docker Incident Controller worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one real observe/detect pass and exit.",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run the Phase C polling worker loop.",
    )
    return parser

def main(argv: Sequence[str] | None = None) -> int:
    setup_logging()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.once == args.loop:
        parser.error("choose exactly one of --once or --loop")

    if args.once:
        result = run_once(SQLiteStore.from_env())
        print(json.dumps(result, sort_keys=True))
        return 0

    run_polling_loop(SQLiteStore.from_env())
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
