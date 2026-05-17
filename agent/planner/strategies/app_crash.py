from __future__ import annotations

from agent.models.incident import IncidentRecord, IncidentType
from agent.models.plan import Plan, PlanStep
from agent.planner.protocol import PlannerStrategy

class AppCrashLoopPlanner(PlannerStrategy):
    def can_handle(self, incident: IncidentRecord) -> bool:
        return incident.type == IncidentType.APP_CRASH_LOOP.value

    def plan(self, incident: IncidentRecord) -> Plan:
        return Plan(
            incident_id=incident.id,
            rationale="Disable the app startup crash flag, restart app, then verify health.",
            risk_level="low",
            steps=[
                PlanStep(
                    tool="write_runtime_flags",
                    params={"flags": {"crash_on_start": False}},
                    preconditions=["runtime volume is writable by agent"],
                    postconditions=["runtime flags disable crash_on_start"],
                ),
                PlanStep(
                    tool="restart_container",
                    params={"name": "app"},
                    preconditions=["runtime flags were written"],
                    postconditions=["app container is restarted"],
                ),
                PlanStep(
                    tool="verify_health_stable",
                    params={
                        "url": "http://nginx/health",
                        "stable_window_s": 20,
                        "max_wait_s": 60,
                    },
                    preconditions=["app restart requested"],
                    postconditions=["health endpoint remains healthy for the stable window"],
                ),
            ],
        )
