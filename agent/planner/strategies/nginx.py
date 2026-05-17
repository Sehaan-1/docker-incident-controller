from __future__ import annotations

from agent.models.incident import IncidentRecord, IncidentType
from agent.models.plan import Plan, PlanStep
from agent.planner.protocol import PlannerStrategy

class NginxConfigErrorPlanner(PlannerStrategy):
    def can_handle(self, incident: IncidentRecord) -> bool:
        return incident.type == IncidentType.NGINX_CONFIG_ERROR.value

    def plan(self, incident: IncidentRecord) -> Plan:
        return Plan(
            incident_id=incident.id,
            rationale="Replace the managed nginx site snippet with known-good config, then restart and verify.",
            risk_level="medium",
            steps=[
                PlanStep(
                    tool="render_known_good_nginx_config",
                    params={"target": "/nginx_conf/site.conf.tmp"},
                    preconditions=["nginx_conf volume is writable by agent"],
                    postconditions=["candidate config exists in nginx_conf volume"],
                ),
                PlanStep(
                    tool="nginx_configtest",
                    params={"config_path": "/nginx_conf/site.conf.tmp"},
                    preconditions=["candidate config exists"],
                    postconditions=["candidate config passes nginx -t in sandbox"],
                ),
                PlanStep(
                    tool="atomic_replace",
                    params={
                        "src": "/nginx_conf/site.conf.tmp",
                        "dst": "/nginx_conf/site.conf",
                    },
                    preconditions=["candidate config passed validation"],
                    postconditions=["managed site.conf is replaced atomically within the same volume"],
                ),
                PlanStep(
                    tool="restart_container",
                    params={"name": "nginx"},
                    preconditions=["known-good site.conf is in place"],
                    postconditions=["nginx container is restarted"],
                ),
                PlanStep(
                    tool="verify_health_stable",
                    params={
                        "url": "http://nginx/health",
                        "stable_window_s": 20,
                        "max_wait_s": 60,
                    },
                    preconditions=["nginx restart requested"],
                    postconditions=["health endpoint remains healthy for the stable window"],
                ),
            ],
        )
