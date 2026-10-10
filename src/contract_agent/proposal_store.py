"""Proposal store: where drafted playbook proposals wait for counsel.

A proposal is not a contract record — it is a candidate change to
the playbook itself — so it gets its own small store with the same
posture as ``store.py``: **PostgreSQL is the system of record**
(``playbook_proposals`` table, migration
``0003_playbook_proposals.sql``; the full proposal as JSONB), and
``InMemoryProposalStore`` is the test double for the hermetic suite
and the offline demo. The service registers ACTIVATED proposals'
entries into ``playbook_registry`` when it resolves this store, so
activations survive restarts through the store, not the process.
"""

from __future__ import annotations

import json
import threading
from typing import Protocol

from .config import env_str, load_dotenv
from .schemas import PlaybookProposal


class PlaybookProposalStore(Protocol):
    def save(self, proposal: PlaybookProposal) -> None: ...
    def get(self, proposal_id: str) -> PlaybookProposal | None: ...
    def all(self) -> list[PlaybookProposal]: ...


class InMemoryProposalStore:
    """Process-local proposal store — a test double. Thread-safe."""

    def __init__(self) -> None:
        self._proposals: dict[str, PlaybookProposal] = {}
        self._lock = threading.Lock()

    def save(self, proposal: PlaybookProposal) -> None:
        with self._lock:
            self._proposals[proposal.proposal_id] = proposal

    def get(self, proposal_id: str) -> PlaybookProposal | None:
        with self._lock:
            return self._proposals.get(proposal_id)

    def all(self) -> list[PlaybookProposal]:
        with self._lock:
            return list(self._proposals.values())


class PostgresProposalStore:
    """PostgreSQL-backed proposal store. One short-lived connection
    per call, like the record store; the schema is owned by
    ``migrations/`` and applied on construction."""

    def __init__(self, database_url: str) -> None:
        import psycopg  # local import: the double must work without the driver

        self._url = database_url
        self._psycopg = psycopg
        with self._connect() as conn:
            from .migrations import run_migrations

            run_migrations(conn)

    def _connect(self):
        return self._psycopg.connect(self._url)

    def save(self, proposal: PlaybookProposal) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO playbook_proposals (proposal_id, clause_type, status, payload, updated_at)
                    VALUES (%s, %s, %s, %s::jsonb, now())
                    ON CONFLICT (proposal_id) DO UPDATE
                    SET clause_type = EXCLUDED.clause_type,
                        status = EXCLUDED.status,
                        payload = EXCLUDED.payload,
                        updated_at = now()
                    """,
                    (
                        proposal.proposal_id,
                        proposal.clause_type,
                        proposal.status,
                        json.dumps(proposal.model_dump(mode="json")),
                    ),
                )
            conn.commit()

    def get(self, proposal_id: str) -> PlaybookProposal | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT payload FROM playbook_proposals WHERE proposal_id = %s",
                    (proposal_id,),
                )
                row = cur.fetchone()
        if not row:
            return None
        data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        return PlaybookProposal.model_validate(data)

    def all(self) -> list[PlaybookProposal]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT payload FROM playbook_proposals ORDER BY updated_at"
                )
                rows = cur.fetchall()
        return [
            PlaybookProposal.model_validate(
                json.loads(row[0]) if isinstance(row[0], str) else row[0]
            )
            for row in rows
        ]


def default_proposal_store() -> InMemoryProposalStore | PostgresProposalStore:
    """PostgreSQL when ``DATABASE_URL`` is set (the production
    default); the in-memory double only when it is not."""
    load_dotenv()
    url = env_str("DATABASE_URL", "")
    if url:
        return PostgresProposalStore(url)
    return InMemoryProposalStore()
