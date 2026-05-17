from __future__ import annotations

import logging
from datetime import UTC, datetime
from threading import Event

from agent.detector.rules import detect as detect_candidates
from agent.executor.runner import execute_plan
from agent.models.incident import IncidentRecord, IncidentStatus
from agent.models.plan import Plan
from agent.models.state_machine import IncidentStateMachine
from agent.observer.observer import DockerClient, HealthClient, observe_once
from agent.planner.registry import PlannerRegistry
from agent.storage.sqlite_store import OptimisticLockError, SQLiteStore
from agent.core.metrics import INCIDENT_CREATED_COUNT, INCIDENT_RETRY_COUNT, WORKER_PASS_COUNT

logger = logging.getLogger("agent.orchestrator")

MAX_ATTEMPTS = 3

class RemediationOrchestrator:
    def __init__(
        self,
        observer_docker_client: DockerClient,
        observer_health_client: HealthClient,
        store: SQLiteStore,
        planner_registry: PlannerRegistry,
        health_url: str,
        label_filters: list[str],
    ):
        self.docker_client = observer_docker_client
        self.health_client = observer_health_client
        self.store = store
        self.planner_registry = planner_registry
        self.health_url = health_url
        self.label_filters = label_filters

    def run_pass(self) -> list[IncidentRecord]:
        # 1. Observe
        bundle = observe_once(
            datetime.now(UTC),
            self.docker_client,
            self.health_client,
            health_url=self.health_url,
            label=self.label_filters,
        )

        # 2. Detect
        candidates = detect_candidates(bundle)

        # 3. Persist atomically
        with self.store.transaction() as conn:
            observation_count = self.store.record_observations(bundle.observations, conn=conn)
            created = self.store.create_incidents_from_candidates(candidates, conn=conn)

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

    def process_ready_incidents(self) -> list[IncidentRecord]:
        processed: list[IncidentRecord] = []
        for incident in self.store.list_incidents_by_statuses(IncidentStateMachine.actionable_statuses()):
            if incident.status == IncidentStatus.FAILED:
                retried = self.retry_failed_incident(incident)
                if retried is None:
                    continue
                incident = retried

            if incident.status == IncidentStatus.OPEN:
                planned = self.plan_open_incident(incident)
                if planned is None:
                    continue
                incident = planned

            if incident.status == IncidentStatus.PLANNED:
                result = self.execute_planned_incident(incident)
                processed.append(result)
        return processed

    def retry_failed_incident(self, incident: IncidentRecord) -> IncidentRecord | None:
        if incident.attempt_count >= MAX_ATTEMPTS:
            needs_human = self.store.transition_incident(
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

        # Exponential backoff: 2s, 4s, 8s based on attempt count
        backoff_seconds = 2 ** (incident.attempt_count + 1)
        if (datetime.now(UTC) - incident.updated_at).total_seconds() < backoff_seconds:
            return None

        try:
            retried = self.store.transition_incident(
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
            retried.attempt_count + 1,
            MAX_ATTEMPTS,
        )
        INCIDENT_RETRY_COUNT.labels(type=retried.type).inc()
        return retried

    def plan_open_incident(self, incident: IncidentRecord) -> IncidentRecord | None:
        if incident.attempt_count >= MAX_ATTEMPTS:
            needs_human = self.store.transition_incident(
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
            plan = self.planner_registry.plan_for(incident)
        except Exception as exc:
            needs_human = self.store.transition_incident(
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

        self.store.create_plan(plan)
        try:
            planned = self.store.transition_incident(
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

    def execute_planned_incident(self, incident: IncidentRecord) -> IncidentRecord:
        plan_record = self.store.latest_plan_for_incident(incident.id)
        if plan_record is None:
            needs_human = self.store.transition_incident(
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
        return execute_plan(self.store, incident, plan_record.id, plan)

    def run_polling_loop(self, stop_event: Event, poll_interval_s: float) -> None:
        logger.info(
            "Starting polling worker health_url=%s poll_interval_s=%s",
            self.health_url,
            poll_interval_s,
        )
        while not stop_event.is_set():
            try:
                self.run_pass()
                self.process_ready_incidents()
                WORKER_PASS_COUNT.labels(status="success").inc()
            except Exception:
                logger.exception("Worker pass failed")
                WORKER_PASS_COUNT.labels(status="failed").inc()
            stop_event.wait(poll_interval_s)
