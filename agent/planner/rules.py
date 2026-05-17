"""
Planner rule composition system.

Instead of a hardcoded if/else chain, each incident type is described by a
``PlanRule`` — a pure-data object that declares the ordered ``PlanStep``s and
the plan metadata (rationale, risk_level).  The ``PLAN_RULES`` registry maps
``IncidentType`` → ``PlanRule``; ``plan_for_incident`` is a thin dispatcher.

Adding support for a new incident type requires *only* adding a new entry to
``PLAN_RULES`` — the dispatch logic itself never changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.models.incident import IncidentRecord, IncidentType
from agent.models.plan import Plan, PlanStep


@dataclass(frozen=True)
class PlanRule:
    """Data-only description of how to remediate a particular incident type."""

    incident_type: IncidentType
    rationale: str
    risk_level: str
    steps: list[PlanStep] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Registry — add new incident types here; dispatcher never changes.
# ---------------------------------------------------------------------------

PLAN_RULES: dict[IncidentType, PlanRule] = {
    IncidentType.NGINX_CONFIG_ERROR: PlanRule(
        incident_type=IncidentType.NGINX_CONFIG_ERROR,
        rationale=(
            "Replace the managed nginx site snippet with known-good config, "
            "then restart and verify."
        ),
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
    ),
    IncidentType.APP_CRASH_LOOP: PlanRule(
        incident_type=IncidentType.APP_CRASH_LOOP,
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
    ),
}


def plan_for_incident(incident: IncidentRecord) -> Plan:
    """
    Dispatch to the registered ``PlanRule`` for *incident.type* and return a
    ``Plan`` bound to the given incident.

    Raises ``ValueError`` with a human-readable listing of known types when no
    rule is registered for the supplied incident type.
    """
    try:
        incident_type = IncidentType(incident.type)
    except ValueError:
        known = ", ".join(t.value for t in PLAN_RULES)
        raise ValueError(f"unknown incident type {incident.type!r}; known types: {known}") from None

    rule = PLAN_RULES.get(incident_type)
    if rule is None:
        known = ", ".join(t.value for t in PLAN_RULES)
        raise ValueError(
            f"no planner rule registered for {incident_type.value!r}; known types: {known}"
        )

    return Plan(
        incident_id=incident.id,
        rationale=rule.rationale,
        risk_level=rule.risk_level,
        steps=list(rule.steps),  # defensive copy — rules are immutable but Plan is not
    )
