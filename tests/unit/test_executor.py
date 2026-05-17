from __future__ import annotations

from agent.executor.runner import execute_plan
from agent.models.incident import IncidentStatus
from agent.models.plan import Plan, PlanStep
from agent.storage.sqlite_store import SQLiteStore
from agent.tools import allowlist


def test_executor_records_successful_actions_and_resolves(monkeypatch, tmp_path):
    store = SQLiteStore(tmp_path / "incidents.sqlite3")
    store.initialize()
    incident = store.create_incident(
        "APP_CRASH_LOOP",
        "App container is crash-looping.",
        status=IncidentStatus.PLANNED,
    )
    plan = Plan(
        incident_id=incident.id,
        rationale="test",
        risk_level="low",
        steps=[
            PlanStep(tool="fake_success", params={"value": 1}),
            PlanStep(tool="fake_success", params={"value": 2}),
        ],
    )
    plan_record = store.create_plan(plan)

    monkeypatch.setattr(
        allowlist,
        "TOOL_REGISTRY",
        {"fake_success": lambda value: {"value": value}},
    )

    result = execute_plan(store, incident, plan_record.id, plan)

    actions = store.list_actions(incident_id=incident.id)
    assert result.status == IncidentStatus.RESOLVED
    assert [action.status.value for action in actions] == ["SUCCEEDED", "SUCCEEDED"]
    assert actions[1].result_json == {"value": 2}


def test_executor_records_failure_and_marks_failed(monkeypatch, tmp_path):
    store = SQLiteStore(tmp_path / "incidents.sqlite3")
    store.initialize()
    incident = store.create_incident(
        "APP_CRASH_LOOP",
        "App container is crash-looping.",
        status=IncidentStatus.PLANNED,
    )
    plan = Plan(
        incident_id=incident.id,
        rationale="test",
        risk_level="low",
        steps=[PlanStep(tool="fake_failure", params={})],
    )
    plan_record = store.create_plan(plan)

    def fail():
        raise RuntimeError("boom")

    monkeypatch.setattr(allowlist, "TOOL_REGISTRY", {"fake_failure": fail})

    result = execute_plan(store, incident, plan_record.id, plan)

    actions = store.list_actions(incident_id=incident.id)
    assert result.status == IncidentStatus.FAILED
    assert result.last_error_json["failure_reason"] == "step_failed"
    assert actions[0].status.value == "FAILED"
    assert actions[0].error_json["message"] == "boom"
