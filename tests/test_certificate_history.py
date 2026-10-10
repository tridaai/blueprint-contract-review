"""Certificate biography (round 16): the annex history gave
the two living lists their timelines in round 15; the
certificate chain — each round's requirement set and
certificate, each renewal's evidence verdict, the watch the
renewals move or fail to move — is the third living document
and read only round by round. The biography lays it out in
the annex history's shape: rounds, steps, summary, computed
with the renewal check's own composition and the exhibit
checks' own readings, so the timeline cannot disagree with
the findings it narrates. The watch's operative reading for
the chain rides on the view: a renewal that failed the
evidence check leaves the watch standing on the earlier
expiry, and the biography says so.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from contract_agent.certificate_history import build_certificate_history
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


# ---------------------------------------------------------------------------
# The pure timeline: rounds, steps, drops
# ---------------------------------------------------------------------------


def test_rounds_carry_each_rounds_requirements_and_certificate(samples):
    history = build_certificate_history(
        [samples["CTR-2029"], samples["CTR-2037"]]
    )
    assert history is not None
    first, second = history["rounds"]
    assert first["contract_id"] == "CTR-2029"
    required = {row["key"]: row["amount"] for row in first["requirements"]["limits"]}
    assert required["general_liability"] == 2_000_000
    assert required["professional_liability"] == 1_000_000
    assert first["requirements"]["additional_insured"] is True
    assert first["requirements"]["waiver_of_subrogation"] is True
    assert first["requirements"]["named_insured"] == (
        "Brightpath Managed Services Ltd"
    )
    stated = {
        row["key"]: row["amount"] for row in second["certificate"]["limits"]
    }
    # CTR-2037's certificate quietly thins the professional
    # liability cover and drops the waiver — the biography
    # states both, from the certificate itself.
    assert stated["professional_liability"] == 250_000
    assert second["certificate"]["endorsements"]["waiver_of_subrogation"] is not True
    # No results handed in: no verdicts are claimed.
    assert first["checks"] == [] and second["renewal"] is None


def test_a_failed_renewals_drops_are_named_with_provenance(samples):
    history = build_certificate_history(
        [samples["CTR-2029"], samples["CTR-2037"]]
    )
    step = history["steps"][0]
    assert step["renewal_arrived"] is True
    drops = {d["requirement"]: d for d in step["drops"]}
    assert "professional_liability" in drops
    assert "$1,000,000" in drops["professional_liability"]["detail"]
    assert "$250,000" in drops["professional_liability"]["detail"]
    assert "set_by" in drops["professional_liability"]
    assert "CTR-2029" in drops["professional_liability"]["set_by"]
    assert "waiver of subrogation" in drops
    summary = " ".join(history["summary"])
    assert "1 renewal(s) arrived" in summary


def test_a_chain_without_insurance_has_no_certificate_biography(samples):
    assert build_certificate_history([samples["CTR-2001"]]) is None
    svc = _service()
    svc.analyze(samples["CTR-2001"])
    assert svc.annex_history("CTR-2001")["certificates"] is None


# ---------------------------------------------------------------------------
# Through the service: verdicts on the steps, the watch reading
# ---------------------------------------------------------------------------


def test_passing_renewal_reads_consistent_and_the_watch_moves(samples):
    svc = _service()
    svc.analyze(samples["CTR-2029"])
    svc.analyze(samples["CTR-2036"])
    history = svc.annex_history("CTR-2036")
    certs = history["certificates"]
    assert certs is not None
    step = certs["steps"][0]
    assert step["renewal_arrived"] is True
    assert step["verdict"] == "consistent"
    assert step["drops"] == []
    assert "evidences the requirement set" in step["summary"]
    # The stored single-round verdicts ride on the round rows.
    assert certs["rounds"][1]["renewal"]["status"] == "consistent"
    assert any(
        c["name"] == "insurance_certificate_expiry"
        for c in certs["rounds"][0]["checks"]
    )
    # The watch moved to the renewal's expiry, on the evidence.
    watch = certs["watch"]
    assert watch["contract_id"] == "CTR-2036"
    assert watch["renewed"] is True
    assert "renewal_evidence_failed" not in watch


def test_failed_renewal_reads_mismatch_and_the_watch_stays_open(samples):
    svc = _service()
    svc.analyze(samples["CTR-2029"])
    svc.analyze(samples["CTR-2037"])
    certs = svc.annex_history("CTR-2037")["certificates"]
    step = certs["steps"][0]
    assert step["verdict"] == "mismatch"
    assert "the watch stays open on the earlier expiry" in step["summary"]
    summary = " ".join(certs["summary"])
    assert "failed the evidence check" in summary
    assert "CTR-2037" in summary
    watch = certs["watch"]
    assert watch["contract_id"] == "CTR-2029"
    assert watch["renewal_evidence_failed"] is True
    assert watch["renewal_contract_id"] == "CTR-2037"


def test_a_third_round_extends_the_timeline(samples):
    svc = _service()
    svc.analyze(samples["CTR-2029"])
    svc.analyze(samples["CTR-2036"])
    data = samples["CTR-2036"].model_dump(mode="json")
    data["contract_id"] = "CTR-CH-3"
    data["supersedes"] = "CTR-2036"
    for doc in data["documents"]:
        if doc["document_id"] == "Exhibit D":
            doc["raw_text"] = doc["raw_text"].replace(
                "December 31, 2027", "December 31, 2028"
            )
    svc.analyze(ContractInput.model_validate(data))
    certs = svc.annex_history("CTR-CH-3")["certificates"]
    assert [r["contract_id"] for r in certs["rounds"]] == [
        "CTR-2029", "CTR-2036", "CTR-CH-3",
    ]
    assert len(certs["steps"]) == 2
    assert all(s["renewal_arrived"] for s in certs["steps"])
    assert "2 renewal(s) arrived" in " ".join(certs["summary"])
    assert certs["watch"]["contract_id"] == "CTR-CH-3"


def test_annex_history_endpoint_carries_the_biography(samples):
    from contract_agent.api import app

    client = TestClient(app)
    for contract in (samples["CTR-2029"], samples["CTR-2037"]):
        res = client.post(
            "/contracts/analyze", json=contract.model_dump(mode="json")
        )
        assert res.status_code == 200
    body = client.get("/contracts/CTR-2037/annex-history").json()
    assert body["certificates"]["steps"][0]["verdict"] == "mismatch"
    assert body["certificates"]["watch"]["renewal_evidence_failed"] is True
