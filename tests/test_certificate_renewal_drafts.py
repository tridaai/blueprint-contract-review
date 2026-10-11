"""Certificate watch rows carry the coverage, and draft the ask:
the watch named a contract and a date, but the cover at stake
lived on the review record and the renewal request had to be
written from scratch. Each row now carries the coverage facts
as its review recorded them, an explicit expired/expiring
state, its chain's root — and a deterministic renewal-request
draft composed from those recorded facts and no others."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from contract_agent.api import app
from contract_agent.certificates import certificate_watches
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import AgentResult, ExhibitCheck
from contract_agent.service import ContractService
from contract_agent.store import ApprovalRecord, InMemoryStore


@pytest.fixture(scope="module")
def samples() -> dict:
    return {c.contract_id: c for c in sample_contract_models()}


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


def _brightpath_service(samples) -> ContractService:
    svc = _service()
    svc.analyze(samples["CTR-2029"])  # cert consistent, expires 2026-12-31
    return svc


def test_watch_row_carries_the_recorded_coverage_facts(samples):
    view = _brightpath_service(samples).certificate_watches(
        today=date(2026, 10, 10)
    )
    row = view["certificates"][0]
    assert row["state"] == "expiring"
    assert row["chain_root"] == "CTR-2029"
    coverage = row["coverage"]
    assert coverage["named_insured"] == "Brightpath Managed Services Ltd"
    limits = {limit["coverage"]: limit for limit in coverage["limits"]}
    assert limits["general liability"]["required"] == (
        "commercial general liability ≥ $2,000,000"
    )
    assert limits["general liability"]["found"] == "$2,000,000"
    assert limits["professional liability"]["found"] == "$1,000,000"
    endorsements = {e["name"] for e in coverage["endorsements"]}
    assert endorsements == {"additional insured", "waiver of subrogation"}


def test_renewal_draft_names_the_recorded_facts(samples):
    view = _brightpath_service(samples).certificate_watches(
        today=date(2026, 10, 10)
    )
    draft = view["certificates"][0]["renewal_request_draft"]
    assert draft.startswith("DRAFT")
    assert "not sent" in draft
    assert "Brightpath Managed Services Ltd" in draft
    assert "CTR-2029" in draft
    assert "Exhibit D" in draft
    assert "expires on 2026-12-31 (82 days from 2026-10-10)" in draft
    assert "commercial general liability ≥ $2,000,000" in draft
    assert "professional liability ≥ $1,000,000" in draft
    assert "additional insured endorsed" in draft
    assert "waiver of subrogation endorsed" in draft
    assert "before the current certificate expires" in draft


def test_expired_certificate_flags_expired_and_the_draft_says_so(samples):
    view = _brightpath_service(samples).certificate_watches(
        today=date(2027, 1, 15)
    )
    row = view["certificates"][0]
    # Expired is its own state, not the loud end of "expiring".
    assert row["state"] == "expired"
    assert row["overdue"] is True
    assert row["days_until"] == -15
    draft = row["renewal_request_draft"]
    assert "expired on 2026-12-31 (15 days before 2027-01-15)" in draft
    assert "as a matter of urgency" in draft
    assert "before the current certificate expires" not in draft


def _bare_record(cid: str, supersedes: str, expiry: str) -> ApprovalRecord:
    """A record whose review stored ONLY the expiry check — no
    limits, no endorsements, no named insured."""
    result = AgentResult(
        contract_id=cid,
        counterparty_name="Chain Co",
        exhibit_checks=[
            ExhibitCheck(
                name="insurance_certificate_expiry",
                found=expiry,
                status="consistent",
                exhibit_document_id="Exhibit A",
            )
        ],
    )
    return ApprovalRecord(result=result, contract={"supersedes": supersedes})


def test_draft_invents_no_fact_the_record_does_not_carry():
    records = [_bare_record("CTR-BARE-1", "", "2026-11-01")]
    rows = certificate_watches(records, date(2026, 10, 10), 90)
    assert len(rows) == 1
    row = rows[0]
    assert row["coverage"] == {
        "named_insured": "",
        "limits": [],
        "endorsements": [],
    }
    draft = row["renewal_request_draft"]
    assert "CTR-BARE-1" in draft and "2026-11-01" in draft
    # Nothing quoted that the record never recorded.
    assert "$" not in draft
    assert "endorsed" not in draft
    assert "as the insured" not in draft
    assert "the cover recorded on the review" in draft


def test_watch_row_names_the_chain_root_beside_the_operative_round():
    records = [
        _bare_record("CTR-CH-1", "", "2026-11-01"),
        _bare_record("CTR-CH-2", "CTR-CH-1", "2026-12-01"),
    ]
    rows = certificate_watches(records, date(2026, 10, 10), 90)
    assert [r["contract_id"] for r in rows] == ["CTR-CH-2"]
    row = rows[0]
    assert row["chain_root"] == "CTR-CH-1"
    assert "(chain root CTR-CH-1)" in row["renewal_request_draft"]


def test_expiring_endpoint_rows_carry_coverage_and_draft(samples):
    client = TestClient(app)
    payload = samples["CTR-2029"].model_dump(mode="json")
    assert client.post("/contracts/analyze", json=payload).status_code == 200
    res = client.get("/certificates/expiring", params={"window_days": 3650})
    assert res.status_code == 200
    row = next(
        r for r in res.json()["certificates"] if r["contract_id"] == "CTR-2029"
    )
    assert row["coverage"]["named_insured"] == "Brightpath Managed Services Ltd"
    assert row["renewal_request_draft"].startswith("DRAFT")
    assert row["state"] in ("expiring", "expired")
