"""Stale-failure announcement sweep (round 16): round 15
surfaced stale outbox failures on reads — the outbox view,
/metrics, the counsel queue — but a surface waits for someone
to open it, and nothing ANNOUNCED a dead delivery. The sweep
announces: one ``webhook_stale_failure`` event per stale entry
on the stream, one signed webhook per sweep naming them all,
and a stamp on each entry so it is announced once per stale
period — the SLA breach sweep's dedupe discipline. A re-send
attempt clears the stamp, so a delivery that was touched and
failed again re-arms: announced once is a property of a stale
period, not of an entry's whole life.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from contract_agent.events import CollectingSink
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore
from contract_agent.webhook_outbox import (
    InMemoryOutboxStore,
    record_dispatch,
    unannounced_stale_failures,
)


def _service(outbox: InMemoryOutboxStore) -> ContractService:
    return ContractService(
        store=InMemoryStore(), checkpointer=False, outbox_store=outbox
    )


def _dispatch(outbox: InMemoryOutboxStore, *, outcome: str, age_days: float,
              contract_id: str = "CTR-ST-1", family: str = "approval"):
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


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Endpoint:
    def __init__(self, monkeypatch, status: int = 200) -> None:
        self.status = status
        self.bodies: list[dict] = []

        def fake_urlopen(request, timeout=None):
            self.bodies.append(json.loads(request.data.decode("utf-8")))
            return _Response(self.status)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setenv("ACTION_WEBHOOK_URL", "https://ops.example.com/hook")
        monkeypatch.setenv("ACTION_WEBHOOK_SECRET", "hook-secret")


# ---------------------------------------------------------------------------
# Selection: stale, failed, and not yet announced
# ---------------------------------------------------------------------------


def test_unannounced_selection_skips_stamped_sent_and_fresh_entries():
    outbox = InMemoryOutboxStore()
    stale = _dispatch(outbox, outcome="failed", age_days=10)
    _dispatch(outbox, outcome="failed", age_days=2, contract_id="CTR-ST-2")
    _dispatch(outbox, outcome="sent", age_days=30, contract_id="CTR-ST-3")
    stamped = _dispatch(outbox, outcome="failed", age_days=20,
                        contract_id="CTR-ST-4")
    stamped.stale_announced_at = "2026-10-01T00:00:00+00:00"
    outbox.save(stamped)
    assert [e.outbox_id for e in unannounced_stale_failures(outbox.all())] == [
        stale.outbox_id
    ]


# ---------------------------------------------------------------------------
# The sweep: announce once per stale period
# ---------------------------------------------------------------------------


def test_sweep_announces_stamps_and_reports():
    outbox = InMemoryOutboxStore()
    stale = _dispatch(outbox, outcome="failed", age_days=12)
    _dispatch(outbox, outcome="failed", age_days=1, contract_id="CTR-ST-2")
    svc = _service(outbox)
    sink = CollectingSink()
    report = svc.dispatch_stale_failure_sweep(event_sink=sink)
    assert report["stale_failed"] == 1
    assert report["newly_announced"] == 1
    assert report["already_announced"] == 0
    assert report["failures"][0]["outbox_id"] == stale.outbox_id
    assert report["failures"][0]["announced"] is True
    # No webhook endpoint configured: the announcement is the
    # event stream and the stamp alone.
    assert report["webhooks"] == {"configured": False, "sent": 0, "failed": 0}
    events = [e for e in sink.events if e.type == "webhook_stale_failure"]
    assert len(events) == 1
    assert events[0].contract_id == "CTR-ST-1"
    assert events[0].detail["outbox_id"] == stale.outbox_id
    assert events[0].detail["age_days"] == 12.0
    assert outbox.get(stale.outbox_id).stale_announced_at != ""
    # The stale worklist now reads the entry as announced.
    rows = svc.stale_webhook_failures()
    assert rows[0]["announced"] is True
    assert svc.webhook_outbox()["staleness"]["unannounced_stale_failed"] == 0
    assert svc.metrics()["webhooks"]["unannounced_stale_failed"] == 0


def test_a_second_sweep_announces_nothing_new():
    outbox = InMemoryOutboxStore()
    _dispatch(outbox, outcome="failed", age_days=12)
    svc = _service(outbox)
    svc.dispatch_stale_failure_sweep()
    sink = CollectingSink()
    report = svc.dispatch_stale_failure_sweep(event_sink=sink)
    assert report["newly_announced"] == 0
    assert report["already_announced"] == 1
    assert sink.events == []


def test_sweep_posts_one_signed_announcement_naming_every_stale_failure(
    monkeypatch,
):
    endpoint = _Endpoint(monkeypatch)
    outbox = InMemoryOutboxStore()
    _dispatch(outbox, outcome="failed", age_days=12)
    _dispatch(outbox, outcome="failed", age_days=9, contract_id="CTR-ST-2",
              family="drift_finding")
    svc = _service(outbox)
    report = svc.dispatch_stale_failure_sweep()
    assert report["newly_announced"] == 2
    assert report["webhooks"] == {"configured": True, "sent": 1, "failed": 0}
    # ONE webhook for the sweep, naming both failures — and the
    # announcement itself is not an outbox entry (it would go
    # stale and announce itself forever).
    assert len(endpoint.bodies) == 1
    body = endpoint.bodies[0]
    assert body["event"] == "webhook_stale_failure"
    assert body["stale_count"] == 2
    assert {f["contract_id"] for f in body["failures"]} == {
        "CTR-ST-1", "CTR-ST-2",
    }
    assert len(outbox.all()) == 2


def test_a_failed_announcement_still_stamps_the_entries(monkeypatch):
    _Endpoint(monkeypatch, status=500)
    outbox = InMemoryOutboxStore()
    stale = _dispatch(outbox, outcome="failed", age_days=12)
    svc = _service(outbox)
    report = svc.dispatch_stale_failure_sweep()
    assert report["webhooks"]["failed"] == 1
    # Announced is announced: the sweep ran and the telling was
    # attempted — the stamp stands, like the breach sweep's.
    assert outbox.get(stale.outbox_id).stale_announced_at != ""


def test_a_resend_attempt_rearms_the_announcement(monkeypatch):
    endpoint = _Endpoint(monkeypatch, status=500)  # endpoint stays broken
    outbox = InMemoryOutboxStore()
    stale = _dispatch(outbox, outcome="failed", age_days=12)
    svc = _service(outbox)
    svc.dispatch_stale_failure_sweep()
    assert outbox.get(stale.outbox_id).stale_announced_at != ""
    # The operator tries the repair; it fails again. The touch
    # clears the stamp — and once the entry has sat for another
    # stale period, the sweep announces it anew.
    result = svc.resend_webhook(stale.outbox_id, actor="Ops Rivera")
    assert result["outcome"] == "failed"
    assert outbox.get(stale.outbox_id).stale_announced_at == ""
    assert svc.dispatch_stale_failure_sweep()["newly_announced"] == 0
    entry = outbox.get(stale.outbox_id)
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    entry.updated_at = old
    entry.attempts[-1].at = old
    outbox.save(entry)
    report = svc.dispatch_stale_failure_sweep()
    assert report["newly_announced"] == 1
    assert len(endpoint.bodies) == 3  # announcement, re-send, announcement


def test_announce_endpoint_runs_the_sweep():
    from contract_agent.api import app, service as api_service

    outbox = api_service._get_outbox_store()
    _dispatch(outbox, outcome="failed", age_days=15, contract_id="CTR-ST-API")
    client = TestClient(app)
    res = client.post("/webhooks/outbox/announce-stale")
    assert res.status_code == 200
    body = res.json()
    assert body["newly_announced"] == 1
    assert body["failures"][0]["contract_id"] == "CTR-ST-API"
    again = client.post("/webhooks/outbox/announce-stale").json()
    assert again["newly_announced"] == 0
