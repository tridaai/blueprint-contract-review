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

**Retention** (round 15): a ledger that only grows becomes a
ledger nobody reads. Delivered entries are history once they
are old: past ``WEBHOOK_OUTBOX_RETENTION_DAYS`` (default 90) a
SENT entry may be pruned — by a named operator's deliberate act
(``ContractService.prune_webhook_outbox``), never on a timer
and never as a side effect of a read. FAILED entries are never
pruned, by the prune or anything else: a failure leaves the
ledger only by being repaired (a re-send that lands) and then
ageing out as a sent entry like any other. And a failure that
sits unrepaired past ``WEBHOOK_OUTBOX_STALE_FAILURE_DAYS``
(default 7) is **stale** — surfaced as a count and a worklist
on the outbox view, in ``/metrics``, and on the counsel queue,
so "a delivery died last week and nobody noticed" is a number
legal ops manages to, in the queue-SLA spirit.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from typing import Protocol

from .config import env_int, env_str, load_dotenv
from .schemas import WebhookAttempt, WebhookOutboxEntry

RETENTION_ENV = "WEBHOOK_OUTBOX_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 90
STALE_FAILURE_ENV = "WEBHOOK_OUTBOX_STALE_FAILURE_DAYS"
DEFAULT_STALE_FAILURE_DAYS = 7


def outbox_retention_days() -> int:
    """How old a SENT entry must be before the prune may take
    it, in days. Env-tunable; a negative value disables pruning
    of sent entries too (nothing ages out)."""
    return env_int(RETENTION_ENV, DEFAULT_RETENTION_DAYS)


def outbox_stale_failure_days() -> int:
    """How long a FAILED entry may sit unrepaired before it
    reads as stale, in days."""
    return env_int(STALE_FAILURE_ENV, DEFAULT_STALE_FAILURE_DAYS)


class OutboxStore(Protocol):
    def save(self, entry: WebhookOutboxEntry) -> None: ...
    def get(self, outbox_id: str) -> WebhookOutboxEntry | None: ...
    def all(self) -> list[WebhookOutboxEntry]: ...
    def delete(self, outbox_id: str) -> bool: ...


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

    def delete(self, outbox_id: str) -> bool:
        with self._lock:
            return self._entries.pop(outbox_id, None) is not None


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

    def delete(self, outbox_id: str) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM webhook_outbox WHERE outbox_id = %s",
                    (outbox_id,),
                )
                removed = cur.rowcount
            conn.commit()
        return removed > 0


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


# ---------------------------------------------------------------------------
# Retention + staleness (round 15): the governed ageing of the ledger
# ---------------------------------------------------------------------------


def _stamp(value: str) -> datetime | None:
    """An entry's ISO stamp as a datetime, or None when it does
    not parse — an unstamped entry has no age, and no age means
    never prunable and never stale: the policy fails closed."""
    try:
        parsed = datetime.fromisoformat(value or "")
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def entry_age_days(
    entry: WebhookOutboxEntry, now: datetime | None = None
) -> float | None:
    """The entry's age in days, measured from its LAST attempt
    (``updated_at`` — a re-send refreshes the entry, so its age
    is the age of its current state, not of the original
    dispatch). None when the stamp does not parse."""
    stamp = _stamp(entry.updated_at) or _stamp(entry.created_at)
    if stamp is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - stamp).total_seconds() / 86400.0)


def stale_failure_rows(
    entries: list[WebhookOutboxEntry],
    now: datetime | None = None,
    threshold_days: int | None = None,
) -> list[dict]:
    """The failed entries that have sat unrepaired past the
    stale threshold, oldest first — the worklist behind the
    stale counts on the outbox view, ``/metrics``, and the
    counsel queue. A failure younger than the threshold is a
    delivery in trouble; an older one is a delivery nobody is
    minding, and the surfaces say which."""
    threshold = (
        outbox_stale_failure_days() if threshold_days is None else threshold_days
    )
    now = now or datetime.now(timezone.utc)
    rows: list[dict] = []
    for entry in entries:
        if entry.status != "failed":
            continue
        age = entry_age_days(entry, now)
        if age is None or age < threshold:
            continue
        rows.append(
            {
                "outbox_id": entry.outbox_id,
                "family": entry.family,
                "contract_id": entry.contract_id,
                "label": entry.label,
                "age_days": round(age, 1),
                "attempts": len(entry.attempts),
                "last_attempt_at": entry.updated_at,
            }
        )
    rows.sort(key=lambda row: (-row["age_days"], row["outbox_id"]))
    return rows


def prunable_sent_entries(
    entries: list[WebhookOutboxEntry],
    now: datetime | None = None,
    retention_days: int | None = None,
) -> list[WebhookOutboxEntry]:
    """The entries the retention rule may take: SENT, and older
    than the retention age. Failed entries are never in this
    list — the prune cannot spend a failure, however old; a
    negative retention age disables the rule entirely."""
    retention = (
        outbox_retention_days() if retention_days is None else retention_days
    )
    if retention < 0:
        return []
    now = now or datetime.now(timezone.utc)
    prunable: list[WebhookOutboxEntry] = []
    for entry in entries:
        if entry.status != "sent":
            continue
        age = entry_age_days(entry, now)
        if age is not None and age >= retention:
            prunable.append(entry)
    prunable.sort(key=lambda e: (e.created_at, e.outbox_id))
    return prunable
