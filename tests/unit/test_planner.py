from __future__ import annotations

from agent.models.incident import IncidentStatus
from agent.planner.rules import plan_for_incident
from agent.storage.sqlite_store import SQLiteStore


def test_plans_nginx_config_error_steps(tmp_path):
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


def test_plans_app_crash_loop_steps(tmp_path):
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
