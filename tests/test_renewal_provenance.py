"""Renewal-requirement provenance (round 13): the renewal check
judges a renewal certificate against the requirement set the
earlier rounds imposed — and now NAMES where each requirement
came from: the round that set it and the clause that states
it, per requirement, on the check's detail. On a long chain
this is the evidence chain counsel needs: a limit raised by a
mid-chain amendment is judged from the amendment ("set by
CTR-PV-2 §9 (Insurance Uplift)"), not attributed to the root
round's clause, and a dropped endorsement names the clause
that required it.
"""

from __future__ import annotations

import pytest

from contract_agent.certificate_renewal import (
    _requirement_provenance,
    build_certificate_renewal,
)
from contract_agent.exhibit_review import _coverage_amounts
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


_CERT = (
    "Exhibit D — Certificate of Insurance\n\n"
    "Named insured: Brightpath Managed Services Ltd\n\n"
    "Commercial general liability insurance: {gl} per occurrence.\n"
    "Professional liability insurance: {pl} per claim.\n\n"
    "The Customer is named as an additional insured under the general liability policy.\n"
    "{waiver}"
    "This certificate expires on December 31, {year}.\n"
)
_WAIVER = "The insurer provides a waiver of subrogation in favour of the Customer.\n"


def _cert(gl: str = "$2,000,000", pl: str = "$1,000,000",
          waiver: bool = True, year: str = "2027") -> str:
    return _CERT.format(
        gl=gl, pl=pl, waiver=_WAIVER if waiver else "", year=year
    )


def _round(samples, contract_id: str, cert_text: str,
           supersedes: str = "CTR-2029", agreement: str | None = None,
           base: str = "CTR-2029") -> ContractInput:
    data = samples[base].model_dump(mode="json")
    data["contract_id"] = contract_id
    data["supersedes"] = supersedes
    for doc in data["documents"]:
        if doc["document_id"] == "Exhibit D":
            doc["raw_text"] = cert_text
        if doc["doc_type"] == "agreement" and agreement is not None:
            doc["raw_text"] = agreement
    return ContractInput.model_validate(data)


# ---------------------------------------------------------------------------
# The provenance read itself
# ---------------------------------------------------------------------------


def test_provenance_places_every_requirement_in_its_clause(samples):
    prior = samples["CTR-2029"]
    provenance = _requirement_provenance(
        prior, _coverage_amounts(prior.agreement_text())
    )
    assert provenance["general_liability"] == "CTR-2029 §7 (Insurance)"
    assert provenance["professional_liability"] == "CTR-2029 §7 (Insurance)"
    assert provenance["additional insured"] == "CTR-2029 §7 (Insurance)"
    assert provenance["waiver of subrogation"] == "CTR-2029 §7 (Insurance)"
    assert provenance["named_insured"] == "CTR-2029 §7 (Insurance)"


# ---------------------------------------------------------------------------
# On the check: consistent enumerates, drops name their source
# ---------------------------------------------------------------------------


def test_consistent_renewal_enumerates_requirements_with_provenance(samples):
    renewal = build_certificate_renewal(
        samples["CTR-2029"], _round(samples, "CTR-PV-A", _cert())
    )
    check = renewal[0]
    assert check.status == "consistent"
    assert "set by CTR-2029 §7 (Insurance)" in check.detail
    assert "commercial general liability at $2,000,000" in check.detail
    assert "professional liability at $1,000,000" in check.detail
    assert "named insured" in check.detail


def test_dropped_limit_names_the_clause_that_set_it(samples):
    renewal = build_certificate_renewal(
        samples["CTR-2029"],
        _round(samples, "CTR-PV-B", _cert(pl="$250,000")),
    )
    check = renewal[0]
    assert check.status == "mismatch"
    assert (
        "professional liability cover stands at $250,000, below the "
        "$1,000,000 the original terms require "
        "(set by CTR-2029 §7 (Insurance))"
    ) in check.detail


def test_dropped_endorsement_names_the_clause_that_required_it(samples):
    renewal = build_certificate_renewal(
        samples["CTR-2029"],
        _round(samples, "CTR-PV-C", _cert(waiver=False)),
    )
    check = renewal[0]
    assert check.status == "mismatch"
    assert (
        "the waiver of subrogation endorsement the original terms "
        "require (set by CTR-2029 §7 (Insurance)) is not shown on it"
    ) in check.detail


# ---------------------------------------------------------------------------
# The amendment case: judged from the amendment, not the root
# ---------------------------------------------------------------------------


_UPLIFT = (
    "\n\n9. Insurance Uplift\n"
    "The Provider shall maintain professional liability insurance "
    "of not less than $3,000,000.\n"
)


def _amended_round(samples) -> ContractInput:
    """Round 2 of a chain: CTR-2029's agreement, plus an uplift
    section raising professional liability to $3,000,000."""
    base = samples["CTR-2029"]
    agreement = base.agreement_text() + _UPLIFT
    return _round(
        samples, "CTR-PV-2", _cert(year="2026"),
        supersedes="CTR-2029", agreement=agreement,
    )


def test_uplift_requirement_is_placed_at_the_amendment_clause(samples):
    prior = _amended_round(samples)
    provenance = _requirement_provenance(
        prior, _coverage_amounts(prior.agreement_text())
    )
    # The $3,000,000 requirement lives in §9, the uplift — even
    # though §7 also states professional liability (at $1,000,000).
    assert provenance["professional_liability"] == "CTR-PV-2 §9 (Insurance Uplift)"
    assert provenance["general_liability"] == "CTR-PV-2 §7 (Insurance)"


def test_renewal_judged_against_the_uplift_names_the_amendment(samples):
    prior = _amended_round(samples)
    # A renewal evidencing the ROOT terms in full — GL at the
    # root's figure but professional liability at $1,000,000 —
    # meets CTR-2029's bargain and drops the amendment's.
    renewal = build_certificate_renewal(
        prior,
        _round(samples, "CTR-PV-3", _cert(gl="$500,000", year="2028"),
               supersedes="CTR-PV-2", base="CTR-2029"),
    )
    check = renewal[0]
    assert check.status == "mismatch"
    assert (
        "professional liability cover stands at $1,000,000, below "
        "the $3,000,000 the original terms require "
        "(set by CTR-PV-2 §9 (Insurance Uplift))"
    ) in check.detail
    # And the general-liability drop is attributed to its own
    # clause in the same round — provenance is per requirement.
    assert (
        "commercial general liability cover stands at $500,000, "
        "below the $2,000,000 the original terms require "
        "(set by CTR-PV-2 §7 (Insurance))"
    ) in check.detail


# ---------------------------------------------------------------------------
# Through the service: the quiet-drop sample carries its chain
# ---------------------------------------------------------------------------


def test_service_quiet_drop_detail_carries_provenance(samples):
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(samples["CTR-2029"])
    result = svc.analyze(samples["CTR-2037"])
    check = result.certificate_renewal[0]
    assert check.status == "mismatch"
    assert "set by CTR-2029 §7 (Insurance)" in check.detail
