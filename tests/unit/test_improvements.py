"""
Tests covering the five architectural improvements:

1.  Planner rule-composition system (PlannerRegistry).
2.  Retry wiring (FAILED → OPEN, MAX_ATTEMPTS = 3).
3.  Deduplication via INSERT OR IGNORE + partial UNIQUE index.
4.  State-machine: FAILED is no longer terminal (FAILED → OPEN is legal).
5.  Docker socket proxy is configured (compose contract test).
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from agent.models.incident import IncidentCandidate, IncidentStatus, IncidentType
from agent.models.state_machine import IncidentStateMachine, InvalidTransitionError
from agent.planner.registry import PlannerRegistry
from agent.planner.strategies.nginx import NginxConfigErrorPlanner
from agent.planner.strategies.app_crash import AppCrashLoopPlanner
from agent.pipeline.orchestrator import RemediationOrchestrator, MAX_ATTEMPTS
from agent.storage.sqlite_store import SQLiteStore


# ---------------------------------------------------------------------------
# 1. Rule-composition system
# ---------------------------------------------------------------------------

class TestPlannerRuleRegistry:
    def test_plan_for_incident_nginx_config_error(self, tmp_path):
        store = SQLiteStore(tmp_path / "incidents.sqlite3")
        store.initialize()
        incident = store.create_incident(
            "NGINX_CONFIG_ERROR",
            "Nginx failed to load managed config.",
            status=IncidentStatus.OPEN,
        )

        registry = PlannerRegistry()
        registry.register(NginxConfigErrorPlanner())
        plan = registry.plan_for(incident)

        assert [step.tool for step in plan.steps] == [
            "render_known_good_nginx_config",
            "nginx_configtest",
            "atomic_replace",
            "restart_container",
            "verify_health_stable",
        ]
        assert plan.incident_id == incident.id
        assert plan.risk_level == "medium"

    def test_plan_for_incident_app_crash_loop(self, tmp_path):
        store = SQLiteStore(tmp_path / "incidents.sqlite3")
        store.initialize()
        incident = store.create_incident(
            "APP_CRASH_LOOP",
            "App container is crash-looping.",
            status=IncidentStatus.OPEN,
        )

        registry = PlannerRegistry()
        registry.register(AppCrashLoopPlanner())
        plan = registry.plan_for(incident)

        assert [step.tool for step in plan.steps] == [
            "write_runtime_flags",
            "restart_container",
            "verify_health_stable",
        ]
        assert plan.risk_level == "low"

    def test_plan_for_incident_unknown_type_raises(self, tmp_path):
        store = SQLiteStore(tmp_path / "incidents.sqlite3")
        store.initialize()
        incident = store.create_incident(
            "UNKNOWN_TYPE_XYZ",
            "Some unknown fault.",
            status=IncidentStatus.OPEN,
        )

        registry = PlannerRegistry()
        with pytest.raises(ValueError, match="UNKNOWN_TYPE_XYZ"):
            registry.plan_for(incident)


# ---------------------------------------------------------------------------
# 2. State machine — FAILED is now retryable
# ---------------------------------------------------------------------------

class TestStateMachineRetryTransition:
    def test_failed_to_open_is_legal(self):
        assert IncidentStateMachine.can_transition(IncidentStatus.FAILED, IncidentStatus.OPEN)

    def test_failed_to_planned_is_illegal(self):
        assert not IncidentStateMachine.can_transition(
            IncidentStatus.FAILED, IncidentStatus.PLANNED
        )

    def test_failed_to_resolved_is_illegal(self):
        assert not IncidentStateMachine.can_transition(
            IncidentStatus.FAILED, IncidentStatus.RESOLVED
        )

    def test_failed_is_in_actionable_statuses(self):
        """FAILED must be included so the worker loop picks up retry candidates."""
        assert IncidentStatus.FAILED in IncidentStateMachine.actionable_statuses()

    def test_resolved_is_not_actionable(self):
        assert IncidentStatus.RESOLVED not in IncidentStateMachine.actionable_statuses()

    def test_needs_human_is_not_actionable(self):
        assert IncidentStatus.NEEDS_HUMAN not in IncidentStateMachine.actionable_statuses()

    def test_assert_can_transition_failed_open_does_not_raise(self):
        IncidentStateMachine.assert_can_transition(IncidentStatus.FAILED, IncidentStatus.OPEN)

    def test_assert_can_transition_failed_resolved_raises(self):
        with pytest.raises(InvalidTransitionError):
            IncidentStateMachine.assert_can_transition(
                IncidentStatus.FAILED, IncidentStatus.RESOLVED
            )


# ---------------------------------------------------------------------------
# 3. Retry wiring in Orchestrator
# ---------------------------------------------------------------------------

class TestRetryWiring:
    """Tests for retry_failed_incident() and the MAX_ATTEMPTS gate."""

    def _make_failed_incident(self, store, attempt_count=1):
        """Create an incident that is already FAILED with a given attempt_count."""
        inc = store.create_incident("APP_CRASH_LOOP", "crash loop", status=IncidentStatus.OPEN)
        planned = store.transition_incident(
            inc.id,
            from_status=IncidentStatus.OPEN,
            to_status=IncidentStatus.PLANNED,
            expected_version=inc.version,
        )
        in_progress = store.transition_incident(
            planned.id,
            from_status=IncidentStatus.PLANNED,
            to_status=IncidentStatus.IN_PROGRESS,
            expected_version=planned.version,
            increment_attempt=True,
        )
        failed = store.transition_incident(
            in_progress.id,
            from_status=IncidentStatus.IN_PROGRESS,
            to_status=IncidentStatus.FAILED,
            expected_version=in_progress.version,
            last_error_json={"failure_reason": "step_failed"},
        )
        assert failed.attempt_count == attempt_count
        return failed

    def test_retry_transitions_failed_to_open(self, tmp_path):
        store = SQLiteStore(tmp_path / "incidents.sqlite3")
        store.initialize()
        failed = self._make_failed_incident(store)
        
        # Bypass backoff check
        import datetime
        with store.connection() as conn:
            conn.execute(
                "UPDATE incidents SET updated_at = ? WHERE id = ?",
                ((datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=60)).isoformat(), failed.id)
            )
        failed = store.get_incident(failed.id)

        orchestrator = RemediationOrchestrator(MagicMock(), MagicMock(), store, MagicMock(), "http://nginx/health", [])
        retried = orchestrator.retry_failed_incident(failed)

        assert retried is not None
        assert retried.status == IncidentStatus.OPEN

    def test_retry_exhausted_escalates_to_needs_human(self, tmp_path):
        store = SQLiteStore(tmp_path / "incidents.sqlite3")
        store.initialize()

        inc = store.create_incident(
            "APP_CRASH_LOOP",
            "crash loop — max attempts",
            status=IncidentStatus.FAILED,
        )
        with store.connection() as conn:
            conn.execute(
                "UPDATE incidents SET attempt_count = ? WHERE id = ?",
                (MAX_ATTEMPTS, inc.id),
            )
        exhausted = store.get_incident(inc.id)
        assert exhausted.attempt_count == MAX_ATTEMPTS

        orchestrator = RemediationOrchestrator(MagicMock(), MagicMock(), store, MagicMock(), "http://nginx/health", [])
        result = orchestrator.retry_failed_incident(exhausted)

        assert result is None
        final = store.get_incident(inc.id)
        assert final.status == IncidentStatus.NEEDS_HUMAN
        assert final.last_error_json["failure_reason"] == "max_attempts_exhausted"

    def test_store_transition_failed_to_open(self, tmp_path):
        """The store itself must allow FAILED → OPEN now."""
        store = SQLiteStore(tmp_path / "incidents.sqlite3")
        store.initialize()
        failed = self._make_failed_incident(store)

        reopened = store.transition_incident(
            failed.id,
            from_status=IncidentStatus.FAILED,
            to_status=IncidentStatus.OPEN,
            expected_version=failed.version,
        )

        assert reopened.status == IncidentStatus.OPEN


# ---------------------------------------------------------------------------
# 4. Deduplication — partial UNIQUE index via INSERT OR IGNORE
# ---------------------------------------------------------------------------

class TestDeduplicationIndex:
    def _candidate(self, incident_type: IncidentType = IncidentType.APP_CRASH_LOOP):
        return IncidentCandidate(
            type=incident_type,
            summary="App is crash-looping",
            evidence=[{"container": "app", "restart_count": 5}],
        )

    def test_first_insert_succeeds(self, tmp_path):
        store = SQLiteStore(tmp_path / "incidents.sqlite3")
        store.initialize()
        candidate = self._candidate()

        result = store.create_incidents_from_candidates([candidate])

        assert len(result) == 1
        assert result[0].status == IncidentStatus.OPEN

    def test_duplicate_while_open_returns_none(self, tmp_path):
        store = SQLiteStore(tmp_path / "incidents.sqlite3")
        store.initialize()
        candidate = self._candidate()

        first = store.create_incidents_from_candidates([candidate])
        second = store.create_incidents_from_candidates([candidate])

        assert len(first) == 1
        assert len(second) == 0
        assert len(store.list_incidents()) == 1

    def test_duplicate_while_planned_returns_none(self, tmp_path):
        store = SQLiteStore(tmp_path / "incidents.sqlite3")
        store.initialize()
        candidate = self._candidate()

        first = store.create_incidents_from_candidates([candidate])
        store.transition_incident(
            first[0].id,
            from_status=IncidentStatus.OPEN,
            to_status=IncidentStatus.PLANNED,
            expected_version=first[0].version,
        )

        second = store.create_incidents_from_candidates([candidate])
        assert len(second) == 0

    def test_duplicate_while_failed_returns_none(self, tmp_path):
        store = SQLiteStore(tmp_path / "incidents.sqlite3")
        store.initialize()
        store.create_incident(
            IncidentType.APP_CRASH_LOOP.value,
            "crash loop",
            status=IncidentStatus.FAILED,
        )
        candidate = self._candidate()

        result = store.create_incidents_from_candidates([candidate])

        assert len(result) == 0

    def test_new_incident_allowed_after_resolved(self, tmp_path):
        store = SQLiteStore(tmp_path / "incidents.sqlite3")
        store.initialize()
        candidate = self._candidate()

        first = store.create_incidents_from_candidates([candidate])
        planned = store.transition_incident(
            first[0].id,
            from_status=IncidentStatus.OPEN,
            to_status=IncidentStatus.PLANNED,
            expected_version=first[0].version,
        )
        in_progress = store.transition_incident(
            planned.id,
            from_status=IncidentStatus.PLANNED,
            to_status=IncidentStatus.IN_PROGRESS,
            expected_version=planned.version,
        )
        store.transition_incident(
            in_progress.id,
            from_status=IncidentStatus.IN_PROGRESS,
            to_status=IncidentStatus.RESOLVED,
            expected_version=in_progress.version,
        )

        second = store.create_incidents_from_candidates([candidate])

        assert len(second) == 1
        assert second[0].status == IncidentStatus.OPEN

    def test_concurrent_inserts_produce_one_incident(self, tmp_path):
        store = SQLiteStore(tmp_path / "incidents.sqlite3")
        store.initialize()
        candidate = self._candidate()
        results: list[object] = []
        barrier = threading.Barrier(2)

        def insert():
            barrier.wait()
            result = store.create_incidents_from_candidates([candidate])
            results.append(result)

        t1 = threading.Thread(target=insert)
        t2 = threading.Thread(target=insert)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        non_empty = [r for r in results if r]
        assert len(non_empty) == 1
        assert len(store.list_incidents()) == 1


# ---------------------------------------------------------------------------
# 5. Docker socket proxy compose contract
# ---------------------------------------------------------------------------

class TestDockerSocketProxyCompose:
    def _compose_text(self) -> str:
        import pathlib
        compose_path = pathlib.Path(__file__).parent.parent.parent / "docker-compose.yml"
        return compose_path.read_text(encoding="utf-8")

    def test_socket_proxy_service_present(self):
        assert "docker-socket-proxy" in self._compose_text()

    def test_agent_does_not_mount_raw_socket(self):
        text = self._compose_text()
        agent_section_start = text.index("  agent:")
        agent_section = text[agent_section_start:]
        assert (
            "/var/run/docker.sock:/var/run/docker.sock"
            not in agent_section.split("  volumes:\n")[0]
        )

    def test_agent_uses_docker_host_env(self):
        assert "DOCKER_HOST: tcp://docker-socket-proxy" in self._compose_text()

    def test_proxy_has_internal_network(self):
        text = self._compose_text()
        assert "socket_proxy" in text
        assert "internal: true" in text

    def test_proxy_only_mounts_socket_readonly(self):
        text = self._compose_text()
        assert "/var/run/docker.sock:/var/run/docker.sock:ro" in text
