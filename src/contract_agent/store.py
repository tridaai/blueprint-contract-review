"""Record store: where analyses and counsel decisions live.

**PostgreSQL is the system of record.** ``PostgresStore`` keeps one
row per contract in ``contract_records`` (schema in
``migrations/0001_contract_records.sql``, applied at startup by
``migrations.py``): the full result + contract payload as JSONB, so a
counsel decision — and the counterparty memory built from earlier
contracts — survives restarts and is shared across API replicas.
Selected with ``DATABASE_URL`` (see ``default_store``).

``InMemoryStore`` is a TEST DOUBLE: it exists for the hermetic test
suite and the offline demo, and is never the documented runtime.
Both implement the same ``Store`` protocol (``ports.py``).

The store is also the memory source: ``prior_contracts`` feeds the
assessment evidence (same counterparty) and the feedback loop
(counsel decisions with their reasons teach later runs).
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field

from .config import env_str, load_dotenv
from .schemas import AgentResult


@dataclass
class ApprovalRecord:
    result: AgentResult
    contract: dict = field(default_factory=dict)
    approved: bool = False
    approver: str = ""
    approve_reason: str = ""
    rejected_by: str = ""
    reject_reason: str = ""
    dispatch_status: str | None = None
    # Queue ownership (round 5): who on counsel's team holds this
    # contract while it awaits a decision. Set by an operator via
    # the service's assign(); recorded, never acted on by the agent.
    assignee: str = ""
    assigned_by: str = ""
    assigned_at: str = ""


def history_entry(record: ApprovalRecord) -> dict:
    """The compact history projection of one analysed contract."""
    result = record.result
    contract = record.contract or {}
    return {
        "contract_id": result.contract_id,
        "counterparty_name": result.counterparty_name or contract.get("counterparty_name", ""),
        "doc_type": result.doc_type or contract.get("doc_type", ""),
        "posture": result.facts.posture if result.facts else "",
        "worst_position": result.facts.worst_position if result.facts else "",
        "decision": result.decision or "",
    }


def feedback_entry(record: ApprovalRecord) -> dict | None:
    """A decided record as a feedback entry, or None when undecided
    (or decided without a reason — feedback without a reason teaches
    nothing)."""
    result = record.result
    if result.status not in ("approved", "rejected") or not result.decision_reason:
        return None
    contract = record.contract or {}
    return {
        "contract_id": result.contract_id,
        "counterparty_name": result.counterparty_name or contract.get("counterparty_name", ""),
        "decision": result.status,
        "reason": result.decision_reason,
    }


def counterparty_summary(priors: list[dict], counterparty: str) -> dict:
    """Posture counts for one counterparty across prior contracts."""
    hits = [e for e in priors if counterparty and e.get("counterparty_name") == counterparty]
    counts: dict[str, int] = {}
    for entry in hits:
        if entry.get("posture"):
            counts[entry["posture"]] = counts.get(entry["posture"], 0) + 1
    return {
        "counterparty": counterparty,
        "counterparty_count": len(hits),
        "counterparty_posture_counts": counts,
        "counterparty_recent_postures": [e.get("posture", "") for e in hits[:3]],
    }


def format_position_counts(counts: dict[str, int]) -> str:
    """{'preferred': 2, 'walk_away': 1} -> '2 preferred, 1 walk_away'."""
    return ", ".join(f"{n} {name}" for name, n in sorted(counts.items())) or "none"


def _dump_record(record: ApprovalRecord) -> str:
    return json.dumps(
        {
            "result": record.result.model_dump(mode="json"),
            "contract": record.contract,
            "approved": record.approved,
            "approver": record.approver,
            "approve_reason": record.approve_reason,
            "rejected_by": record.rejected_by,
            "reject_reason": record.reject_reason,
            "dispatch_status": record.dispatch_status,
            "assignee": record.assignee,
            "assigned_by": record.assigned_by,
            "assigned_at": record.assigned_at,
        }
    )


def _load_record(payload: str | dict) -> ApprovalRecord:
    data = json.loads(payload) if isinstance(payload, str) else payload
    return ApprovalRecord(
        result=AgentResult.model_validate(data["result"]),
        contract=data.get("contract", {}),
        approved=data.get("approved", False),
        approver=data.get("approver", ""),
        approve_reason=data.get("approve_reason", ""),
        rejected_by=data.get("rejected_by", ""),
        reject_reason=data.get("reject_reason", ""),
        dispatch_status=data.get("dispatch_status"),
        assignee=data.get("assignee", ""),
        assigned_by=data.get("assigned_by", ""),
        assigned_at=data.get("assigned_at", ""),
    )


class _BaseStore:
    """The read-side history/feedback projections, shared by both stores."""

    def _all(self) -> list[ApprovalRecord]:  # pragma: no cover - overridden
        raise NotImplementedError

    def prior_contracts(self, exclude_contract_id: str | None = None) -> list[dict]:
        return [
            history_entry(r)
            for r in self._all()
            if r.result.contract_id != exclude_contract_id
        ]

    def decision_feedback(self, exclude_contract_id: str | None = None) -> list[dict]:
        entries = []
        for record in reversed(self._all()):
            if record.result.contract_id == exclude_contract_id:
                continue
            entry = feedback_entry(record)
            if entry is not None:
                entries.append(entry)
        return entries

    def all_records(self) -> list[ApprovalRecord]:
        """Every record in the store — the audit-export and metrics
        read path (the decision-of-record set, unfiltered)."""
        return self._all()


class InMemoryStore(_BaseStore):
    """Process-local store — a test double for the hermetic suite and
    the offline demo. Thread-safe. Never the documented runtime."""

    def __init__(self) -> None:
        self._records: dict[str, ApprovalRecord] = {}
        self._lock = threading.Lock()

    def save(self, record: ApprovalRecord) -> None:
        with self._lock:
            self._records[record.result.contract_id] = record

    def get(self, contract_id: str) -> ApprovalRecord | None:
        with self._lock:
            return self._records.get(contract_id)

    def _all(self) -> list[ApprovalRecord]:
        with self._lock:
            return list(self._records.values())


class PostgresStore(_BaseStore):
    """PostgreSQL-backed store (the production system of record).

    One short-lived connection per call, so concurrent batch writers
    and multiple API replicas share the same record set. The schema is
    owned by ``migrations/`` and applied on construction.
    """

    def __init__(self, database_url: str) -> None:
        import psycopg  # local import: the double must work without the driver

        self._url = database_url
        self._psycopg = psycopg
        with self._connect() as conn:
            from .migrations import run_migrations

            run_migrations(conn)

    def _connect(self):
        return self._psycopg.connect(self._url)

    def save(self, record: ApprovalRecord) -> None:
        decided = record.result.status in ("approved", "rejected")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO contract_records (contract_id, payload, decided, updated_at)
                    VALUES (%s, %s::jsonb, %s, now())
                    ON CONFLICT (contract_id) DO UPDATE
                    SET payload = EXCLUDED.payload,
                        decided = EXCLUDED.decided,
                        updated_at = now()
                    """,
                    (record.result.contract_id, _dump_record(record), decided),
                )
            conn.commit()

    def get(self, contract_id: str) -> ApprovalRecord | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT payload FROM contract_records WHERE contract_id = %s",
                    (contract_id,),
                )
                row = cur.fetchone()
        return _load_record(row[0]) if row else None

    def _all(self) -> list[ApprovalRecord]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT payload FROM contract_records ORDER BY updated_at"
                )
                rows = cur.fetchall()
        return [_load_record(row[0]) for row in rows]

    def ping(self) -> bool:
        """Readiness probe: the database answers a trivial query."""
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            return True
        except Exception:
            return False


def default_store() -> InMemoryStore | PostgresStore:
    """The store for this process: PostgreSQL when ``DATABASE_URL``
    is set (the production default — compose and every real deployment
    set it); the in-memory test double only when it is not (offline
    demo, tests)."""
    load_dotenv()
    url = env_str("DATABASE_URL", "")
    if url:
        return PostgresStore(url)
    return InMemoryStore()
