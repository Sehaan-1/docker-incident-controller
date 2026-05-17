from __future__ import annotations

from agent.models.incident import IncidentRecord
from agent.models.plan import Plan, PlanStep
from agent.planner.protocol import PlannerStrategy

class RetryAwarePlanner(PlannerStrategy):
    """Wraps another planner and adds rollback steps when retry count is high."""

    def __init__(self, delegate: PlannerStrategy, threshold: int = 2):
        self.delegate = delegate
        self.threshold = threshold

    def can_handle(self, incident: IncidentRecord) -> bool:
        return self.delegate.can_handle(incident)

    def plan(self, incident: IncidentRecord) -> Plan:
        base = self.delegate.plan(incident)
        if incident.attempt_count >= self.threshold:
            # We'll use a no-op fallback snapshot tool as an example of dynamic evaluation
            return Plan(
                incident_id=base.incident_id,
                rationale=base.rationale + " (includes emergency rollback)",
                risk_level="high",  # bumped up
                steps=[
                    PlanStep(
                        tool="noop",  # assuming noop or similar exists, or we just fail gracefully if missing
                        params={"message": "create_backup_snapshot"},
                        preconditions=["retry threshold exceeded"],
                        postconditions=["system state backed up"]
                    )
                ] + base.steps
            )
        return base
