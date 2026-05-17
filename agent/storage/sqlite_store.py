from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.detector.rules import detect as _detect_candidates
from agent.models.incident import (
    ActionRecord,
    ActionStatus,
    IncidentCandidate,
    IncidentRecord,
    IncidentStatus,
    ObservationRecord,
)
from agent.models.plan import Plan, PlanRecord
from agent.models.state_machine import IncidentStateMachine
from agent.observer.observer import Observation, ObservationsBundle


DEFAULT_DB_PATH = Path(".data/incidents.sqlite3")
ACTIVE_INCIDENT_STATUSES = (
    IncidentStatus.OPEN,
    IncidentStatus.PLANNED,
    IncidentStatus.IN_PROGRESS,
    IncidentStatus.FAILED,
    IncidentStatus.NEEDS_HUMAN,
)


class OptimisticLockError(RuntimeError):
    """Raised when a versioned state transition loses its compare-and-swap."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def encode_dt(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def decode_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def encode_json(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def decode_json(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return json.loads(value)


class SQLiteStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)

    @classmethod
    def from_env(cls) -> SQLiteStore:
        return cls(os.environ.get("INCIDENT_DB_PATH", str(DEFAULT_DB_PATH)))

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            self._create_schema(conn)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        statuses = ",".join(f"'{status.value}'" for status in IncidentStatus)
        conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS incidents (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ({statuses})),
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 0,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_incidents_status
            ON incidents(status);

            -- Partial unique index: only one active (non-terminal) incident
            -- per type may exist at a time.  This closes the TOCTOU window
            -- between the SELECT and INSERT in create_incident_from_candidate_if_absent.
            CREATE UNIQUE INDEX IF NOT EXISTS uq_incidents_type_active
            ON incidents(type)
            WHERE status IN ('OPEN','PLANNED','IN_PROGRESS','FAILED','NEEDS_HUMAN');

            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                source TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (incident_id) REFERENCES incidents(id)
            );

            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id TEXT NOT NULL,
                plan_id INTEGER,
                step_index INTEGER NOT NULL,
                tool TEXT NOT NULL,
                params_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('STARTED','SUCCEEDED','FAILED','SKIPPED')),
                started_at TEXT NOT NULL,
                finished_at TEXT,
                result_json TEXT,
                error_json TEXT,
                FOREIGN KEY (incident_id) REFERENCES incidents(id),
                FOREIGN KEY (plan_id) REFERENCES plans(id)
            );

            CREATE INDEX IF NOT EXISTS idx_actions_incident_id
            ON actions(incident_id);
            """
        )

    def record_observations(self, observations: Iterable[Observation]) -> int:
        rows = [
            (
                encode_dt(observation.ts),
                observation.source,
                observation.kind,
                encode_json(observation.payload),
            )
            for observation in observations
        ]
        if not rows:
            return 0

        with self.connection() as conn:
            conn.executemany(
                """
                INSERT INTO observations (ts, source, kind, payload_json)
                VALUES (?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    # ------------------------------------------------------------------
    # Atomic observe → detect → persist pipeline
    # ------------------------------------------------------------------

    def observe_and_persist_atomic(
        self,
        bundle: ObservationsBundle,
    ) -> tuple[int, list[IncidentRecord]]:
        """Record *bundle.observations* and persist any detected incident
        candidates in a **single SQLite transaction**.

        This eliminates the TOCTOU window that existed when
        ``record_observations`` and ``create_incident_from_candidate_if_absent``
        were called as separate operations (two connections, two commits).  A
        crash between the two old calls could leave observations written but no
        incident created, or — after a mid-INSERT failure — leave the DB in an
        inconsistent state with no clear recovery path.

        Returns
        -------
        (observation_count, created_incidents)
            ``observation_count`` is the number of observation rows inserted.
            ``created_incidents`` is the list of newly-created
            :class:`IncidentRecord` objects (empty when all candidates already
            have an active incident).
        """
        candidates: list[IncidentCandidate] = _detect_candidates(bundle)

        obs_rows = [
            (
                encode_dt(obs.ts),
                obs.source,
                obs.kind,
                encode_json(obs.payload),
            )
            for obs in bundle.observations
        ]

        # Prepare incident rows so all uuid generation happens before the
        # transaction opens — keeps the critical section as short as possible.
        now = encode_dt(utc_now())
        candidate_rows = [
            (
                str(uuid.uuid4()),  # id
                candidate.type.value,  # type
                IncidentStatus.OPEN.value,
                candidate.summary,
                now,  # created_at
                now,  # updated_at
                encode_json({"evidence": candidate.evidence}),
            )
            for candidate in candidates
        ]

        created_ids: list[str] = []

        with self.connection() as conn:
            # 1. Bulk-insert observations (idempotent autoincrement rows).
            if obs_rows:
                conn.executemany(
                    """
                    INSERT INTO observations (ts, source, kind, payload_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    obs_rows,
                )

            # 2. Attempt to INSERT each candidate; the partial UNIQUE index on
            #    incidents(type) WHERE status IN active statuses makes conflicts
            #    silent via INSERT OR IGNORE — same guard as the standalone
            #    method.  Both steps share the same connection/transaction so
            #    the commit is atomic across all rows.
            for incident_id, row in zip([r[0] for r in candidate_rows], candidate_rows):
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO incidents (
                        id, type, status, summary, created_at, updated_at,
                        version, attempt_count, last_error_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)
                    """,
                    row,
                )
                if cursor.rowcount == 1:
                    created_ids.append(incident_id)

        # Read-back is done *after* the transaction commits so the rows are
        # visible to any concurrent reader.
        created: list[IncidentRecord] = []
        for incident_id in created_ids:
            record = self.get_incident(incident_id)
            if record is None:
                raise RuntimeError(f"created incident {incident_id} could not be read back")
            created.append(record)

        return len(obs_rows), created

    def list_observations(self, limit: int = 100) -> list[ObservationRecord]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM observations
                ORDER BY ts DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_observation(row) for row in rows]

    def create_plan(self, plan: Plan) -> PlanRecord:
        now = encode_dt(utc_now())
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO plans (incident_id, plan_json, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    plan.incident_id,
                    encode_json(plan.model_dump(mode="json")),
                    now,
                ),
            )
            row_id = cursor.lastrowid
        plan_record = self.get_plan(row_id)
        if plan_record is None:
            raise RuntimeError("created plan could not be read back")
        return plan_record

    def get_plan(self, plan_id: int) -> PlanRecord | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_plan(row)

    def latest_plan_for_incident(self, incident_id: str) -> PlanRecord | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM plans
                WHERE incident_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (incident_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_plan(row)

    def create_incident(
        self,
        incident_type: str,
        summary: str,
        status: IncidentStatus = IncidentStatus.OPEN,
        last_error_json: dict[str, Any] | None = None,
    ) -> IncidentRecord:
        now = encode_dt(utc_now())
        incident_id = str(uuid.uuid4())
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO incidents (
                    id, type, status, summary, created_at, updated_at,
                    version, attempt_count, last_error_json
                )
                VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)
                """,
                (
                    incident_id,
                    incident_type,
                    status.value,
                    summary,
                    now,
                    now,
                    encode_json(last_error_json),
                ),
            )
        incident = self.get_incident(incident_id)
        if incident is None:
            raise RuntimeError("created incident could not be read back")
        return incident

    def create_incident_from_candidate_if_absent(
        self,
        candidate: IncidentCandidate,
    ) -> IncidentRecord | None:
        now = encode_dt(utc_now())
        incident_id = str(uuid.uuid4())
        with self.connection() as conn:
            # Atomic INSERT OR IGNORE: the partial UNIQUE index on
            # incidents(type) WHERE status IN active statuses ensures that at
            # most one concurrent worker wins the INSERT race.  The loser
            # silently ignores the conflict and returns None.
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO incidents (
                    id, type, status, summary, created_at, updated_at,
                    version, attempt_count, last_error_json
                )
                VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)
                """,
                (
                    incident_id,
                    candidate.type.value,
                    IncidentStatus.OPEN.value,
                    candidate.summary,
                    now,
                    now,
                    encode_json({"evidence": candidate.evidence}),
                ),
            )
            if cursor.rowcount == 0:
                # Conflict: an active incident of this type already exists.
                return None

        incident = self.get_incident(incident_id)
        if incident is None:
            raise RuntimeError("created incident could not be read back")
        return incident

    def list_incidents(self, status: IncidentStatus | None = None) -> list[IncidentRecord]:
        with self.connection() as conn:
            if status is None:
                rows = conn.execute(
                    """
                    SELECT * FROM incidents
                    ORDER BY created_at DESC, id DESC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM incidents
                    WHERE status = ?
                    ORDER BY created_at DESC, id DESC
                    """,
                    (status.value,),
                ).fetchall()
        return [self._row_to_incident(row) for row in rows]

    def list_incidents_by_statuses(
        self, statuses: Iterable[IncidentStatus]
    ) -> list[IncidentRecord]:
        status_values = [status.value for status in statuses]
        if not status_values:
            return []
        placeholders = ",".join("?" for _ in status_values)
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM incidents
                WHERE status IN ({placeholders})
                ORDER BY created_at ASC, id ASC
                """,
                status_values,
            ).fetchall()
        return [self._row_to_incident(row) for row in rows]

    def get_incident(self, incident_id: str) -> IncidentRecord | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM incidents WHERE id = ?",
                (incident_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_incident(row)

    def transition_incident(
        self,
        incident_id: str,
        *,
        from_status: IncidentStatus,
        to_status: IncidentStatus,
        expected_version: int,
        last_error_json: dict[str, Any] | None = None,
        increment_attempt: bool = False,
    ) -> IncidentRecord:
        IncidentStateMachine.assert_can_transition(from_status, to_status)
        now = encode_dt(utc_now())
        with self.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE incidents
                SET status = ?,
                    updated_at = ?,
                    version = version + 1,
                    attempt_count = attempt_count + ?,
                    last_error_json = ?
                WHERE id = ?
                  AND status = ?
                  AND version = ?
                """,
                (
                    to_status.value,
                    now,
                    1 if increment_attempt else 0,
                    encode_json(last_error_json),
                    incident_id,
                    from_status.value,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise OptimisticLockError(
                    "incident transition lost optimistic lock or status precondition"
                )
        incident = self.get_incident(incident_id)
        if incident is None:
            raise RuntimeError("transitioned incident could not be read back")
        return incident

    def mark_in_progress_needs_human(self) -> int:
        # Validate via state machine before touching the DB.
        IncidentStateMachine.assert_can_transition(
            IncidentStatus.IN_PROGRESS, IncidentStatus.NEEDS_HUMAN
        )
        now = encode_dt(utc_now())
        error_json = encode_json(
            {
                "failure_reason": "agent_restarted_while_in_progress",
                "message": "Incident was IN_PROGRESS during agent startup.",
            }
        )
        with self.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE incidents
                SET status = ?,
                    updated_at = ?,
                    version = version + 1,
                    last_error_json = COALESCE(last_error_json, ?)
                WHERE status = ?
                """,
                (
                    IncidentStatus.NEEDS_HUMAN.value,
                    now,
                    error_json,
                    IncidentStatus.IN_PROGRESS.value,
                ),
            )
            return cursor.rowcount

    def list_actions(self, incident_id: str | None = None) -> list[ActionRecord]:
        with self.connection() as conn:
            if incident_id is None:
                rows = conn.execute(
                    """
                    SELECT * FROM actions
                    ORDER BY started_at DESC, id DESC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM actions
                    WHERE incident_id = ?
                    ORDER BY started_at ASC, id ASC
                    """,
                    (incident_id,),
                ).fetchall()
        return [self._row_to_action(row) for row in rows]

    def record_action_started(
        self,
        *,
        incident_id: str,
        step_index: int,
        tool: str,
        params_json: dict[str, Any],
        plan_id: int | None = None,
    ) -> ActionRecord:
        now = encode_dt(utc_now())
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO actions (
                    incident_id, plan_id, step_index, tool, params_json,
                    status, started_at
                )
                VALUES (?, ?, ?, ?, ?, 'STARTED', ?)
                """,
                (incident_id, plan_id, step_index, tool, encode_json(params_json), now),
            )
            row_id = cursor.lastrowid
        return self._get_action(row_id)

    def finish_action(
        self,
        action_id: int,
        *,
        status: ActionStatus | str,
        result_json: dict[str, Any] | None = None,
        error_json: dict[str, Any] | None = None,
    ) -> ActionRecord:
        action_status = ActionStatus(status)
        now = encode_dt(utc_now())
        with self.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE actions
                SET status = ?,
                    finished_at = ?,
                    result_json = ?,
                    error_json = ?
                WHERE id = ?
                """,
                (
                    action_status.value,
                    now,
                    encode_json(result_json),
                    encode_json(error_json),
                    action_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"action not found: {action_id}")
        return self._get_action(action_id)

    def _get_action(self, action_id: int) -> ActionRecord:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"action {action_id} could not be read back")
        return self._row_to_action(row)

    def _row_to_incident(self, row: sqlite3.Row) -> IncidentRecord:
        return IncidentRecord(
            id=row["id"],
            type=row["type"],
            status=IncidentStatus(row["status"]),
            summary=row["summary"],
            created_at=decode_dt(row["created_at"]),
            updated_at=decode_dt(row["updated_at"]),
            version=row["version"],
            attempt_count=row["attempt_count"],
            last_error_json=decode_json(row["last_error_json"]),
        )

    def _row_to_observation(self, row: sqlite3.Row) -> ObservationRecord:
        return ObservationRecord(
            id=row["id"],
            ts=decode_dt(row["ts"]),
            source=row["source"],
            kind=row["kind"],
            payload_json=decode_json(row["payload_json"]) or {},
        )

    def _row_to_plan(self, row: sqlite3.Row) -> PlanRecord:
        return PlanRecord(
            id=row["id"],
            incident_id=row["incident_id"],
            plan_json=decode_json(row["plan_json"]) or {},
            created_at=decode_dt(row["created_at"]),
        )

    def _row_to_action(self, row: sqlite3.Row) -> ActionRecord:
        return ActionRecord(
            id=row["id"],
            incident_id=row["incident_id"],
            plan_id=row["plan_id"],
            step_index=row["step_index"],
            tool=row["tool"],
            params_json=decode_json(row["params_json"]) or {},
            status=ActionStatus(row["status"]),
            started_at=decode_dt(row["started_at"]),
            finished_at=decode_dt(row["finished_at"]),
            result_json=decode_json(row["result_json"]),
            error_json=decode_json(row["error_json"]),
        )
