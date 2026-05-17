from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request

from prometheus_client import make_asgi_app

from agent.models.incident import ActionRecord, IncidentRecord, IncidentStatus, ObservationRecord
from agent.storage.sqlite_store import SQLiteStore

logger = logging.getLogger("agent.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = SQLiteStore.from_env()
    store.initialize()
    recovered = store.mark_in_progress_needs_human()
    if recovered:
        logger.error(
            "Recovered %s incident(s) left IN_PROGRESS; marked NEEDS_HUMAN",
            recovered,
        )
    app.state.store = store
    yield


app = FastAPI(
    title="Docker Incident Controller",
    version="0.1.0",
    description="Read-only incident API for the local self-healing demo.",
    lifespan=lifespan,
)
app.mount("/metrics", make_asgi_app())


def get_store(request: Request) -> SQLiteStore:
    return request.app.state.store


StoreDep = Annotated[SQLiteStore, Depends(get_store)]


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/incidents", response_model=list[IncidentRecord])
def list_incidents(
    store: StoreDep,
    status: Annotated[IncidentStatus | None, Query()] = None,
) -> list[IncidentRecord]:
    return store.list_incidents(status=status)


@app.get("/incidents/{incident_id}", response_model=IncidentRecord)
def get_incident(incident_id: str, store: StoreDep) -> IncidentRecord:
    incident = store.get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return incident


@app.get("/actions", response_model=list[ActionRecord])
def list_actions(
    store: StoreDep,
    incident_id: Annotated[str | None, Query()] = None,
) -> list[ActionRecord]:
    return store.list_actions(incident_id=incident_id)


@app.get("/observations", response_model=list[ObservationRecord])
def list_observations(
    store: StoreDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[ObservationRecord]:
    return store.list_observations(limit=limit)
