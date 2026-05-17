from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.models.incident import IncidentRecord
    from agent.models.plan import Plan
    from agent.planner.protocol import PlannerStrategy

class PlannerRegistry:
    def __init__(self) -> None:
        self._strategies: list["PlannerStrategy"] = []

    def register(self, strategy: "PlannerStrategy") -> "PlannerRegistry":
        self._strategies.append(strategy)
        return self

    def plan_for(self, incident: "IncidentRecord") -> "Plan":
        for strategy in self._strategies:
            if strategy.can_handle(incident):
                return strategy.plan(incident)
        known = ", ".join(type(s).__name__ for s in self._strategies)
        raise ValueError(f"No planner registered for incident type {incident.type!r}. Known strategies: {known}")
