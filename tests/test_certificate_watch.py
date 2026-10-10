"""Certificate renewal watch (round 9): a certificate that passed
its exhibit review still expires mid-term — the deadline sweep
watches the relied-upon ones and announces the expiry as a
``certificate_expiring`` event, while certificates that already
failed review stay findings, not watches."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from contract_agent.api import app
from contract_agent.certificates import certificate_watches
from contract_agent.events import CollectingSink
from contract_agent.samples import sample_contract_models
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore


@pytest.fixture(scope="module")
def samples() -> dict:
    return {c.contract_id: c for c in sample_contract_models()}


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


def _halcyon_service(samples) -> ContractService:
    svc = _service()
    svc.analyze(samples["CTR-2027"])  # cert consistent, expires 2027-03-31
    svc.analyze(samples["CTR-2028"])  # cert failed review (expired, wrong entity)
    svc.analyze(samples["CTR-2029"])  # cert consistent, expires 2026-12-31
    return svc


def test_ctr2029_certificate_is_consistent_at_review(samples):
    svc = _service()
    result = svc.analyze(samples["CTR-2029"])
    expiry = next(
        c for c in result.exhibit_checks if c.name == "insurance_certificate_expiry"
    )
    assert expiry.status == "consistent"
    assert expiry.found == "2026-12-31"
    assert all(c.status == "consistent" for c in result.exhibit_checks)


def test_watch_flags_only_relied_upon_certificates_in_window(samples):
    svc = _halcyon_service(samples)
    view = svc.certificate_watches(today=date(2026, 10, 10))
    assert view["window_days"] == 90
    ids = [row["contract_id"] for row in view["certificates"]]
    # CTR-2029 expires in 82 days: watched. CTR-2027 expires in 172
    # days: outside the window. CTR-2028's certificate failed review
    # (expired at receipt) — a finding, never a watch.
    assert ids == ["CTR-2029"]
    row = view["certificates"][0]
    assert row["expiry_date"] == "2026-12-31"
    assert row["days_until"] == 82
    assert row["overdue"] is False
    assert row["counterparty_name"] == "Brightpath Managed Services Ltd"


def test_watch_window_is_configurable_and_overdue_is_flagged(samples):
    svc = _halcyon_service(samples)
    narrow = svc.certificate_watches(window_days=30, today=date(2026, 10, 10))
    assert narrow["count"] == 0
    wide = svc.certificate_watches(window_days=200, today=date(2026, 10, 10))
    assert [r["contract_id"] for r in wide["certificates"]] == [
        "CTR-2029",
        "CTR-2027",
    ]
    # Past the expiry the watch does not age out: a lapsed
    # certificate is the loudest row.
    lapsed = svc.certificate_watches(today=date(2027, 1, 15))
    row = next(r for r in lapsed["certificates"] if r["contract_id"] == "CTR-2029")
    assert row["overdue"] is True
    assert row["days_until"] == -15


def test_watch_window_env_override(samples, monkeypatch):
    monkeypatch.setenv("CERTIFICATE_EXPIRY_WATCH_DAYS", "200")
    svc = _halcyon_service(samples)
    view = svc.certificate_watches(today=date(2026, 10, 10))
    assert view["window_days"] == 200
    assert view["count"] == 2


def test_watch_reads_the_latest_round_of_a_chain(samples):
    # Two stored rounds of one negotiation, each carrying a
    # certificate check: only the latest round's expiry is watched.
    from contract_agent.schemas import AgentResult, ExhibitCheck
    from contract_agent.store import ApprovalRecord

    def record(cid, supersedes, expiry):
        result = AgentResult(
            contract_id=cid,
            counterparty_name="Chain Co",
            exhibit_checks=[
                ExhibitCheck(
                    name="insurance_certificate_expiry",
                    found=expiry,
                    status="consistent",
                )
            ],
        )
        return ApprovalRecord(
            result=result, contract={"supersedes": supersedes}
        )

    records = [
        record("CTR-CH-1", "", "2026-11-01"),
        record("CTR-CH-2", "CTR-CH-1", "2027-06-01"),
    ]
    rows = certificate_watches(records, date(2026, 10, 10), 90)
    assert rows == []  # the operative certificate runs to 2027-06-01
    rows = certificate_watches(records, date(2026, 10, 10), 300)
    assert [r["contract_id"] for r in rows] == ["CTR-CH-2"]


def test_sweep_emits_certificate_expiring_events(samples):
    svc = _halcyon_service(samples)
    sink = CollectingSink()
    report = svc.dispatch_obligation_sweep(
        days=90, today=date(2026, 10, 10), event_sink=sink
    )
    assert report["certificate_watch_window_days"] == 90
    assert report["certificate_expiring_count"] == 1
    assert report["certificate_events_emitted"] == 1
    assert report["certificates"][0]["contract_id"] == "CTR-2029"
    events = [e for e in sink.events if e.type == "certificate_expiring"]
    assert len(events) == 1
    assert events[0].contract_id == "CTR-2029"
    assert events[0].detail["via"] == "deadline_sweep"
    assert events[0].detail["expiry_date"] == "2026-12-31"
    assert events[0].detail["days_until"] == 82


def test_expiring_endpoint_returns_the_watch(samples):
    client = TestClient(app)
    payload = samples["CTR-2029"].model_dump(mode="json")
    assert client.post("/contracts/analyze", json=payload).status_code == 200
    res = client.get("/certificates/expiring", params={"window_days": 3650})
    assert res.status_code == 200
    body = res.json()
    row = next(
        r for r in body["certificates"] if r["contract_id"] == "CTR-2029"
    )
    assert row["expiry_date"] == "2026-12-31"
    bad = client.get("/certificates/expiring", params={"window_days": -1})
    assert bad.status_code == 422
