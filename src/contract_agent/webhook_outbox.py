"""Webhook outbox: the one delivery ledger for signed dispatches.

Three signed families leave the building through the webhook
seam (``service.py``): approved packets, queue SLA breaches,
and cross-round drift findings. Before round 14 their outcomes
lived scattered — a status field on the decision record, a
stamp on the breached record, a dispatch list on the analysis
result — and a delivery that failed could only be recovered by
re-running whatever produced it: re-deciding a packet that was
already decided (impossible), re-analysing a contract to
regenerate a drift event. The outbox ends that: every dispatch
is recorded here as it happens, with the EXACT payload that was
posted, and a failed entry is repaired by a deliberate,
recorded re-send of that stored payload (``ContractService.
resend_webhook``) — the event is never regenerated.

Same posture as the other small stores: **PostgreSQL is the
system of record** (``webhook_outbox`` table, migration
``0005_webhook_outbox.sql``; the full entry as JSONB), and
``InMemoryOutboxStore`` is the test double for the hermetic
suite and the offline demo.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from typing import Protocol

from .config import env_str, load_dotenv
from .schemas import WebhookAttempt, WebhookOutboxEntry


class OutboxStore(Protocol):
    def save(self, entry: WebhookOutboxEntry) -> None: ...
    def get(self, outbox_id: str) -> WebhookOutboxEntry | None: ...
    def all(self) -> list[WebhookOutboxEntry]: ...


class InMemoryOutboxStore:
    """Process-local outbox store — a test double. Thread-safe."""

    def __init__(self) -> None:
        self._entries: dict[str, WebhookOutboxEntry] = {}
        self._lock = threading.Lock()

    def save(self, entry: WebhookOutboxEntry) -> None:
        with self._lock:
            self._entries[entry.outbox_id] = entry

    def get(self, outbox_id: str) -> WebhookOutboxEntry | None:
        with self._lock:
            return self._entries.get(outbox_id)

    def all(self) -> list[WebhookOutboxEntry]:
        with self._lock:
            return list(self._entries.values())


class PostgresOutboxStore:
    """PostgreSQL-backed outbox store. One short-lived connection
    per call, like the other stores; the schema is owned by
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

    def save(self, entry: WebhookOutboxEntry) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO webhook_outbox (outbox_id, family, contract_id, status, payload, updated_at)
                    VALUES (%s, %s, %s, %s, %s::jsonb, now())
                    ON CONFLICT (outbox_id) DO UPDATE
                    SET family = EXCLUDED.family,
                        contract_id = EXCLUDED.contract_id,
                        status = EXCLUDED.status,
                        payload = EXCLUDED.payload,
                        updated_at = now()
                    """,
                    (
                        entry.outbox_id,
                        entry.family,
                        entry.contract_id,
                        entry.status,
                        json.dumps(entry.model_dump(mode="json")),
                    ),
                )
            conn.commit()

    def get(self, outbox_id: str) -> WebhookOutboxEntry | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT payload FROM webhook_outbox WHERE outbox_id = %s",
                    (outbox_id,),
                )
                row = cur.fetchone()
        if not row:
            return None
        data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        return WebhookOutboxEntry.model_validate(data)

    def all(self) -> list[WebhookOutboxEntry]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT payload FROM webhook_outbox ORDER BY updated_at"
                )
                rows = cur.fetchall()
        return [
            WebhookOutboxEntry.model_validate(
                json.loads(row[0]) if isinstance(row[0], str) else row[0]
            )
            for row in rows
        ]


def default_outbox_store() -> InMemoryOutboxStore | PostgresOutboxStore:
    """PostgreSQL when ``DATABASE_URL`` is set (the production
    default); the in-memory double only when it is not."""
    load_dotenv()
    url = env_str("DATABASE_URL", "")
    if url:
        return PostgresOutboxStore(url)
    return InMemoryOutboxStore()


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or "entry"


def make_outbox_id(
    existing_ids: set[str], family: str, contract_id: str, label: str
) -> str:
    """A readable, unique outbox id: family, contract, and what
    was dispatched (``drift_finding-ctr-2037-subprocessor-list-
    drift``). The same triple dispatched twice — a contract
    re-analysed, its finding pushed again — takes a numeric
    suffix rather than overwriting the first delivery's record:
    both deliveries happened, and the ledger keeps both."""
    base = f"{_slug(family)}-{_slug(contract_id)}-{_slug(label)}"
    if base not in existing_ids:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing_ids:
        suffix += 1
    return f"{base}-{suffix}"


def record_dispatch(
    store: OutboxStore,
    *,
    family: str,
    contract_id: str,
    label: str,
    payload: dict,
    outcome: str,
) -> WebhookOutboxEntry:
    """Record one attempted dispatch in the outbox: the payload
    as posted, the outcome as the first attempt. Returns the
    stored entry."""
    now = datetime.now(timezone.utc).isoformat()
    entry = WebhookOutboxEntry(
        outbox_id=make_outbox_id(
            {e.outbox_id for e in store.all()}, family, contract_id, label
        ),
        family=family,
        contract_id=contract_id,
        label=label,
        payload=payload,
        status=outcome,
        attempts=[WebhookAttempt(at=now, outcome=outcome)],
        created_at=now,
        updated_at=now,
    )
    store.save(entry)
    return entry
