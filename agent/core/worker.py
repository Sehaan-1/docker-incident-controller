from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from threading import Event

from agent.executor.runner import execute_plan
from agent.models.incident import IncidentRecord
from agent.models.incident import IncidentStatus
from agent.models.plan import Plan
from agent.models.state_machine import IncidentStateMachine
from agent.observer.docker_socket import DockerSocketClient, DockerTCPClient, parse_tcp_docker_host
from agent.observer.health import UrllibHealthClient
from agent.observer.observer import DockerClient, HealthClient, observe_once
from agent.planner.rules import plan_for_incident
from agent.storage.sqlite_store import OptimisticLockError
from agent.storage.sqlite_store import SQLiteStore
from agent.core.logs import setup_logging
from agent.core.metrics import INCIDENT_CREATED_COUNT, INCIDENT_RETRY_COUNT, WORKER_PASS_COUNT

logger = logging.getLogger("agent.worker")


DEFAULT_HEALTH_URL = "http://nginx/health"
DEFAULT_DOCKER_SOCKET_PATH = "/var/run/docker.sock"
DEFAULT_POLL_INTERVAL_S = 2.0
DEFAULT_SANDBOX_PROJECT = "docker-incident-controller"
MAX_ATTEMPTS = 3


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
    label_filters: list[str] | None = None,
) -> dict[str, object]:
    store.initialize()
    recover_startup_state(store)
    incidents = observe_detect_and_persist(
        store,
        docker_client or default_docker_client(),
        health_client or UrllibHealthClient(),
        health_url=health_url or default_health_url(),
        label_filters=label_filters or default_label_filters(),
    )
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


def process_ready_incidents(store: SQLiteStore) -> list[IncidentRecord]:
    processed: list[IncidentRecord] = []
    for incident in store.list_incidents_by_statuses(IncidentStateMachine.actionable_statuses()):
        # Retry-eligible: FAILED incidents are re-queued as OPEN before the
        # rest of the loop handles them as normal OPEN incidents.
        if incident.status == IncidentStatus.FAILED:
            retried = retry_failed_incident(store, incident)
            if retried is None:
                continue
            incident = retried

        if incident.status == IncidentStatus.OPEN:
            planned = plan_open_incident(store, incident)
            if planned is None:
                continue
            incident = planned

        if incident.status == IncidentStatus.PLANNED:
            result = execute_planned_incident(store, incident)
            processed.append(result)
    return processed


def retry_failed_incident(store: SQLiteStore, incident: IncidentRecord) -> IncidentRecord | None:
    """Transition a FAILED incident back to OPEN for another attempt.

    Returns the refreshed OPEN incident, or ``None`` if the attempt budget is
    exhausted (escalated to NEEDS_HUMAN) or the optimistic lock was lost.
    """
    if incident.attempt_count >= MAX_ATTEMPTS:
        needs_human = store.transition_incident(
            incident.id,
            from_status=IncidentStatus.FAILED,
            to_status=IncidentStatus.NEEDS_HUMAN,
            expected_version=incident.version,
            last_error_json={
                "failure_reason": "max_attempts_exhausted",
                "max_attempts": MAX_ATTEMPTS,
                "attempt_count": incident.attempt_count,
            },
        )
        logger.error(
            "INCIDENT_NEEDS_HUMAN id=%s type=%s reason=max_attempts_exhausted attempts=%s",
            needs_human.id,
            needs_human.type,
            incident.attempt_count,
        )
        return None

    try:
        retried = store.transition_incident(
            incident.id,
            from_status=IncidentStatus.FAILED,
            to_status=IncidentStatus.OPEN,
            expected_version=incident.version,
        )
    except OptimisticLockError:
        logger.info("Incident %s changed before retry transition; skipping", incident.id)
        return None

    logger.warning(
        "INCIDENT_RETRY id=%s type=%s attempt=%s/%s",
        retried.id,
        retried.type,
        retried.attempt_count + 1,  # next attempt will be attempt_count + 1
        MAX_ATTEMPTS,
    )
    INCIDENT_RETRY_COUNT.labels(type=retried.type).inc()
    return retried


