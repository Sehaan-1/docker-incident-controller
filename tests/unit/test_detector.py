from __future__ import annotations

from datetime import UTC, datetime

from agent.detector.rules import detect
from agent.observer.observer import Observation, ObservationsBundle


NOW = datetime(2026, 5, 16, tzinfo=UTC)


def bundle(*observations: Observation) -> ObservationsBundle:
    return ObservationsBundle(ts=NOW, observations=list(observations))


def test_detects_nginx_config_error_from_emerg_log():
    candidates = detect(
        bundle(
            Observation(
                ts=NOW,
                source="nginx",
                kind="log",
                payload={"text": 'nginx: [emerg] unknown directive "bad"'},
            ),
            Observation(
                ts=NOW,
                source="nginx",
                kind="container",
                payload={"restart_count": 2, "state": {"restarting": True}},
            ),
        )
    )

    assert [candidate.type.value for candidate in candidates] == ["NGINX_CONFIG_ERROR"]


def test_ignores_stale_nginx_emerg_log_when_nginx_container_is_healthy():
    candidates = detect(
        bundle(
            Observation(
                ts=NOW,
                source="nginx",
                kind="log",
                payload={"text": "nginx: [emerg] old error from before restart"},
            ),
            Observation(
                ts=NOW,
                source="nginx",
                kind="container",
                payload={"restart_count": 2, "state": {"running": True, "restarting": False}},
            ),
            Observation(
                ts=NOW,
                source="nginx",
                kind="health",
                payload={"ok": False, "status_code": 502},
            ),
        )
    )

    assert candidates == []


def test_detects_app_crash_loop_from_restart_state():
    candidates = detect(
        bundle(
            Observation(
                ts=NOW,
                source="app",
                kind="container",
                payload={"restart_count": 3, "state": {"restarting": True}},
            )
        )
    )

    assert [candidate.type.value for candidate in candidates] == ["APP_CRASH_LOOP"]


def test_does_not_detect_app_crash_loop_below_threshold():
    candidates = detect(
        bundle(
            Observation(
                ts=NOW,
                source="app",
                kind="container",
                payload={"restart_count": 2, "state": {"restarting": True}},
            )
        )
    )

    assert candidates == []
