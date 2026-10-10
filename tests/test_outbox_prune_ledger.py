"""The prune ledger (round 17): round 15 gave the outbox a
retention rule, and the prune's report was returned to the
caller and gone — the act that removes delivery history left
no record of itself, so the delivery ledger's own gaps were
unauditable. Every prune act now appends an entry to a durable
ledger beside the outbox: who pruned, when, the retention age
in force, the exact entry ids removed, and what was kept —
a prune that removed nothing included, because the ledger
records the act, not only its casualties. Nothing prunes the
prunes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore
from contract_agent.webhook_outbox import InMemoryOutboxStore, record_dispatch


def _service(outbox: InMemoryOutboxStore) -> ContractService:
    return ContractService(
        store=InMemoryStore(), checkpointer=False, outbox_store=outbox
    )


def _dispatch(outbox: InMemoryOutboxStore, *, outcome: str, age_days: float,
              contract_id: str = "CTR-PL-1"):
    """Record a dispatch and backdate it (the retention tests'
    idiom: the entry's age is the age of its last attempt)."""
    entry = record_dispatch(
        outbox,
        family="approval",
        contract_id=contract_id,
        label="counsel decision: approved",
        payload={"event": "approval", "contract_id": contract_id},
        outcome=outcome,
    )
    stamp = (
        datetime.now(timezone.utc) - timedelta(days=age_days)
    ).isoformat()
    entry.created_at = stamp
    entry.updated_at = stamp
    entry.attempts[0].at = stamp
    outbox.save(entry)
    return entry


def test_a_prune_appends_its_act_to_the_ledger():
    outbox = InMemoryOutboxStore()
    old = _dispatch(outbox, outcome="sent", age_days=120)
    _dispatch(outbox, outcome="sent", age_days=10, contract_id="CTR-PL-2")
    _dispatch(outbox, outcome="failed", age_days=120, contract_id="CTR-PL-3")
    svc = _service(outbox)
    report = svc.prune_webhook_outbox(actor="Ops Lead")
    assert report["pruned"] == [old.outbox_id]
    assert report["prune_id"] == "prune-1"
    ledger = svc.webhook_outbox_prunes()
    assert ledger["count"] == 1
    entry = ledger["prunes"][0]
    assert entry["prune_id"] == "prune-1"
    assert entry["actor"] == "Ops Lead"
    assert entry["retention_days"] == 90
    assert entry["pruned_ids"] == [old.outbox_id]
    assert entry["kept_sent"] == 1
    assert entry["kept_failed"] == 1
    assert entry["at"]


def test_a_prune_that_removes_nothing_is_still_recorded():
    outbox = InMemoryOutboxStore()
    _dispatch(outbox, outcome="sent", age_days=10)
    svc = _service(outbox)
    report = svc.prune_webhook_outbox(actor="Ops Lead")
    assert report["pruned_count"] == 0
    ledger = svc.webhook_outbox_prunes()
    assert ledger["count"] == 1
    assert ledger["prunes"][0]["pruned_ids"] == []
    assert ledger["prunes"][0]["kept_sent"] == 1


def test_the_ledger_accumulates_in_order_and_is_append_only():
    outbox = InMemoryOutboxStore()
    _dispatch(outbox, outcome="sent", age_days=120)
    svc = _service(outbox)
    svc.prune_webhook_outbox(actor="Ops Lead")
    svc.prune_webhook_outbox(actor="Second Operator")
    # A third prune takes nothing and removes nothing from the
    # ledger either: the earlier acts stay exactly as recorded.
    svc.prune_webhook_outbox(actor="Ops Lead")
    ledger = svc.webhook_outbox_prunes()
    assert ledger["count"] == 3
    assert [p["prune_id"] for p in ledger["prunes"]] == [
        "prune-1", "prune-2", "prune-3",
    ]
    assert [p["actor"] for p in ledger["prunes"]] == [
        "Ops Lead", "Second Operator", "Ops Lead",
    ]
    assert ledger["prunes"][0]["pruned_ids"] != []
    assert ledger["prunes"][1]["pruned_ids"] == []


def test_the_retention_age_in_force_is_the_one_recorded(monkeypatch):
    monkeypatch.setenv("WEBHOOK_OUTBOX_RETENTION_DAYS", "30")
    outbox = InMemoryOutboxStore()
    _dispatch(outbox, outcome="sent", age_days=45)
    svc = _service(outbox)
    svc.prune_webhook_outbox(actor="Ops Lead")
    entry = svc.webhook_outbox_prunes()["prunes"][0]
    assert entry["retention_days"] == 30
    assert len(entry["pruned_ids"]) == 1


def test_a_refused_prune_records_nothing():
    outbox = InMemoryOutboxStore()
    svc = _service(outbox)
    with pytest.raises(ValueError, match="named actor"):
        svc.prune_webhook_outbox(actor="  ")
    assert svc.webhook_outbox_prunes()["count"] == 0


def test_the_ledger_is_readable_over_the_api():
    outbox = InMemoryOutboxStore()
    _dispatch(outbox, outcome="sent", age_days=120)
    svc = _service(outbox)
    import contract_agent.api as api

    original = api.service
    api.service = svc
    try:
        client = TestClient(api.app)
        assert client.get("/webhooks/outbox/prunes").json()["count"] == 0
        pruned = client.post(
            "/webhooks/outbox/prune", json={"actor": "Ops Lead"}
        )
        assert pruned.status_code == 200
        assert pruned.json()["prune_id"] == "prune-1"
        ledger = client.get("/webhooks/outbox/prunes")
        assert ledger.status_code == 200
        body = ledger.json()
        assert body["count"] == 1
        assert body["prunes"][0]["actor"] == "Ops Lead"
        assert len(body["prunes"][0]["pruned_ids"]) == 1
    finally:
        api.service = original
