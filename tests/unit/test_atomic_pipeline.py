"""Tests for SQLiteStore.observe_and_persist_atomic.

These tests verify that the observe→detect→persist pipeline is executed inside
a *single* SQLite transaction so that a partial failure can never leave the
database with observations but no corresponding incident (or vice-versa).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from agent.models.incident import IncidentCandidate, IncidentStatus
from agent.models.incident import IncidentType
from agent.observer.observer import Observation, ObservationsBundle
from agent.storage.sqlite_store import SQLiteStore


def _make_bundle(
    *,
    source: str = "nginx",
    kind: str = "health",
    payload: dict | None = None,
    ts: datetime | None = None,
) -> ObservationsBundle:
    now = ts or datetime.now(UTC)
    return ObservationsBundle(
        ts=now,
        observations=[
            Observation(
                ts=now,
                source=source,
                kind=kind,
                payload=payload
                or {
                    "ok": False,
                    "status_code": 503,
                    "body": "",
                    "error": None,
                    "url": "http://nginx/health",
                },
            )
        ],
    )


# ---------------------------------------------------------------------------
# Basic atomicity: both observations and incidents land together
# ---------------------------------------------------------------------------


def test_atomic_pipeline_writes_observation_and_incident(tmp_path):
    """A single call should insert observation rows AND an incident row."""
    store = SQLiteStore(tmp_path / "incidents.sqlite3")
    store.initialize()

    bundle = _make_bundle()

    # Patch detector to return one synthetic candidate.
    candidate = IncidentCandidate(
        type=IncidentType.NGINX_CONFIG_ERROR,
        summary="nginx is down",
        evidence=[{"status_code": 503}],
    )
    with patch("agent.storage.sqlite_store._detect_candidates", return_value=[candidate]):
        obs_count, created = store.observe_and_persist_atomic(bundle)

    assert obs_count == 1
    assert len(created) == 1
    assert created[0].type == IncidentType.NGINX_CONFIG_ERROR.value
    assert created[0].status == IncidentStatus.OPEN

    # Both rows must be visible in subsequent reads.
    assert len(store.list_observations()) == 1
    assert len(store.list_incidents()) == 1


def test_atomic_pipeline_no_candidates(tmp_path):
    """When detection yields no candidates, observations are still persisted."""
    store = SQLiteStore(tmp_path / "incidents.sqlite3")
    store.initialize()

    bundle = _make_bundle(kind="container", payload={"state": "running"})

    with patch("agent.storage.sqlite_store._detect_candidates", return_value=[]):
        obs_count, created = store.observe_and_persist_atomic(bundle)

    assert obs_count == 1
    assert created == []
    assert len(store.list_observations()) == 1
    assert len(store.list_incidents()) == 0


def test_atomic_pipeline_deduplicates_active_incident(tmp_path):
    """A second call with the same incident type should NOT create a duplicate."""
    store = SQLiteStore(tmp_path / "incidents.sqlite3")
    store.initialize()

    candidate = IncidentCandidate(
        type=IncidentType.NGINX_CONFIG_ERROR,
        summary="nginx is down",
        evidence=[],
    )

    with patch("agent.storage.sqlite_store._detect_candidates", return_value=[candidate]):
        _, first = store.observe_and_persist_atomic(_make_bundle())
        _, second = store.observe_and_persist_atomic(_make_bundle())

    assert len(first) == 1
    assert len(second) == 0  # duplicate suppressed by partial UNIQUE index
    assert len(store.list_incidents()) == 1


# ---------------------------------------------------------------------------
# Atomicity guarantee: if the incident INSERT fails, observations must be
# rolled back too (they're in the same transaction).
# ---------------------------------------------------------------------------


def test_atomic_pipeline_rolls_back_on_incident_insert_error(tmp_path):
    """If the incident INSERT raises, the observation rows must also be absent."""
    store = SQLiteStore(tmp_path / "incidents.sqlite3")
    store.initialize()

    candidate = IncidentCandidate(
        type=IncidentType.NGINX_CONFIG_ERROR,
        summary="nginx is down",
        evidence=[],
    )

    original_connection = store.connection

    def _failing_connection():
        cm = original_connection()

        # Wrap it so conn.execute raises on the second call (the incident INSERT).
        class _FailingConn:
            _call_count = 0

            def __enter__(self_inner):
                self_inner._conn = cm.__enter__()
                return self_inner

            def __exit__(self_inner, *args):
                return cm.__exit__(*args)

            def executemany(self_inner, sql, rows):
                return self_inner._conn.executemany(sql, rows)

            def execute(self_inner, sql, params=()):
                self_inner._call_count += 1
                if "INSERT OR IGNORE INTO incidents" in sql:
                    raise sqlite3.OperationalError("injected failure")
                return self_inner._conn.execute(sql, params)

        return _FailingConn()

    with patch.object(store, "connection", side_effect=_failing_connection):
        with pytest.raises(sqlite3.OperationalError, match="injected failure"):
            with patch("agent.storage.sqlite_store._detect_candidates", return_value=[candidate]):
                store.observe_and_persist_atomic(_make_bundle())

    # Both tables must be empty — the transaction was rolled back.
    assert store.list_observations() == []
    assert store.list_incidents() == []


# ---------------------------------------------------------------------------
# Multiple candidates in one pass all land in the same transaction
# ---------------------------------------------------------------------------


def test_atomic_pipeline_multiple_candidates(tmp_path):
    """Two distinct candidates created in a single pass share one transaction."""
    store = SQLiteStore(tmp_path / "incidents.sqlite3")
    store.initialize()

    candidates = [
        IncidentCandidate(type=IncidentType.NGINX_CONFIG_ERROR, summary="nginx down", evidence=[]),
        IncidentCandidate(type=IncidentType.APP_CRASH_LOOP, summary="app crashing", evidence=[]),
    ]

    with patch("agent.storage.sqlite_store._detect_candidates", return_value=candidates):
        obs_count, created = store.observe_and_persist_atomic(_make_bundle())

    assert obs_count == 1
    assert len(created) == 2
    types = {r.type for r in created}
    assert IncidentType.NGINX_CONFIG_ERROR.value in types
    assert IncidentType.APP_CRASH_LOOP.value in types
