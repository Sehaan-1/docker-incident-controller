"""
tests/unit/test_model_serialization.py

Defensive serialization tests for Pydantic models.

These tests verify that every model with enum fields serializes correctly
when model_dump(mode="json") is called.  They are *intentionally green
before and after* the model_config addition — the ConfigDict change is
purely defensive: it guards against a future developer accidentally
dropping mode="json" from call-sites and introducing silent breakage.
"""

from __future__ import annotations

import json


from agent.models.incident import (
    IncidentCandidate,
    IncidentType,
    ObservationRecord,
)
from agent.models.plan import Plan, PlanRecord, PlanStep


# ---------------------------------------------------------------------------
# IncidentCandidate
# ---------------------------------------------------------------------------


class TestIncidentCandidateSerialization:
    def test_type_serializes_as_string_in_json_mode(self):
        candidate = IncidentCandidate(
            type=IncidentType.NGINX_CONFIG_ERROR,
            summary="nginx config is broken",
        )
        data = candidate.model_dump(mode="json")
        assert isinstance(data["type"], str), "type must be a str in JSON mode"
        assert data["type"] == "NGINX_CONFIG_ERROR"

    def test_app_crash_loop_type_serializes(self):
        candidate = IncidentCandidate(
            type=IncidentType.APP_CRASH_LOOP,
            summary="app is crashing",
        )
        data = candidate.model_dump(mode="json")
        assert data["type"] == "APP_CRASH_LOOP"

    def test_evidence_defaults_to_empty_list(self):
        candidate = IncidentCandidate(
            type=IncidentType.NGINX_CONFIG_ERROR,
            summary="test",
        )
        data = candidate.model_dump(mode="json")
        assert data["evidence"] == []

    def test_json_roundtrip_is_valid(self):
        candidate = IncidentCandidate(
            type=IncidentType.NGINX_CONFIG_ERROR,
            summary="test summary",
            evidence=[{"key": "value"}],
        )
        raw = json.dumps(candidate.model_dump(mode="json"))
        parsed = json.loads(raw)
        assert parsed["type"] == "NGINX_CONFIG_ERROR"
        assert parsed["summary"] == "test summary"


# ---------------------------------------------------------------------------
# ObservationRecord
# ---------------------------------------------------------------------------


class TestObservationRecordSerialization:
    def test_fields_serialize_correctly(self):
        from datetime import datetime, timezone

        record = ObservationRecord(
            id=1,
            ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
            source="observer",
            kind="log",
            payload_json={"msg": "hello"},
        )
        data = record.model_dump(mode="json")
        assert data["id"] == 1
        assert data["source"] == "observer"
        assert data["kind"] == "log"
        assert data["payload_json"] == {"msg": "hello"}


# ---------------------------------------------------------------------------
# Plan / PlanStep / PlanRecord
# ---------------------------------------------------------------------------


class TestPlanSerialization:
    def _make_plan(self) -> Plan:
        return Plan(
            incident_id="i-1",
            rationale="restart the nginx container",
            risk_level="low",
            steps=[
                PlanStep(
                    tool="restart_container",
                    params={"name": "nginx"},
                    preconditions=["container_exists"],
                    postconditions=["container_running"],
                )
            ],
        )

    def test_rationale_serializes(self):
        data = self._make_plan().model_dump(mode="json")
        assert data["rationale"] == "restart the nginx container"

    def test_steps_serialize(self):
        data = self._make_plan().model_dump(mode="json")
        assert len(data["steps"]) == 1
        step = data["steps"][0]
        assert step["tool"] == "restart_container"
        assert step["params"] == {"name": "nginx"}
        assert step["preconditions"] == ["container_exists"]
        assert step["postconditions"] == ["container_running"]

    def test_plan_json_roundtrip(self):
        plan = self._make_plan()
        raw = json.dumps(plan.model_dump(mode="json"))
        parsed = json.loads(raw)
        assert parsed["incident_id"] == "i-1"
        assert parsed["risk_level"] == "low"


class TestPlanStepDefaults:
    def test_empty_params_and_conditions_default(self):
        step = PlanStep(tool="check_health")
        data = step.model_dump(mode="json")
        assert data["params"] == {}
        assert data["preconditions"] == []
        assert data["postconditions"] == []


class TestPlanRecordSerialization:
    def test_fields_serialize_correctly(self):
        from datetime import datetime, timezone

        record = PlanRecord(
            id=42,
            incident_id="i-99",
            plan_json={"steps": []},
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        data = record.model_dump(mode="json")
        assert data["id"] == 42
        assert data["incident_id"] == "i-99"
        assert data["plan_json"] == {"steps": []}
