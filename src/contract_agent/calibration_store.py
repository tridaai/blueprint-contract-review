"""Calibration store: where pattern-term suspensions live.

A suspension is counsel's act on the scorer, not a contract record
— so, like the proposal store, it gets its own small store with
the same posture: **PostgreSQL is the system of record**
(``pattern_term_suspensions`` table, migration
``0004_pattern_term_suspensions.sql``; the full record as JSONB),
and ``InMemoryCalibrationStore`` is the test double for the
hermetic suite and the offline demo. One row per clause type: the
record carries the suspension and, when counsel reverses it, the
lift — both acts stay readable from the one row.
"""

from __future__ import annotations

import json
import threading
from typing import Protocol

from .config import env_str, load_dotenv
from .schemas import PatternTermSuspension


class CalibrationStore(Protocol):
    def save(self, suspension: PatternTermSuspension) -> None: ...
    def get(self, clause_type: str) -> PatternTermSuspension | None: ...
    def all(self) -> list[PatternTermSuspension]: ...


class InMemoryCalibrationStore:
    """Process-local calibration store — a test double. Thread-safe."""

    def __init__(self) -> None:
        self._suspensions: dict[str, PatternTermSuspension] = {}
        self._lock = threading.Lock()

    def save(self, suspension: PatternTermSuspension) -> None:
        with self._lock:
            self._suspensions[suspension.clause_type] = suspension

    def get(self, clause_type: str) -> PatternTermSuspension | None:
        with self._lock:
            return self._suspensions.get(clause_type)

    def all(self) -> list[PatternTermSuspension]:
        with self._lock:
            return list(self._suspensions.values())


class PostgresCalibrationStore:
    """PostgreSQL-backed calibration store. One short-lived
    connection per call, like the other stores; the schema is owned
    by ``migrations/`` and applied on construction."""

    def __init__(self, database_url: str) -> None:
        import psycopg  # local import: the double must work without the driver

        self._url = database_url
        self._psycopg = psycopg
        with self._connect() as conn:
            from .migrations import run_migrations

            run_migrations(conn)

    def _connect(self):
        return self._psycopg.connect(self._url)

    def save(self, suspension: PatternTermSuspension) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO pattern_term_suspensions (clause_type, status, payload, updated_at)
                    VALUES (%s, %s, %s::jsonb, now())
                    ON CONFLICT (clause_type) DO UPDATE
                    SET status = EXCLUDED.status,
                        payload = EXCLUDED.payload,
                        updated_at = now()
                    """,
                    (
                        suspension.clause_type,
                        suspension.status,
                        json.dumps(suspension.model_dump(mode="json")),
                    ),
                )
            conn.commit()

    def get(self, clause_type: str) -> PatternTermSuspension | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT payload FROM pattern_term_suspensions WHERE clause_type = %s",
                    (clause_type,),
                )
                row = cur.fetchone()
        if not row:
            return None
        data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        return PatternTermSuspension.model_validate(data)

    def all(self) -> list[PatternTermSuspension]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT payload FROM pattern_term_suspensions ORDER BY updated_at"
                )
                rows = cur.fetchall()
        return [
            PatternTermSuspension.model_validate(
                json.loads(row[0]) if isinstance(row[0], str) else row[0]
            )
            for row in rows
        ]


def default_calibration_store() -> InMemoryCalibrationStore | PostgresCalibrationStore:
    """PostgreSQL when ``DATABASE_URL`` is set (the production
    default); the in-memory double only when it is not."""
    load_dotenv()
    url = env_str("DATABASE_URL", "")
    if url:
        return PostgresCalibrationStore(url)
    return InMemoryCalibrationStore()
