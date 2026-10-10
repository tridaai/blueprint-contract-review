"""Renewal-certificate evidence (round 12): a renewal certificate
is checked against the ORIGINAL round's insurance requirement
set — not against whatever terms arrived with the renewal. A
renewal round whose draft restates the insurance clause at the
thinner cover its new certificate happens to carry passes its
own single-round checks while quietly dropping the cover the
original bargain required; the renewal check is the comparison
that catches it. And the renewal watch closes on that evidence
or not at all: a failed renewal leaves the watch open on the
last relied-upon expiry, flagged on the row.
"""

from __future__ import annotations

from datetime import date

import pytest

from contract_agent.certificate_renewal import build_certificate_renewal
from contract_agent.events import CollectingSink
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


def _round(samples, contract_id: str, cert_text: str,
           supersedes: str = "CTR-2029") -> ContractInput:
    """A round of the Brightpath chain carrying a chosen
    certificate text (the agreement stays CTR-2029's)."""
    data = samples["CTR-2029"].model_dump(mode="json")
    data["contract_id"] = contract_id
    data["supersedes"] = supersedes
    for doc in data["documents"]:
        if doc["document_id"] == "Exhibit D":
            doc["raw_text"] = cert_text
    return ContractInput.model_validate(data)


_CERT = (
    "Exhibit D — Certificate of Insurance\n\n"
    "Named insured: Brightpath Managed Services Ltd\n\n"
    "Commercial general liability insurance: $2,000,000 per occurrence.\n"
    "Professional liability insurance: {pl} per claim.\n\n"
    "The Customer is named as an additional insured under the general liability policy.\n"
    "{waiver}"
    "This certificate expires on December 31, 2027.\n"
)
_WAIVER = "The insurer provides a waiver of subrogation in favour of the Customer.\n"


def _cert(pl: str = "$1,000,000", waiver: bool = True) -> str:
    return _CERT.format(pl=pl, waiver=_WAIVER if waiver else "")


# ---------------------------------------------------------------------------
# The evidence comparison itself
# ---------------------------------------------------------------------------


def test_renewal_evidencing_the_original_set_is_consistent(samples):
    renewal = build_certificate_renewal(
        samples["CTR-2029"], _round(samples, "CTR-RN-1", _cert())
    )
    assert len(renewal) == 1
    check = renewal[0]
    assert check.name == "insurance_certificate_renewal"
    assert check.status == "consistent"
    assert "2027-12-31" in check.found
    assert "closes on this evidence" in check.detail


def test_renewal_with_a_dropped_limit_mismatches_the_original_terms(samples):
    renewal = build_certificate_renewal(
        samples["CTR-2029"], _round(samples, "CTR-RN-2", _cert(pl="$250,000"))
    )
    check = renewal[0]
    assert check.status == "mismatch"
    assert "$250,000" in check.detail
    assert "$1,000,000" in check.detail


def test_renewal_with_a_dropped_endorsement_mismatches(samples):
    renewal = build_certificate_renewal(
        samples["CTR-2029"], _round(samples, "CTR-RN-3", _cert(waiver=False))
    )
    check = renewal[0]
    assert check.status == "mismatch"
    assert "waiver of subrogation" in check.detail


def test_no_extension_of_cover_is_no_renewal(samples):
    same_expiry = _cert().replace("December 31, 2027", "December 31, 2026")
    assert build_certificate_renewal(
        samples["CTR-2029"], _round(samples, "CTR-RN-4", same_expiry)
    ) == []


def test_no_certificate_on_the_prior_round_is_no_renewal(samples):
    data = samples["CTR-2029"].model_dump(mode="json")
    data["documents"] = [d for d in data["documents"] if d["doc_type"] == "agreement"]
    prior = ContractInput.model_validate(data)
    assert build_certificate_renewal(prior, samples["CTR-2036"]) == []


# ---------------------------------------------------------------------------
# Through the service: the samples and the watch
# ---------------------------------------------------------------------------


def test_service_attaches_a_passing_renewal_and_the_watch_closes(samples):
    svc = _service()
    svc.analyze(samples["CTR-2029"])
    result = svc.analyze(samples["CTR-2036"])
    assert len(result.certificate_renewal) == 1
    assert result.certificate_renewal[0].status == "consistent"
    # 2026-12-01: the old expiry (2026-12-31) sat inside the
    # 90-day window; renewed on evidence, the watch reads the new
    # expiry — 2027-12-31 is far outside it.
    view = svc.certificate_watches(today=date(2026, 12, 1))
    assert view["count"] == 0
    # On a wide window the renewed certificate is the watch row,
    # marked as renewed on evidence.
    wide = svc.certificate_watches(window_days=400, today=date(2026, 12, 1))
    row = next(r for r in wide["certificates"] if r["contract_id"] == "CTR-2036")
    assert row["expiry_date"] == "2027-12-31"
    assert row["renewed"] is True


def test_service_quiet_drop_passes_its_own_checks_but_not_the_renewal(samples):
    svc = _service()
    svc.analyze(samples["CTR-2029"])
    result = svc.analyze(samples["CTR-2037"])
    # The renewal draft restated the clause at the thinner cover,
    # so the round's own certificate checks pass it in full...
    own = [c for c in result.exhibit_checks if c.name.startswith("insurance_")]
    assert own and all(c.status == "consistent" for c in own)
    # ...and the renewal check is the one that says what happened.
    assert len(result.certificate_renewal) == 1
    check = result.certificate_renewal[0]
    assert check.status == "mismatch"
    assert "professional liability" in check.detail.lower()
    assert "waiver of subrogation" in check.detail


def test_failed_renewal_keeps_the_watch_open_on_the_original_expiry(samples):
    svc = _service()
    svc.analyze(samples["CTR-2029"])
    svc.analyze(samples["CTR-2037"])
    view = svc.certificate_watches(today=date(2026, 12, 1))
    assert view["count"] == 1
    row = view["certificates"][0]
    # The watch never moved to the renewal's 2027 expiry: the
    # renewal evidenced nothing, so the relied-upon certificate
    # is still the original, expiring 2026-12-31.
    assert row["contract_id"] == "CTR-2029"
    assert row["expiry_date"] == "2026-12-31"
    assert row["renewal_evidence_failed"] is True
    assert row["renewal_contract_id"] == "CTR-2037"
    assert row["renewed"] is False
    # And the sweep still announces it.
    sink = CollectingSink()
    report = svc.dispatch_obligation_sweep(
        days=90, today=date(2026, 12, 1), event_sink=sink
    )
    assert report["certificate_expiring_count"] == 1
    events = [e for e in sink.events if e.type == "certificate_expiring"]
    assert len(events) == 1
    assert events[0].contract_id == "CTR-2029"


def test_renewal_not_attached_when_the_prior_round_is_not_stored(samples):
    svc = _service()
    result = svc.analyze(samples["CTR-2036"])
    assert result.certificate_renewal == []


def test_brief_carries_the_renewal_row(samples):
    svc = _service()
    svc.analyze(samples["CTR-2029"])
    svc.analyze(samples["CTR-2037"])
    brief = svc.negotiation_brief("CTR-2037")["markdown"]
    assert "insurance_certificate_renewal" in brief
    assert "mismatch" in brief
