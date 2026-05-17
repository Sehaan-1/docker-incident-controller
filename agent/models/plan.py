from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PlanStep(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    tool: str
    params: dict[str, Any] = Field(default_factory=dict)
    preconditions: list[str] = Field(default_factory=list)
    postconditions: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    incident_id: str
    rationale: str
    risk_level: str
    steps: list[PlanStep]


class PlanRecord(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    id: int
    incident_id: str
    plan_json: dict[str, Any]
    created_at: datetime