def plan_open_incident(store: SQLiteStore, incident: IncidentRecord) -> IncidentRecord | None:
    if incident.attempt_count >= MAX_ATTEMPTS:
        needs_human = store.transition_incident(
            incident.id,
            from_status=IncidentStatus.OPEN,
            to_status=IncidentStatus.NEEDS_HUMAN,
            expected_version=incident.version,
            last_error_json={
                "failure_reason": "max_attempts_exhausted",
                "max_attempts": MAX_ATTEMPTS,
            },
        )
        logger.error(
            "INCIDENT_NEEDS_HUMAN id=%s type=%s reason=max_attempts_exhausted",
            needs_human.id,
            needs_human.type,
        )
        return None

    try:
        plan = plan_for_incident(incident)
    except Exception as exc:
        needs_human = store.transition_incident(
            incident.id,
            from_status=IncidentStatus.OPEN,
            to_status=IncidentStatus.NEEDS_HUMAN,
            expected_version=incident.version,
            last_error_json={
                "failure_reason": "planning_failed",
                "exception_type": exc.__class__.__name__,
                "message": str(exc),
            },
        )
        logger.error(
            "INCIDENT_NEEDS_HUMAN id=%s type=%s reason=planning_failed",
            needs_human.id,
            needs_human.type,
        )
        return None

    store.create_plan(plan)
    try:
        planned = store.transition_incident(
            incident.id,
            from_status=IncidentStatus.OPEN,
            to_status=IncidentStatus.PLANNED,
            expected_version=incident.version,
        )
    except OptimisticLockError:
        logger.info("Incident %s changed before planning transition; skipping", incident.id)
        return None
    logger.info("Planned incident %s type=%s", planned.id, planned.type)
    return planned


def execute_planned_incident(store: SQLiteStore, incident: IncidentRecord) -> IncidentRecord:
    plan_record = store.latest_plan_for_incident(incident.id)
    if plan_record is None:
        needs_human = store.transition_incident(
            incident.id,
            from_status=IncidentStatus.PLANNED,
            to_status=IncidentStatus.NEEDS_HUMAN,
            expected_version=incident.version,
            last_error_json={"failure_reason": "missing_plan"},
        )
        logger.error(
            "INCIDENT_NEEDS_HUMAN id=%s type=%s reason=missing_plan",
            needs_human.id,
            needs_human.type,
        )
        return needs_human
    plan = Plan.model_validate(plan_record.plan_json)
    return execute_plan(store, incident, plan_record.id, plan)


def observe_detect_and_persist(
    store: SQLiteStore,
    docker_client: DockerClient,
    health_client: HealthClient,
    *,
    health_url: str,
    label_filters: list[str],
) -> list[IncidentRecord]:
    """Observe, detect, and persist in a single atomic SQLite transaction.

    Calling ``store.observe_and_persist_atomic`` runs the three steps inside
    one ``BEGIN … COMMIT`` block, so a mid-pass crash can never produce a state
    where observations are written but the corresponding incidents are not (or
    vice-versa).
    """
    bundle = observe_once(
        datetime.now(UTC),
        docker_client,
        health_client,
        health_url=health_url,
        label=label_filters,
    )
    observation_count, created = store.observe_and_persist_atomic(bundle)
    for incident in created:
        logger.warning(
            "Created incident %s type=%s: %s", incident.id, incident.type, incident.summary
        )
        INCIDENT_CREATED_COUNT.labels(type=incident.type).inc()
    logger.info(
        "Observation pass recorded %s observation(s), %s new incident(s)",
        observation_count,
        len(created),
    )
    return created


def run_polling_loop(
    store: SQLiteStore,
    *,
    stop_event: Event | None = None,
    docker_client: DockerClient | None = None,
    health_client: HealthClient | None = None,
    poll_interval_s: float | None = None,
    health_url: str | None = None,
    label_filters: list[str] | None = None,
) -> None:
    store.initialize()
    recover_startup_state(store)
    docker_client = docker_client or default_docker_client()
    health_client = health_client or UrllibHealthClient()
    poll_interval_s = poll_interval_s or default_poll_interval_s()
    health_url = health_url or default_health_url()
    label_filters = label_filters or default_label_filters()
    stop_event = stop_event or Event()

    logger.info(
        "Starting polling worker health_url=%s poll_interval_s=%s",
        health_url,
        poll_interval_s,
    )
    while not stop_event.is_set():
        try:
            observe_detect_and_persist(
                store,
                docker_client,
                health_client,
                health_url=health_url,
                label_filters=label_filters,
            )
            process_ready_incidents(store)
            WORKER_PASS_COUNT.labels(status="success").inc()
        except Exception:
            logger.exception("Worker pass failed")
            WORKER_PASS_COUNT.labels(status="failed").inc()
        stop_event.wait(poll_interval_s)


def default_docker_client() -> DockerSocketClient:
    docker_host = os.environ.get("DOCKER_HOST", "").strip()
    if docker_host.startswith("tcp://"):
        host, port = parse_tcp_docker_host(docker_host)
        return DockerTCPClient(host=host, port=port)
    return DockerSocketClient(os.environ.get("DOCKER_SOCKET_PATH", DEFAULT_DOCKER_SOCKET_PATH))


def default_health_url() -> str:
    return os.environ.get("HEALTH_URL", DEFAULT_HEALTH_URL)


def default_poll_interval_s() -> float:
    return float(os.environ.get("AGENT_POLL_INTERVAL_S", DEFAULT_POLL_INTERVAL_S))


def default_label_filters() -> list[str]:
    project = os.environ.get("SANDBOX_PROJECT", DEFAULT_SANDBOX_PROJECT)
    return ["sre.demo=true", f"com.docker.compose.project={project}"]


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
