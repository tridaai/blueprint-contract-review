"""Outbox retention and stale-failure surfacing (round 15):
round 14 made the outbox the one delivery ledger, but a ledger
that only grows becomes a ledger nobody reads, and a failed
entry nobody re-sends just sat there. The retention rule is
governed: SENT entries older than WEBHOOK_OUTBOX_RETENTION_DAYS
(default 90) may be pruned — by a named operator's deliberate
act, reported with the names of what it took — while FAILED
entries are never pruned by anything. And a failure left
unrepaired past WEBHOOK_OUTBOX_STALE_FAILURE_DAYS (default 7)
reads as stale: counted on the outbox view and in /metrics,
and listed on the counsel queue beside the suspension alerts,
so a delivery that died last week is a number legal ops
manages to.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore
from contract_agent.webhook_outbox import (
    InMemoryOutboxStore,
    entry_age_days,
    prunable_sent_entries,
    record_dispatch,
    stale_failure_rows,
)


def _service(outbox: InMemoryOutboxStore) -> ContractService:
    return ContractService(
        store=InMemoryStore(), checkpointer=False, outbox_store=outbox
    )


def _dispatch(outbox: InMemoryOutboxStore, *, outcome: str, age_days: float,
              contract_id: str = "CTR-RET-1", family: str = "approval"):
    """Record a dispatch and backdate it: the entry's age is
    the age of its last attempt, so both stamps move."""
    entry = record_dispatch(
        outbox,
        family=family,
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


# ---------------------------------------------------------------------------
# Staleness: a failure's age against the threshold
# ---------------------------------------------------------------------------


def test_a_failure_past_the_threshold_is_stale_a_fresh_one_is_not():
    outbox = InMemoryOutboxStore()
    old = _dispatch(outbox, outcome="failed", age_days=10)
    _dispatch(outbox, outcome="failed", age_days=2, contract_id="CTR-RET-2")
    rows = stale_failure_rows(outbox.all())
    assert [row["outbox_id"] for row in rows] == [old.outbox_id]
    assert rows[0]["age_days"] == 10.0
    assert rows[0]["family"] == "approval"
    assert rows[0]["attempts"] == 1


def test_a_sent_entry_is_never_stale_however_old():
    outbox = InMemoryOutboxStore()
    _dispatch(outbox, outcome="sent", age_days=120)
    assert stale_failure_rows(outbox.all()) == []


def test_stale_threshold_is_env_tunable(monkeypatch):
    monkeypatch.setenv("WEBHOOK_OUTBOX_STALE_FAILURE_DAYS", "30")
    outbox = InMemoryOutboxStore()
    _dispatch(outbox, outcome="failed", age_days=10)
    assert stale_failure_rows(outbox.all()) == []
    assert stale_failure_rows(outbox.all(), threshold_days=7)[0]["age_days"] == 10.0


def test_age_is_measured_from_the_last_attempt():
    outbox = InMemoryOutboxStore()
    entry = _dispatch(outbox, outcome="failed", age_days=40)
    # A re-send attempt three days ago refreshes the entry: its
    # age is the age of its current state, not the original
    # dispatch — and a recently-touched failure is not stale.
    recent = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    entry.updated_at = recent
    outbox.save(entry)
    assert entry_age_days(entry) == pytest.approx(3.0, abs=0.1)
    assert stale_failure_rows(outbox.all()) == []


# ---------------------------------------------------------------------------
# The surfaces: outbox view, queue worklist, metrics
# ---------------------------------------------------------------------------


def test_staleness_rides_the_view_the_queue_and_metrics():
    outbox = InMemoryOutboxStore()
    stale = _dispatch(outbox, outcome="failed", age_days=12)
    _dispatch(outbox, outcome="sent", age_days=1, contract_id="CTR-RET-2")
    svc = _service(outbox)

    view = svc.webhook_outbox()
    assert view["staleness"]["stale_failed"] == 1
    assert view["staleness"]["stale_threshold_days"] == 7
    assert view["staleness"]["retention_days"] == 90
    assert view["staleness"]["stale_failures"][0]["outbox_id"] == stale.outbox_id
    by_id = {e["outbox_id"]: e for e in view["entries"]}
    assert by_id[stale.outbox_id]["age_days"] == 12.0

    worklist = svc.stale_webhook_failures()
    assert [row["outbox_id"] for row in worklist] == [stale.outbox_id]

    webhooks = svc.metrics()["webhooks"]
    assert webhooks["total"] == 2
    assert webhooks["stale_failed"] == 1
    assert webhooks["stale_threshold_days"] == 7
    assert webhooks["retention_days"] == 90


# ---------------------------------------------------------------------------
# The prune: governed, named, and never spends a failure
# ---------------------------------------------------------------------------


def test_prune_takes_old_sent_entries_and_keeps_the_rest():
    outbox = InMemoryOutboxStore()
    old_sent = _dispatch(outbox, outcome="sent", age_days=100)
    _dispatch(outbox, outcome="sent", age_days=10, contract_id="CTR-RET-2")
    old_failed = _dispatch(
        outbox, outcome="failed", age_days=100, contract_id="CTR-RET-3"
    )
    svc = _service(outbox)
    report = svc.prune_webhook_outbox("Ops Rivera")
    assert report["pruned"] == [old_sent.outbox_id]
    assert report["pruned_count"] == 1
    assert report["retention_days"] == 90
    assert report["kept_sent"] == 1
    # The hundred-day-old failure is KEPT — and named as stale
    # in the report, so pruning history never buries it.
    assert report["kept_failed"] == 1
    assert [r["outbox_id"] for r in report["stale_failures_kept"]] == [
        old_failed.outbox_id
    ]
    assert [e.outbox_id for e in outbox.all()] != []
    assert outbox.get(old_sent.outbox_id) is None
    assert outbox.get(old_failed.outbox_id) is not None


def test_prune_needs_a_named_actor_and_spends_nothing_without_one():
    outbox = InMemoryOutboxStore()
    _dispatch(outbox, outcome="sent", age_days=100)
    svc = _service(outbox)
    with pytest.raises(ValueError, match="named actor"):
        svc.prune_webhook_outbox("  ")
    assert len(outbox.all()) == 1


def test_retention_age_is_env_tunable_and_can_be_disabled(monkeypatch):
    outbox = InMemoryOutboxStore()
    _dispatch(outbox, outcome="sent", age_days=30)
    monkeypatch.setenv("WEBHOOK_OUTBOX_RETENTION_DAYS", "10")
    assert len(prunable_sent_entries(outbox.all())) == 1
    monkeypatch.setenv("WEBHOOK_OUTBOX_RETENTION_DAYS", "-1")
    assert prunable_sent_entries(outbox.all()) == []
    report = _service(outbox).prune_webhook_outbox("Ops Rivera")
    assert report["pruned_count"] == 0
    assert report["retention_days"] == -1


def test_a_repaired_failure_ages_out_as_a_sent_entry():
    # The only way a failure leaves the ledger: it is repaired,
    # becomes a sent entry, and then ages like any other.
    outbox = InMemoryOutboxStore()
    entry = _dispatch(outbox, outcome="failed", age_days=100)
    entry.status = "sent"
    outbox.save(entry)
    assert [e.outbox_id for e in prunable_sent_entries(outbox.all())] == [
        entry.outbox_id
    ]


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


def test_prune_endpoint_requires_an_actor_and_reports():
    from contract_agent.api import app, service as api_service

    outbox = api_service._get_outbox_store()
    entry = _dispatch(outbox, outcome="sent", age_days=200,
                      contract_id="CTR-RET-API")
    client = TestClient(app)
    refused = client.post("/webhooks/outbox/prune", json={})
    assert refused.status_code == 422
    assert outbox.get(entry.outbox_id) is not None
    res = client.post(
        "/webhooks/outbox/prune", json={"actor": "Ops Rivera"}
    )
    assert res.status_code == 200
    body = res.json()
    assert entry.outbox_id in body["pruned"]
    assert body["actor"] == "Ops Rivera"
    queue = client.get("/queue").json()
    assert "stale_webhook_failures" in queue
