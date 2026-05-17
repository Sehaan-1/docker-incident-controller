from __future__ import annotations

import pytest

from agent.models.incident import IncidentStatus
from agent.models.plan import Plan, PlanStep
from agent.observer.observer import Observation
from agent.storage.sqlite_store import OptimisticLockError, SQLiteStore
from agent.storage.sqlite_store import utc_now


def test_create_and_list_incidents(tmp_path):
    store = SQLiteStore(tmp_path / "incidents.sqlite3")
    store.initialize()

    incident = store.create_incident(
        incident_type="TEST_INCIDENT",
        summary="Synthetic plumbing incident",
    )

    assert incident.type == "TEST_INCIDENT"
    assert incident.status == IncidentStatus.OPEN
    assert incident.version == 0
    assert store.get_incident(incident.id) == incident
    assert store.list_incidents(status=IncidentStatus.OPEN) == [incident]


def test_transition_uses_optimistic_lock(tmp_path):
    store = SQLiteStore(tmp_path / "incidents.sqlite3")
    store.initialize()
    incident = store.create_incident("TEST_INCIDENT", "Synthetic plumbing incident")

    transitioned = store.transition_incident(
        incident.id,
        from_status=IncidentStatus.OPEN,
        to_status=IncidentStatus.PLANNED,
        expected_version=incident.version,
    )

    assert transitioned.status == IncidentStatus.PLANNED
    assert transitioned.version == 1

    # Attempting a valid-direction transition with the *stale* version triggers
    # OptimisticLockError (version mismatch in the DB).
    with pytest.raises(OptimisticLockError):
        store.transition_incident(
            incident.id,
            from_status=IncidentStatus.PLANNED,
            to_status=IncidentStatus.IN_PROGRESS,
            expected_version=incident.version,  # stale — already bumped to 1
        )

    # An illegal state-machine transition is rejected before the DB is touched.
    from agent.models.state_machine import InvalidTransitionError

    with pytest.raises(InvalidTransitionError):
        store.transition_incident(
            incident.id,
            from_status=IncidentStatus.OPEN,
            to_status=IncidentStatus.IN_PROGRESS,
            expected_version=transitioned.version,
        )


def test_startup_recovery_marks_in_progress_needs_human(tmp_path):
    store = SQLiteStore(tmp_path / "incidents.sqlite3")
    store.initialize()
    incident = store.create_incident(
        "TEST_INCIDENT",
        "Synthetic plumbing incident",
        status=IncidentStatus.IN_PROGRESS,
    )

    recovered = store.mark_in_progress_needs_human()

    updated = store.get_incident(incident.id)
    assert recovered == 1
    assert updated is not None
    assert updated.status == IncidentStatus.NEEDS_HUMAN
    assert updated.last_error_json is not None
    assert updated.last_error_json["failure_reason"] == "agent_restarted_while_in_progress"


def test_record_observations(tmp_path):
    store = SQLiteStore(tmp_path / "incidents.sqlite3")
    store.initialize()

    count = store.record_observations(
        [
            Observation(
                ts=utc_now(),
                source="nginx",
                kind="log",
                payload={"text": "nginx: [emerg] unknown directive"},
            )
        ]
    )

    observations = store.list_observations()
    assert count == 1
    assert observations[0].source == "nginx"
    assert observations[0].payload_json["text"].startswith("nginx")


def test_create_plan_and_finish_action(tmp_path):
    store = SQLiteStore(tmp_path / "incidents.sqlite3")
    store.initialize()
    incident = store.create_incident("APP_CRASH_LOOP", "App crash loop")
    plan = Plan(
        incident_id=incident.id,
        rationale="test",
        risk_level="low",
        steps=[PlanStep(tool="write_runtime_flags", params={"flags": {"crash_on_start": False}})],
    )

    plan_record = store.create_plan(plan)
    action = store.record_action_started(
        incident_id=incident.id,
        plan_id=plan_record.id,
        step_index=0,
        tool="write_runtime_flags",
        params_json={"flags": {"crash_on_start": False}},
    )
    finished = store.finish_action(action.id, status="SUCCEEDED", result_json={"ok": True})

    assert store.latest_plan_for_incident(incident.id).id == plan_record.id
    assert finished.status.value == "SUCCEEDED"
    assert finished.result_json == {"ok": True}


# ---------------------------------------------------------------------------
# Step 3B: store rejects illegal state transitions via IncidentStateMachine
# ---------------------------------------------------------------------------


def test_transition_rejects_illegal_state_change(tmp_path):
    """
    Walk an incident through the full happy path to RESOLVED, then verify
    that the store raises InvalidTransitionError for a RESOLVED → OPEN
    attempt — before the SQL UPDATE even runs.
    """
    from agent.models.state_machine import InvalidTransitionError

    store = SQLiteStore(tmp_path / "incidents.sqlite3")
    store.initialize()
    inc = store.create_incident("NGINX_CONFIG_ERROR", "test")

    # OPEN → PLANNED
    planned = store.transition_incident(
        inc.id,
        from_status=IncidentStatus.OPEN,
        to_status=IncidentStatus.PLANNED,
        expected_version=inc.version,
    )
    assert planned.status == IncidentStatus.PLANNED

    # PLANNED → IN_PROGRESS
    in_progress = store.transition_incident(
        planned.id,
        from_status=IncidentStatus.PLANNED,
        to_status=IncidentStatus.IN_PROGRESS,
        expected_version=planned.version,
    )
    assert in_progress.status == IncidentStatus.IN_PROGRESS

    # IN_PROGRESS → RESOLVED
    resolved = store.transition_incident(
        in_progress.id,
        from_status=IncidentStatus.IN_PROGRESS,
        to_status=IncidentStatus.RESOLVED,
        expected_version=in_progress.version,
    )
    assert resolved.status == IncidentStatus.RESOLVED

    # RESOLVED → OPEN must be rejected by the state machine guard
    with pytest.raises(InvalidTransitionError):
        store.transition_incident(
            resolved.id,
            from_status=IncidentStatus.RESOLVED,
            to_status=IncidentStatus.OPEN,
            expected_version=resolved.version,
        )


def test_transition_rejects_open_to_in_progress_skip(tmp_path):
    """OPEN → IN_PROGRESS is not a legal transition (must go via PLANNED)."""
    from agent.models.state_machine import InvalidTransitionError

    store = SQLiteStore(tmp_path / "incidents.sqlite3")
    store.initialize()
    inc = store.create_incident("APP_CRASH_LOOP", "skip-step test")

    with pytest.raises(InvalidTransitionError, match="IN_PROGRESS"):
        store.transition_incident(
            inc.id,
            from_status=IncidentStatus.OPEN,
            to_status=IncidentStatus.IN_PROGRESS,
            expected_version=inc.version,
        )
