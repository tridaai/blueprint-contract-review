"""Prune-ledger readings in /metrics (round 18): the prune
ledger made every retention act auditable, but nothing
trended it — legal ops could read the acts one by one and
still not see the ledger's own growth or the pace at which
delivery history was being retired. The readings aggregate
the ledger beside the staleness numbers in /metrics' webhooks
block: prune counts and removed-entry totals, all-time and
over the trailing 30 days, and the last prune by name.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from contract_agent.schemas import OutboxPrune
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore
from contract_agent.webhook_outbox import InMemoryOutboxStore, prune_ledger_readings

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def _prune(actor: str, at: datetime, removed: int, retention: int = 90) -> OutboxPrune:
    return OutboxPrune(
        actor=actor,
        at=at.isoformat(),
        retention_days=retention,
        pruned_ids=[f"entry-{i}" for i in range(removed)],
        kept_sent=10,
        kept_failed=1,
    )


def test_an_empty_ledger_reads_as_zeros():
    readings = prune_ledger_readings([], now=NOW)
    assert readings["total_prunes"] == 0
    assert readings["entries_removed"] == 0
    assert readings["prunes_in_window"] == 0
    assert readings["entries_removed_in_window"] == 0
    assert readings["last_prune"] is None


def test_readings_aggregate_totals_window_and_last():
    prunes = [
        _prune("Ops — J. Park", NOW - timedelta(days=45), 5),
        _prune("Ops — J. Park", NOW - timedelta(days=10), 3),
        _prune("Counsel — A. Reyes", NOW - timedelta(days=1), 0),
    ]
    readings = prune_ledger_readings(prunes, now=NOW)
    assert readings["total_prunes"] == 3
    assert readings["entries_removed"] == 8
    # The 45-day-old act is outside the trailing 30 days; the
    # other two are in it — one of them removed nothing, and
    # still counts as an act.
    assert readings["prunes_in_window"] == 2
    assert readings["entries_removed_in_window"] == 3
    last = readings["last_prune"]
    assert last["actor"] == "Counsel — A. Reyes"
    assert last["entries_removed"] == 0
    assert last["retention_days"] == 90
    assert last["prune_id"] == ""  # ids are the store's to assign


def test_the_last_prune_is_latest_by_stamp_not_by_position():
    prunes = [
        _prune("Ops — J. Park", NOW - timedelta(days=2), 1),
        _prune("Counsel — A. Reyes", NOW - timedelta(days=20), 2),
    ]
    readings = prune_ledger_readings(prunes, now=NOW)
    assert readings["last_prune"]["actor"] == "Ops — J. Park"


def test_metrics_carry_the_readings_beside_the_staleness_numbers():
    outbox = InMemoryOutboxStore()
    outbox.record_prune(
        _prune("Ops — J. Park", datetime.now(timezone.utc), 4)
    )
    svc = ContractService(
        store=InMemoryStore(), checkpointer=False, outbox_store=outbox
    )
    webhooks = svc.metrics()["webhooks"]
    assert webhooks["prunes"]["total_prunes"] == 1
    assert webhooks["prunes"]["entries_removed"] == 4
    assert webhooks["prunes"]["prunes_in_window"] == 1
    assert webhooks["prunes"]["last_prune"]["actor"] == "Ops — J. Park"
    # The staleness numbers the readings ride beside are intact.
    assert webhooks["stale_failed"] == 0
    assert "retention_days" in webhooks


def test_a_prune_act_lands_in_the_metrics_readings():
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    report = svc.prune_webhook_outbox("Ops — J. Park")
    assert report["pruned_count"] == 0  # an empty outbox prunes nothing
    webhooks = svc.metrics()["webhooks"]
    # ...but the act happened, and the readings carry it.
    assert webhooks["prunes"]["total_prunes"] == 1
    assert webhooks["prunes"]["entries_removed"] == 0
    assert webhooks["prunes"]["last_prune"]["actor"] == "Ops — J. Park"
    assert webhooks["prunes"]["last_prune"]["prune_id"] == report["prune_id"]
