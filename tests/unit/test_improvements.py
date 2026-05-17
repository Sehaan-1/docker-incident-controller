"""
Tests covering the five architectural improvements:

1.  Planner rule-composition system (PLAN_RULES registry).
2.  Retry wiring (FAILED → OPEN, MAX_ATTEMPTS = 3).
3.  Deduplication via INSERT OR IGNORE + partial UNIQUE index.
4.  State-machine: FAILED is no longer terminal (FAILED → OPEN is legal).
5.  Docker socket proxy is configured (compose contract test).
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from agent.models.incident import IncidentCandidate, IncidentStatus, IncidentType
from agent.models.state_machine import IncidentStateMachine, InvalidTransitionError
from agent.planner.rules import PLAN_RULES, PlanRule, plan_for_incident
from agent.storage.sqlite_store import SQLiteStore


# ---------------------------------------------------------------------------
# 1. Rule-composition system
# ---------------------------------------------------------------------------


class TestPlannerRuleRegistry:
    def test_all_known_types_have_rules(self):
        """Every IncidentType declared in the model must have a registered rule."""
        for incident_type in IncidentType:
            assert incident_type in PLAN_RULES, (
                f"No PlanRule registered for {incident_type.value!r}"
            )

    def test_rules_are_plan_rule_instances(self):
        for rule in PLAN_RULES.values():
            assert isinstance(rule, PlanRule)

    def test_rules_have_non_empty_steps(self):
        for rule in PLAN_RULES.values():
            assert rule.steps, f"Rule for {rule.incident_type.value!r} has no steps"

    def test_plan_for_incident_nginx_config_error(self, tmp_path):
        store = SQLiteStore(tmp_path / "incidents.sqlite3")
        store.initialize()
        incident = store.create_incident(
            "NGINX_CONFIG_ERROR",
            "Nginx failed to load managed config.",
            status=IncidentStatus.OPEN,
        )

        plan = plan_for_incident(incident)

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

        plan = plan_for_incident(incident)

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

        with pytest.raises(ValueError, match="UNKNOWN_TYPE_XYZ"):
            plan_for_incident(incident)

    def test_returned_steps_are_defensive_copies(self, tmp_path):
        """Mutating the returned Plan's steps list must not affect the registry."""
        store = SQLiteStore(tmp_path / "incidents.sqlite3")
        store.initialize()
        incident = store.create_incident("APP_CRASH_LOOP", "crash loop")
        plan = plan_for_incident(incident)
        original_len = len(PLAN_RULES[IncidentType.APP_CRASH_LOOP].steps)

        plan.steps.clear()  # mutate the returned plan

        assert len(PLAN_RULES[IncidentType.APP_CRASH_LOOP].steps) == original_len


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
# 3. Retry wiring in worker.py
# ---------------------------------------------------------------------------


