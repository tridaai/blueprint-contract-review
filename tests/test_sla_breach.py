"""SLA breach events (round 6): the first time a queued contract is
observed past its SLA budget, the sweep announces it — one
``sla_breach`` event plus a signed webhook on the approval
channel's scheme — and never again for that contract."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import urllib.request
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from contract_agent.api import app
from contract_agent.events import CollectingSink
from contract_agent.samples import sample_contract_models
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore, _dump_record, _load_record


@pytest.fixture(scope="module")
def samples() -> dict:
    return {c.contract_id: c for c in sample_contract_models()}


@pytest.fixture(autouse=True)
def _default_budgets(monkeypatch):
    for var in (
        "QUEUE_SLA_WALK_AWAY_BUSINESS_DAYS",
        "QUEUE_SLA_REVIEW_REQUIRED_BUSINESS_DAYS",
        "QUEUE_SLA_ACCEPTABLE_FALLBACK_BUSINESS_DAYS",
    ):
        monkeypatch.delenv(var, raising=False)


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


def _stale_walk_away(svc: ContractService, samples, contract_id="CTR-2002"):
    contract = samples[contract_id].model_copy(deep=True)  # walk_away, budget 2bd
    contract.received_at = "2026-09-01T09:30:00"  # a Tuesday
    svc.analyze(contract)
    return contract


def test_breach_is_announced_once_with_the_queue_facts(samples):
    svc = _service()
    _stale_walk_away(svc, samples)
    sink = CollectingSink()
    report = svc.dispatch_sla_breach_sweep(today=date(2026, 9, 10), event_sink=sink)
    assert report["breaches_found"] == 1
    assert report["newly_reported"] == 1
    assert report["already_reported"] == 0
    assert report["events_emitted"] == 1
    assert report["webhooks"] == {"configured": False, "sent": 0, "failed": 0}
    events = [e for e in sink.events if e.type == "sla_breach"]
    assert len(events) == 1
    event = events[0]
    assert event.contract_id == "CTR-2002"
    detail = event.detail
    assert detail["via"] == "sla_sweep"
    assert detail["worst_position"] == "walk_away"
    assert detail["sla_budget_business_days"] == 2
    assert detail["business_days_waiting"] > 2
    assert detail["unassigned"] is True  # the loudest fact, stated outright
    # The stamp is on the record and survives serialisation.
    record = svc._get_store().get("CTR-2002")
    assert record.sla_breach_notified_at
    assert _load_record(_dump_record(record)).sla_breach_notified_at == (
        record.sla_breach_notified_at
    )
    # A second sweep — even much later — does not re-announce.
    sink2 = CollectingSink()
    again = svc.dispatch_sla_breach_sweep(today=date(2026, 12, 1), event_sink=sink2)
    assert again["breaches_found"] == 1
    assert again["newly_reported"] == 0
    assert again["already_reported"] == 1
    assert [e for e in sink2.events if e.type == "sla_breach"] == []


def test_within_budget_and_decided_contracts_are_never_announced(samples):
    svc = _service()
    _stale_walk_away(svc, samples)
    fresh = svc.queue(today=date(2026, 9, 2))  # 1 business day in, budget 2
    assert fresh[0]["sla_breached"] is False
    sink = CollectingSink()
    report = svc.dispatch_sla_breach_sweep(today=date(2026, 9, 2), event_sink=sink)
    assert report["breaches_found"] == 0
    assert sink.events == []
    # Decide it before it breaches: it leaves the queue, no late alarm.
    svc.approve("CTR-2002", approver="C. Ounsel")
    report = svc.dispatch_sla_breach_sweep(today=date(2026, 12, 1), event_sink=sink)
    assert report["awaiting"] == 0
    assert report["breaches_found"] == 0
    assert sink.events == []


def test_assigned_breach_names_its_owner(samples):
    svc = _service()
    _stale_walk_away(svc, samples)
    svc.assign("CTR-2002", assignee="Priya Nair", actor="Team Lead")
    sink = CollectingSink()
    svc.dispatch_sla_breach_sweep(today=date(2026, 9, 10), event_sink=sink)
    detail = sink.events[0].detail
    assert detail["unassigned"] is False
    assert detail["assignee"] == "Priya Nair"


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_breach_webhook_is_signed_and_sent_once(samples, monkeypatch):
    captured = []

    def fake_urlopen(request, timeout=None):
        captured.append(request)
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("ACTION_WEBHOOK_URL", "https://legalops.example.com/hook")
    monkeypatch.setenv("ACTION_WEBHOOK_SECRET", "hook-secret")
    svc = _service()
    _stale_walk_away(svc, samples)
    report = svc.dispatch_sla_breach_sweep(today=date(2026, 9, 10), event_sink=CollectingSink())
    assert report["webhooks"] == {"configured": True, "sent": 1, "failed": 0}
    assert len(captured) == 1
    request = captured[0]
    payload = json.loads(request.data)
    assert payload["event"] == "sla_breach"
    assert payload["contract_id"] == "CTR-2002"
    assert payload["unassigned"] is True
    signature = next(
        (v for k, v in request.headers.items() if k.lower() == "x-trida-signature"), None
    )
    expected = hmac.new(b"hook-secret", request.data, hashlib.sha256).hexdigest()
    assert signature == f"sha256={expected}"
    # Dedupe covers the webhook too: no second POST on a re-sweep.
    svc.dispatch_sla_breach_sweep(today=date(2026, 9, 11), event_sink=CollectingSink())
    assert len(captured) == 1


def test_failed_webhook_is_tallied_and_the_breach_still_counts(samples, monkeypatch):
    def boom(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setenv("ACTION_WEBHOOK_URL", "https://legalops.example.com/hook")
    svc = _service()
    _stale_walk_away(svc, samples)
    report = svc.dispatch_sla_breach_sweep(today=date(2026, 9, 10), event_sink=CollectingSink())
    assert report["newly_reported"] == 1
    assert report["webhooks"] == {"configured": True, "sent": 0, "failed": 1}
    assert svc.metrics()["queue_sla"]["breach_events_emitted"] == 1


def test_metrics_count_announced_breaches(samples):
    svc = _service()
    _stale_walk_away(svc, samples)
    assert svc.metrics()["queue_sla"]["breach_events_emitted"] == 0
    svc.dispatch_sla_breach_sweep(today=date(2026, 9, 10), event_sink=CollectingSink())
    assert svc.metrics()["queue_sla"]["breach_events_emitted"] == 1


@pytest.fixture()
def client():
    return TestClient(app)


def test_sla_sweep_endpoint_returns_the_report(client, samples):
    payload = samples["CTR-2002"].model_copy(deep=True)
    payload.received_at = "2020-01-06T09:30:00"  # ancient on any real clock
    assert client.post(
        "/contracts/analyze", json=payload.model_dump(mode="json")
    ).status_code == 200
    res = client.post("/queue/sla-sweep")
    assert res.status_code == 200
    body = res.json()
    assert body["breaches_found"] >= 1
    assert body["newly_reported"] >= 1
    mine = next(b for b in body["breaches"] if b["contract_id"] == "CTR-2002")
    assert mine["worst_position"] == "walk_away"
    again = client.post("/queue/sla-sweep").json()
    assert again["newly_reported"] == 0
    assert again["already_reported"] >= 1


def test_sla_sweep_cli_announces(tmp_path: Path, samples):
    stale = samples["CTR-2002"].model_copy(deep=True)
    stale.received_at = "2020-01-06T09:30:00"
    fixture = tmp_path / "contracts.json"
    fixture.write_text(
        json.dumps([stale.model_dump(mode="json")]), encoding="utf-8"
    )
    env = {**os.environ, "CHECKPOINTS": "off"}
    proc = subprocess.run(
        [sys.executable, "-m", "contract_agent.cli", "--file", str(fixture), "--sweep-sla"],
        capture_output=True, text=True, env=env, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "SLA BREACH" in proc.stdout
    assert "CTR-2002" in proc.stdout
    assert "1 newly announced" in proc.stdout
