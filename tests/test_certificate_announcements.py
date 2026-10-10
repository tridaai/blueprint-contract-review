"""Follow-through-aware certificate announcements (round 19):
the deadline sweep used to announce every watched certificate
the same way on every run — open, chased, and risk-accepted
alike, forever. Now the sweep announces what is true: the
certificate_expiring event carries the watch's follow-through
state; a renewal chase that has gone stale (round 18's ageing
reading) draws its own signed announcement — a
certificate_watch_stale_chase event plus one signed webhook
per sweep naming them all — stamped on the chase act so it is
announced once per stale period, like the outbox stale sweep;
and a watch whose risk counsel accepted stops announcing at
all: the decision closed the loop the announcement was
chasing, the way a confirmed covenant lapse stops announcing.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import date, timedelta

import pytest

from contract_agent.certificates import chase_act, stale_chase_due
from contract_agent.events import CollectingSink
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore

TODAY = date(2026, 10, 10)  # CTR-2029's cert is 82 days out: watched


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def _service(samples) -> ContractService:
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(samples["CTR-2029"])  # cert consistent, expires 2026-12-31
    return svc


def _chase(svc: ContractService) -> None:
    svc.record_certificate_watch_follow_through(
        "CTR-2029", "renewal_chased", "Counsel — A. Reyes", today=TODAY
    )


def _accept(svc: ContractService) -> None:
    svc.record_certificate_watch_follow_through(
        "CTR-2029", "risk_accepted", "Counsel — A. Reyes",
        note="Cover lapses for two weeks mid-term; the exposure is priced.",
        today=TODAY,
    )


def _act_day(svc: ContractService) -> date:
    record = svc._get_store().get("CTR-2029")
    act = chase_act(record.certificate_watch_follow_throughs)
    return date.fromisoformat(act["at"][:10])


def _sweep(svc: ContractService, today: date):
    sink = CollectingSink()
    report = svc.dispatch_obligation_sweep(
        days=90, today=today, event_sink=sink
    )
    return report, sink


# ---------------------------------------------------------------------------
# The states the announcement carries — and the one it stops for
# ---------------------------------------------------------------------------


def test_an_open_watch_announces_with_its_state(samples):
    report, sink = _sweep(_service(samples), TODAY)
    assert report["certificate_expiring_count"] == 1
    assert report["certificate_events_emitted"] == 1
    assert report["certificate_risk_accepted_count"] == 0
    assert report["certificate_stale_chase_count"] == 0
    (event,) = [e for e in sink.events if e.type == "certificate_expiring"]
    assert event.detail["follow_through_state"] == "open"
    assert event.detail["chase_stale"] is False


def test_a_chased_watch_announces_as_chased(samples):
    svc = _service(samples)
    _chase(svc)
    report, sink = _sweep(svc, _act_day(svc))
    (event,) = [e for e in sink.events if e.type == "certificate_expiring"]
    assert event.detail["follow_through_state"] == "renewal_chased"
    assert event.detail["chase_stale"] is False
    assert report["certificate_stale_chase_count"] == 0


def test_an_accepted_risk_stops_announcing(samples):
    svc = _service(samples)
    _chase(svc)
    _accept(svc)
    report, sink = _sweep(svc, TODAY)
    # The watch still stands and still counts — but nothing is
    # announced for it. The decision closed the loop.
    assert report["certificate_expiring_count"] == 1
    assert report["certificate_events_emitted"] == 0
    assert report["certificate_risk_accepted_count"] == 1
    assert report["certificates_risk_accepted"][0]["contract_id"] == "CTR-2029"
    assert not [
        e for e in sink.events
        if e.type in ("certificate_expiring", "certificate_watch_stale_chase")
    ]


# ---------------------------------------------------------------------------
# The stale chase announcement: signed, stamped, once per period
# ---------------------------------------------------------------------------


def test_a_stale_chase_is_announced_once(samples):
    svc = _service(samples)
    _chase(svc)
    later = _act_day(svc) + timedelta(days=45)
    report, sink = _sweep(svc, later)
    assert report["certificate_stale_chase_count"] == 1
    assert report["certificate_stale_chase_already_announced"] == 0
    (stale,) = [
        e for e in sink.events if e.type == "certificate_watch_stale_chase"
    ]
    assert stale.contract_id == "CTR-2029"
    assert stale.detail["chased_by"] == "Counsel — A. Reyes"
    assert stale.detail["age_days"] == 45
    assert stale.detail["expiry_date"] == "2026-12-31"
    # The expiring event still fires, and now says the chase is stale.
    (expiring,) = [e for e in sink.events if e.type == "certificate_expiring"]
    assert expiring.detail["chase_stale"] is True
    # The chase act carries the stamp: this period is announced.
    record = svc._get_store().get("CTR-2029")
    act = chase_act(record.certificate_watch_follow_throughs)
    assert act["stale_announced_at"] == stale.detail["announced_at"]
    # A second sweep over the same stale chase announces nothing new.
    report2, sink2 = _sweep(svc, later)
    assert report2["certificate_stale_chase_count"] == 0
    assert report2["certificate_stale_chase_already_announced"] == 1
    assert not [
        e for e in sink2.events if e.type == "certificate_watch_stale_chase"
    ]


def test_an_accepted_risk_after_a_stale_announcement_closes_it(samples):
    svc = _service(samples)
    _chase(svc)
    later = _act_day(svc) + timedelta(days=45)
    _sweep(svc, later)  # the stale chase is announced
    _accept(svc)
    report, sink = _sweep(svc, later + timedelta(days=30))
    assert report["certificate_risk_accepted_count"] == 1
    assert report["certificate_stale_chase_count"] == 0
    assert report["certificate_stale_chase_already_announced"] == 0
    assert not [
        e for e in sink.events
        if e.type in ("certificate_expiring", "certificate_watch_stale_chase")
    ]


def test_the_stamp_survives_the_store_round_trip(samples):
    from contract_agent.store import _dump_record, _load_record

    svc = _service(samples)
    _chase(svc)
    _sweep(svc, _act_day(svc) + timedelta(days=45))
    record = svc._get_store().get("CTR-2029")
    restored = _load_record(_dump_record(record))
    act = chase_act(restored.certificate_watch_follow_throughs)
    assert act["stale_announced_at"]


def test_stale_chase_due_reads_the_row(samples):
    fresh = {
        "follow_through_ageing": {"stale": False},
        "follow_through": [{"state": "renewal_chased", "at": "2026-10-01"}],
    }
    assert stale_chase_due(fresh) is False
    due = {
        "follow_through_ageing": {"stale": True},
        "follow_through": [{"state": "renewal_chased", "at": "2026-08-01"}],
    }
    assert stale_chase_due(due) is True
    stamped = {
        "follow_through_ageing": {"stale": True},
        "follow_through": [
            {"state": "renewal_chased", "at": "2026-08-01",
             "stale_announced_at": "2026-09-15T00:00:00+00:00"}
        ],
    }
    assert stale_chase_due(stamped) is False


# ---------------------------------------------------------------------------
# The signed webhook: one per sweep, naming them all
# ---------------------------------------------------------------------------


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
        self.requests: list = []

        def fake_urlopen(request, timeout=None):
            self.requests.append(request)
            return _Response(self.status)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setenv("ACTION_WEBHOOK_URL", "https://ops.example.com/hook")
        monkeypatch.setenv("ACTION_WEBHOOK_SECRET", "hook-secret")


def test_one_signed_webhook_names_the_stale_chases(samples, monkeypatch):
    endpoint = _Endpoint(monkeypatch)
    svc = _service(samples)
    _chase(svc)
    later = _act_day(svc) + timedelta(days=45)
    report, _ = _sweep(svc, later)
    assert report["certificate_stale_chase_webhooks"] == {
        "configured": True, "sent": 1, "failed": 0,
    }
    (request,) = endpoint.requests
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["event"] == "certificate_watch_stale_chase"
    assert payload["stale_chase_count"] == 1
    (watch,) = payload["watches"]
    assert watch["contract_id"] == "CTR-2029"
    assert watch["chased_by"] == "Counsel — A. Reyes"
    assert watch["age_days"] == 45
    assert watch["expiry_date"] == "2026-12-31"
    # Signed, like every announcement this service sends.
    headers = {k.lower(): v for k, v in request.headers.items()}
    assert headers.get("x-trida-signature", "").startswith("sha256=")
    # And no second webhook on the deduped re-run.
    _sweep(svc, later)
    assert len(endpoint.requests) == 1


def test_a_failed_announcement_still_stamps_the_chase(samples, monkeypatch):
    _Endpoint(monkeypatch, status=500)
    svc = _service(samples)
    _chase(svc)
    later = _act_day(svc) + timedelta(days=45)
    report, _ = _sweep(svc, later)
    assert report["certificate_stale_chase_webhooks"]["failed"] == 1
    # The stamp is the record — written whether or not the POST
    # landed, so a broken endpoint cannot re-announce forever.
    record = svc._get_store().get("CTR-2029")
    assert chase_act(record.certificate_watch_follow_throughs)[
        "stale_announced_at"
    ]
    report2, _ = _sweep(svc, later)
    assert report2["certificate_stale_chase_already_announced"] == 1


def test_no_webhook_configured_still_announces_to_the_sink(samples):
    svc = _service(samples)
    _chase(svc)
    report, sink = _sweep(svc, _act_day(svc) + timedelta(days=45))
    assert report["certificate_stale_chase_webhooks"] == {
        "configured": False, "sent": 0, "failed": 0,
    }
    assert report["certificate_stale_chase_count"] == 1
    assert [
        e for e in sink.events if e.type == "certificate_watch_stale_chase"
    ]


def test_the_sweep_endpoint_carries_the_new_report(samples):
    from fastapi.testclient import TestClient

    from contract_agent.api import app

    client = TestClient(app)
    payload = samples["CTR-2029"].model_dump(mode="json")
    assert client.post("/contracts/analyze", json=payload).status_code == 200
    res = client.post("/obligations/sweep", json={"days": 90})
    assert res.status_code == 200
    body = res.json()
    assert body["certificate_risk_accepted_count"] == 0
    assert body["certificate_stale_chase_count"] == 0
    assert body["certificates_risk_accepted"] == []
    assert body["certificate_stale_chase_webhooks"]["configured"] is False