class TestRetryWiring:
    """Tests for retry_failed_incident() and the MAX_ATTEMPTS gate."""

    def _make_failed_incident(self, store, attempt_count=1):
        """Create an incident that is already FAILED with a given attempt_count."""
        inc = store.create_incident("APP_CRASH_LOOP", "crash loop", status=IncidentStatus.OPEN)
        # Transition through the happy-path states to IN_PROGRESS so we can fail it
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
        from agent.core.worker import retry_failed_incident

        store = SQLiteStore(tmp_path / "incidents.sqlite3")
        store.initialize()
        failed = self._make_failed_incident(store)

        retried = retry_failed_incident(store, failed)

        assert retried is not None
        assert retried.status == IncidentStatus.OPEN

    def test_retry_exhausted_escalates_to_needs_human(self, tmp_path):
        from agent.core.worker import MAX_ATTEMPTS, retry_failed_incident

        store = SQLiteStore(tmp_path / "incidents.sqlite3")
        store.initialize()

        # Simulate an incident that has already used all attempts
        inc = store.create_incident(
            "APP_CRASH_LOOP",
            "crash loop — max attempts",
            status=IncidentStatus.FAILED,
        )
        # Manually bump attempt_count to MAX_ATTEMPTS
        with store.connection() as conn:
            conn.execute(
                "UPDATE incidents SET attempt_count = ? WHERE id = ?",
                (MAX_ATTEMPTS, inc.id),
            )
        exhausted = store.get_incident(inc.id)
        assert exhausted.attempt_count == MAX_ATTEMPTS

        result = retry_failed_incident(store, exhausted)

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

        result = store.create_incident_from_candidate_if_absent(candidate)

        assert result is not None
        assert result.status == IncidentStatus.OPEN

    def test_duplicate_while_open_returns_none(self, tmp_path):
        store = SQLiteStore(tmp_path / "incidents.sqlite3")
        store.initialize()
        candidate = self._candidate()

        first = store.create_incident_from_candidate_if_absent(candidate)
        second = store.create_incident_from_candidate_if_absent(candidate)

        assert first is not None
        assert second is None
        assert len(store.list_incidents()) == 1

    def test_duplicate_while_planned_returns_none(self, tmp_path):
        store = SQLiteStore(tmp_path / "incidents.sqlite3")
        store.initialize()
        candidate = self._candidate()

        first = store.create_incident_from_candidate_if_absent(candidate)
        store.transition_incident(
            first.id,
            from_status=IncidentStatus.OPEN,
            to_status=IncidentStatus.PLANNED,
            expected_version=first.version,
        )

        second = store.create_incident_from_candidate_if_absent(candidate)
        assert second is None

    def test_duplicate_while_failed_returns_none(self, tmp_path):
        """FAILED is now active (retryable), so a duplicate must be blocked."""
        store = SQLiteStore(tmp_path / "incidents.sqlite3")
        store.initialize()
        inc = store.create_incident(
            IncidentType.APP_CRASH_LOOP.value,
            "crash loop",
            status=IncidentStatus.FAILED,
        )
        candidate = self._candidate()

        result = store.create_incident_from_candidate_if_absent(candidate)

        assert result is None

    def test_new_incident_allowed_after_resolved(self, tmp_path):
        """Once an incident is RESOLVED (terminal), a new one may be created."""
        store = SQLiteStore(tmp_path / "incidents.sqlite3")
        store.initialize()
        candidate = self._candidate()

        first = store.create_incident_from_candidate_if_absent(candidate)
        # Walk to RESOLVED
        planned = store.transition_incident(
            first.id,
            from_status=IncidentStatus.OPEN,
            to_status=IncidentStatus.PLANNED,
            expected_version=first.version,
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

        second = store.create_incident_from_candidate_if_absent(candidate)

        assert second is not None
        assert second.status == IncidentStatus.OPEN

    def test_concurrent_inserts_produce_one_incident(self, tmp_path):
        """Simulate two worker threads racing to create the same incident type."""
        store = SQLiteStore(tmp_path / "incidents.sqlite3")
        store.initialize()
        candidate = self._candidate()
        results: list[object] = []
        barrier = threading.Barrier(2)

        def insert():
            barrier.wait()  # synchronise both threads to maximise the race
            result = store.create_incident_from_candidate_if_absent(candidate)
            results.append(result)

        t1 = threading.Thread(target=insert)
        t2 = threading.Thread(target=insert)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        non_none = [r for r in results if r is not None]
        assert len(non_none) == 1, f"Expected exactly 1 created incident, got {len(non_none)}"
        assert len(store.list_incidents()) == 1


# ---------------------------------------------------------------------------
# 5. Docker socket proxy compose contract
# ---------------------------------------------------------------------------


class TestDockerSocketProxyCompose:
    """Smoke-test that the compose file contains the socket proxy configuration."""

    def _compose_text(self) -> str:
        import pathlib

        compose_path = pathlib.Path(__file__).parent.parent.parent / "docker-compose.yml"
        return compose_path.read_text(encoding="utf-8")

    def test_socket_proxy_service_present(self):
        assert "docker-socket-proxy" in self._compose_text()

    def test_agent_does_not_mount_raw_socket(self):
        text = self._compose_text()
        # The raw socket bind-mount must be gone from the agent section.
        # It may still appear under the proxy service, which is acceptable.
        agent_section_start = text.index("  agent:")
        agent_section = text[agent_section_start:]
        # The proxy service follows the agent service, so stop at the next
        # top-level entry that begins with "  volumes:" (compose top-level key).
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
