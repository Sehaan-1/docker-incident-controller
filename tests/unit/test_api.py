from __future__ import annotations

from fastapi.testclient import TestClient

from agent.api.main import app
from agent.models.incident import IncidentStatus


def test_healthz_and_incident_listing(monkeypatch, tmp_path):
    monkeypatch.setenv("INCIDENT_DB_PATH", str(tmp_path / "incidents.sqlite3"))

    with TestClient(app) as client:
        incident = client.app.state.store.create_incident(
            incident_type="TEST_INCIDENT",
            summary="Synthetic plumbing incident",
            status=IncidentStatus.OPEN,
        )

        health = client.get("/healthz")
        incidents = client.get("/incidents", params={"status": "OPEN"})
        incident_detail = client.get(f"/incidents/{incident.id}")
        observations = client.get("/observations")
        missing = client.get("/incidents/does-not-exist")

    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert incidents.status_code == 200
    assert incidents.json()[0]["id"] == incident.id
    assert incident_detail.status_code == 200
    assert incident_detail.json()["type"] == "TEST_INCIDENT"
    assert observations.status_code == 200
    assert observations.json() == []
    assert missing.status_code == 404
