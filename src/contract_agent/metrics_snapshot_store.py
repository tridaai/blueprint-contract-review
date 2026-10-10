"""Metrics snapshot store: where kept metrics snapshots live.

A snapshot is an operator's act on the metrics surface, not a
contract record — so, like the calibration store, it gets its
own small store with the same posture: **PostgreSQL is the
system of record** (``metrics_snapshots`` table, migration
``0007_metrics_snapshots.sql``; the full snapshot as JSONB),
and ``InMemoryMetricsSnapshotStore`` is the test double for the
hermetic suite and the offline demo. One row per snapshot, in
kept order: the trend reads them oldest first.
"""

from __future__ import annotations

import json
import threading
from typing import Protocol

from .config import env_str, load_dotenv
from .schemas import MetricsSnapshot


class MetricsSnapshotStore(Protocol):
    def save(self, snapshot: MetricsSnapshot) -> None: ...
    def get(self, snapshot_id: str) -> MetricsSnapshot | None: ...
    def all(self) -> list[MetricsSnapshot]: ...


class InMemoryMetricsSnapshotStore:
    """Process-local snapshot store — a test double. Thread-safe."""

    def __init__(self) -> None:
        self._snapshots: dict[str, MetricsSnapshot] = {}
        self._lock = threading.Lock()

    def save(self, snapshot: MetricsSnapshot) -> None:
        with self._lock:
            self._snapshots[snapshot.snapshot_id] = snapshot

    def get(self, snapshot_id: str) -> MetricsSnapshot | None:
        with self._lock:
            return self._snapshots.get(snapshot_id)

    def all(self) -> list[MetricsSnapshot]:
        with self._lock:
            return sorted(
                self._snapshots.values(), key=lambda s: s.snapshot_id
            )


class PostgresMetricsSnapshotStore:
    """PostgreSQL-backed snapshot store. One short-lived
    connection per call, like the other stores; the schema is
    owned by ``migrations/`` and applied on construction."""

    def __init__(self, database_url: str) -> None:
        import psycopg  # local import: the double must work without the driver

        self._url = database_url
        self._psycopg = psycopg
        with self._connect() as conn:
            from .migrations import run_migrations

            run_migrations(conn)

    def _connect(self):
        return self._psycopg.connect(self._url)

    def save(self, snapshot: MetricsSnapshot) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO metrics_snapshots (snapshot_id, label, payload, kept_at)
                    VALUES (%s, %s, %s::jsonb, now())
                    ON CONFLICT (snapshot_id) DO UPDATE
                    SET label = EXCLUDED.label,
                        payload = EXCLUDED.payload
                    """,
                    (
                        snapshot.snapshot_id,
                        snapshot.label,
                        json.dumps(snapshot.model_dump(mode="json")),
                    ),
                )
            conn.commit()

    def get(self, snapshot_id: str) -> MetricsSnapshot | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT payload FROM metrics_snapshots WHERE snapshot_id = %s",
                    (snapshot_id,),
                )
                row = cur.fetchone()
        if not row:
            return None
        data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        return MetricsSnapshot.model_validate(data)

    def all(self) -> list[MetricsSnapshot]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT payload FROM metrics_snapshots ORDER BY snapshot_id"
                )
                rows = cur.fetchall()
        return [
            MetricsSnapshot.model_validate(
                json.loads(row[0]) if isinstance(row[0], str) else row[0]
            )
            for row in rows
        ]


def default_metrics_snapshot_store() -> (
    InMemoryMetricsSnapshotStore | PostgresMetricsSnapshotStore
):
    """PostgreSQL when ``DATABASE_URL`` is set (the production
    default); the in-memory double only when it is not."""
    load_dotenv()
    url = env_str("DATABASE_URL", "")
    if url:
        return PostgresMetricsSnapshotStore(url)
    return InMemoryMetricsSnapshotStore()
