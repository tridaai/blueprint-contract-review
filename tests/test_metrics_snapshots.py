"""Metrics snapshots (round 19): /metrics answered "how do
things stand?" — as of now, only. A snapshot keeps the whole
payload under an operator's name, and the trend reads the kept
snapshots against each other: run growth, queue and breach
movement, outbox totals — and the prune pace, prunes and
retired entries SINCE the previous kept snapshot, differences
of the prune ledger's own totals (which ride inside each
payload's webhooks block), so retention behaviour is visible
over time, not just as an all-time total. Keeping is an act
with a name on it; a label names one moment.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from contract_agent.metrics_snapshots import (
    next_snapshot_id,
    prune_readings,
    snapshot_trend,
)
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import MetricsSnapshot, OutboxPrune
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore
from contract_agent.webhook_outbox import InMemoryOutboxStore

ACTOR = "Ops — J. Park"


def _prune(removed: int) -> OutboxPrune:
    return OutboxPrune(
        actor=ACTOR,
        at=datetime.now(timezone.utc).isoformat(),
        retention_days=90,
        pruned_ids=[f"entry-{i}" for i in range(removed)],
        kept_sent=10,
        kept_failed=1,
    )


def _service(outbox: InMemoryOutboxStore | None = None) -> ContractService:
    return ContractService(
        store=InMemoryStore(),
        checkpointer=False,
        outbox_store=outbox or InMemoryOutboxStore(),
    )


# ---------------------------------------------------------------------------
# Keeping: a named act, sequenced ids, unique labels
# ---------------------------------------------------------------------------


def test_keeping_requires_an_actor():
    svc = _service()
    with pytest.raises(ValueError, match="actor"):
        svc.keep_metrics_snapshot("")
    with pytest.raises(ValueError, match="actor"):
        svc.keep_metrics_snapshot("   ")


def test_a_kept_snapshot_carries_the_whole_payload():
    samples = {c.contract_id: c for c in sample_contract_models()}
    svc = _service()
    svc.analyze(samples["CTR-2014"])
    snapshot = svc.keep_metrics_snapshot(ACTOR, label="October baseline")
    assert snapshot.snapshot_id == "SNAP-0001"
    assert snapshot.label == "October baseline"
    assert snapshot.actor == ACTOR
    assert snapshot.kept_at
    assert snapshot.metrics["runs_total"] == 1
    # The prune readings ride inside the kept payload.
    assert snapshot.metrics["webhooks"]["prunes"]["total_prunes"] == 0
    second = svc.keep_metrics_snapshot(ACTOR)
    assert second.snapshot_id == "SNAP-0002"
    assert second.label == ""


def test_a_label_names_one_moment():
    svc = _service()
    svc.keep_metrics_snapshot(ACTOR, label="October baseline")
    with pytest.raises(ValueError, match="SNAP-0001"):
        svc.keep_metrics_snapshot("Counsel — A. Reyes", label="October baseline")
    # An unlabelled keep never collides.
    svc.keep_metrics_snapshot("Counsel — A. Reyes")
    assert len(svc.metrics_snapshots()) == 2


def test_snapshot_ids_never_reuse():
    existing = [
        MetricsSnapshot(snapshot_id="SNAP-0007"),
        MetricsSnapshot(snapshot_id="SNAP-0002"),
        MetricsSnapshot(snapshot_id=""),
    ]
    assert next_snapshot_id(existing) == "SNAP-0008"
    assert next_snapshot_id([]) == "SNAP-0001"


# ---------------------------------------------------------------------------
# The trend: movement since the previous kept snapshot
# ---------------------------------------------------------------------------


def _payload(runs: int, prunes: int, removed: int) -> dict:
    return {
        "runs_total": runs,
        "queue_sla": {"awaiting": 2, "sla_breached": 1},
        "webhooks": {
            "total": 9, "failed": 1, "stale_failed": 0,
            "prunes": {
                "total_prunes": prunes,
                "entries_removed": removed,
                "last_prune": None,
            },
        },
    }


def test_the_first_snapshot_has_no_movement_to_claim():
    trend = snapshot_trend(
        [MetricsSnapshot(snapshot_id="SNAP-0001", metrics=_payload(3, 1, 4))]
    )
    (row,) = trend["snapshots"]
    assert row["runs_total"] == 3
    assert row["runs_since_previous"] is None
    assert row["prunes_since_previous"] is None
    assert row["entries_retired_since_previous"] is None
    assert row["total_prunes"] == 1
    assert row["entries_removed"] == 4


def test_the_trend_measures_the_prune_pace():
    trend = snapshot_trend([
        MetricsSnapshot(snapshot_id="SNAP-0001", metrics=_payload(3, 1, 4)),
        MetricsSnapshot(snapshot_id="SNAP-0002", metrics=_payload(8, 3, 19)),
    ])
    first, second = trend["snapshots"]
    assert second["runs_since_previous"] == 5
    assert second["prunes_since_previous"] == 2
    assert second["entries_retired_since_previous"] == 15
    assert second["sla_breached_change"] == 0


def test_prune_readings_come_out_of_the_payload():
    snapshot = MetricsSnapshot(metrics=_payload(1, 2, 7))
    assert prune_readings(snapshot)["total_prunes"] == 2
    assert prune_readings(MetricsSnapshot(metrics={})) == {}


def test_the_service_trend_follows_the_ledger():
    outbox = InMemoryOutboxStore()
    svc = _service(outbox)
    svc.keep_metrics_snapshot(ACTOR, label="before retention ran")
    outbox.record_prune(_prune(4))
    outbox.record_prune(_prune(6))
    svc.keep_metrics_snapshot(ACTOR, label="after retention ran")
    trend = svc.metrics_snapshot_trend()
    assert trend["count"] == 2
    first, second = trend["snapshots"]
    assert first["prunes_since_previous"] is None
    assert second["prunes_since_previous"] == 2
    assert second["entries_retired_since_previous"] == 10
    assert second["total_prunes"] == 2
    assert second["entries_removed"] == 10
    assert second["last_prune"]["actor"] == ACTOR


# ---------------------------------------------------------------------------
# The API surface
# ---------------------------------------------------------------------------


def test_the_api_keeps_lists_and_trends():
    from contract_agent.api import app

    client = TestClient(app)
    refused = client.post("/metrics/snapshots", json={"label": "x"})
    assert refused.status_code == 422
    kept = client.post(
        "/metrics/snapshots",
        json={"actor": ACTOR, "label": "api baseline"},
    )
    assert kept.status_code == 200
    assert kept.json()["snapshot_id"].startswith("SNAP-")
    assert kept.json()["metrics"]["webhooks"]["prunes"]["total_prunes"] == 0
    listing = client.get("/metrics/snapshots")
    assert listing.status_code == 200
    assert listing.json()["count"] >= 1
    trend = client.get("/metrics/snapshots/trend")
    assert trend.status_code == 200
    assert trend.json()["count"] >= 1
    assert "prunes_since_previous" in trend.json()["snapshots"][0]
    # A label collision is refused with the holder named.
    clash = client.post(
        "/metrics/snapshots",
        json={"actor": ACTOR, "label": "api baseline"},
    )
    assert clash.status_code == 422
    assert "already kept" in clash.json()["detail"]
