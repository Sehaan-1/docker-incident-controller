from __future__ import annotations

import json
import logging
import traceback
from typing import Any

from agent.models.incident import IncidentRecord, IncidentStatus
from agent.models.plan import Plan
from agent.storage.sqlite_store import OptimisticLockError, SQLiteStore
from agent.tools.allowlist import get_tool
from agent.tools.remediation import VerificationFailed
from agent.core.metrics import INCIDENT_RESOLUTION_COUNT, ACTION_EXECUTED_COUNT

logger = logging.getLogger("agent.executor")


def execute_plan(
    store: SQLiteStore, incident: IncidentRecord, plan_id: int, plan: Plan
) -> IncidentRecord:
    try:
        claimed = store.transition_incident(
            incident.id,
            from_status=IncidentStatus.PLANNED,
            to_status=IncidentStatus.IN_PROGRESS,
            expected_version=incident.version,
            increment_attempt=True,
        )
    except OptimisticLockError:
        logger.info("Incident %s was not claimable for execution", incident.id)
        current = store.get_incident(incident.id)
        if current is None:
            raise
        return current

    try:
        for step_index, step in enumerate(plan.steps):
            action = store.record_action_started(
                incident_id=claimed.id,
                plan_id=plan_id,
                step_index=step_index,
                tool=step.tool,
                params_json=step.params,
            )
            try:
                result = get_tool(step.tool)(**step.params)
            except Exception as exc:
                error_json = exception_json(exc)
                store.finish_action(action.id, status="FAILED", error_json=error_json)
                ACTION_EXECUTED_COUNT.labels(tool_name=step.tool, status="FAILED").inc()
                failure_reason = (
                    "verification_failed" if isinstance(exc, VerificationFailed) else "step_failed"
                )
                last_error_json = {
                    "failure_reason": failure_reason,
                    "step_index": step_index,
                    "tool": step.tool,
                    "error": error_json,
                }
                failed = transition_from_in_progress(
                    store,
                    claimed.id,
                    to_status=IncidentStatus.FAILED,
                    expected_version=claimed.version,
                    last_error_json=last_error_json,
                )
                logger.error(
                    "INCIDENT_FAILED id=%s type=%s reason=%s tool=%s",
                    failed.id,
                    failed.type,
                    failure_reason,
                    step.tool,
                )
                INCIDENT_RESOLUTION_COUNT.labels(type=failed.type, status="FAILED").inc()
                return failed

            store.finish_action(action.id, status="SUCCEEDED", result_json=result)
            ACTION_EXECUTED_COUNT.labels(tool_name=step.tool, status="SUCCEEDED").inc()

        resolved = transition_from_in_progress(
            store,
            claimed.id,
            to_status=IncidentStatus.RESOLVED,
            expected_version=claimed.version,
        )
        logger.warning("INCIDENT_RESOLVED id=%s type=%s", resolved.id, resolved.type)
        INCIDENT_RESOLUTION_COUNT.labels(type=resolved.type, status="RESOLVED").inc()
        return resolved
    except Exception:
        logger.exception("Unexpected executor failure for incident %s", claimed.id)
        raise


def transition_from_in_progress(
    store: SQLiteStore,
    incident_id: str,
    *,
    to_status: IncidentStatus,
    expected_version: int,
    last_error_json: dict[str, Any] | None = None,
) -> IncidentRecord:
    current = store.get_incident(incident_id)
    if current is None:
        raise RuntimeError(f"incident not found: {incident_id}")
    return store.transition_incident(
        incident_id,
        from_status=IncidentStatus.IN_PROGRESS,
        to_status=to_status,
        expected_version=current.version,
        last_error_json=last_error_json,
    )


def exception_json(exc: Exception) -> dict[str, Any]:
    parsed_message: Any
    try:
        parsed_message = json.loads(str(exc))
    except json.JSONDecodeError:
        parsed_message = str(exc)
    return {
        "exception_type": exc.__class__.__name__,
        "message": parsed_message,
        "traceback": traceback.format_exc(),
    }
