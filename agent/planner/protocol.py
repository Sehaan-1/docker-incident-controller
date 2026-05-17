from __future__ import annotations
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from agent.models.incident import IncidentRecord
    from agent.models.plan import Plan

class PlannerStrategy(Protocol):
    """A strategy that can generate a remediation plan for a subset of incidents."""

    def can_handle(self, incident: "IncidentRecord") -> bool: ...
    def plan(self, incident: "IncidentRecord") -> "Plan": ...
