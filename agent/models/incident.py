from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class IncidentStatus(str, Enum):
    OPEN = "OPEN"
    PLANNED = "PLANNED"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    FAILED = "FAILED"
    NEEDS_HUMAN = "NEEDS_HUMAN"


class ActionStatus(str, Enum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class IncidentRecord(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    id: str
    type: str
    status: IncidentStatus
    summary: str
    created_at: datetime
    updated_at: datetime
    version: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    last_error_json: dict[str, Any] | None = None


class IncidentType(str, Enum):
    NGINX_CONFIG_ERROR = "NGINX_CONFIG_ERROR"
    APP_CRASH_LOOP = "APP_CRASH_LOOP"


class IncidentCandidate(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    type: IncidentType
    summary: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class ObservationRecord(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    id: int
    ts: datetime
    source: str
    kind: str
    payload_json: dict[str, Any]


class ActionRecord(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    id: int
    incident_id: str
    plan_id: int | None = None
    step_index: int
    tool: str
    params_json: dict[str, Any]
    status: ActionStatus
    started_at: datetime
    finished_at: datetime | None = None
    result_json: dict[str, Any] | None = None
    error_json: dict[str, Any] | None = None
