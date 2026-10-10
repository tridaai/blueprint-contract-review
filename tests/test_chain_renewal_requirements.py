"""Chain-composed renewal requirements (round 14): the renewal
check used to read its requirement set from the PRIOR round's
agreement alone. On a real chain that is the wrong text twice
over: an amendment round that raised a limit and a later round
that merely carried it leave the requirement invisible to a
prior-round reading (the carrying round may restate nothing at
all), and what the prior round does state is cited from the
prior round even when an earlier amendment set it. Now the set
is composed walking the whole supersedes chain — each
requirement from the round that LAST SET it: a round sets a
limit when it first states it or states it changed, a
restatement at the standing amount merely carries it, and
endorsements once imposed persist through later silence.
"""

from __future__ import annotations

import pytest

from contract_agent.certificate_renewal import (
    _compose_requirement_set,
    build_certificate_renewal,
)
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


def _cert(pl="$1,000,000", gl="$2,000,000", year="2026", waiver=True) -> str:
    return _CERT.format(pl=pl, gl=gl, year=year, waiver=_WAIVER if waiver else "")


_UPLIFT = (
    "\n\n9. Insurance Uplift\n"
    "The Provider shall maintain professional liability insurance "
    "of not less than $3,000,000.\n"
)

_SHORT_AGREEMENT = (
    "1. Renewal Transmittal\n"
    "This round transmits the renewal documentation for the "
    "Brightpath managed services agreement. The commercial terms "
    "are as previously agreed between the parties.\n"
)


def _round(samples, contract_id: str, cert_text: str,
           supersedes: str = "", agreement: str | None = None) -> ContractInput:
    data = samples["CTR-2029"].model_dump(mode="json")
    data["contract_id"] = contract_id
    data["supersedes"] = supersedes
    for doc in data["documents"]:
        if doc["document_id"] == "Exhibit D":
            doc["raw_text"] = cert_text
        if doc["document_id"] == "AGR-1" and agreement is not None:
            doc["raw_text"] = agreement
    return ContractInput.model_validate(data)


def _chain(samples) -> tuple[ContractInput, ContractInput, ContractInput]:
    """Root (full terms) → amendment (PL uplift to $3M) → a
    transmittal round whose agreement restates nothing."""
    root = _round(samples, "CTR-CC-1", _cert())
    amendment = _round(
        samples, "CTR-CC-2", _cert(), supersedes="CTR-CC-1",
        agreement=samples["CTR-2029"].agreement_text() + _UPLIFT,
    )
    transmittal = _round(
        samples, "CTR-CC-3", _cert(), supersedes="CTR-CC-2",
        agreement=_SHORT_AGREEMENT,
    )
    return root, amendment, transmittal


# ---------------------------------------------------------------------------
# The composition rule itself
# ---------------------------------------------------------------------------


def test_composition_takes_each_requirement_from_its_setter(samples):
    root, amendment, transmittal = _chain(samples)
    required, setters = _compose_requirement_set([root, amendment, transmittal])
    assert required["general_liability"] == 2_000_000
    assert required["professional_liability"] == 3_000_000
    assert setters["general_liability"].contract_id == "CTR-CC-1"
    assert setters["professional_liability"].contract_id == "CTR-CC-2"
    # Endorsements were imposed by the root and persist through
    # the transmittal round's silence.
    assert setters["additional insured"].contract_id == "CTR-CC-1"
    assert setters["waiver of subrogation"].contract_id == "CTR-CC-1"
    assert setters["named_insured"].contract_id == "CTR-CC-1"


def test_composition_restatement_carries_and_change_moves(samples):
    root, amendment, _ = _chain(samples)
    carried = _round(
        samples, "CTR-CC-4", _cert(), supersedes="CTR-CC-2",
        agreement=samples["CTR-2029"].agreement_text() + _UPLIFT,
    )
    _, setters = _compose_requirement_set([root, amendment, carried])
    # The uplift restated at the same amount is carried, not set:
    # provenance stays with the amendment.
    assert setters["professional_liability"].contract_id == "CTR-CC-2"
    lowered = _round(
        samples, "CTR-CC-5", _cert(), supersedes="CTR-CC-2",
        agreement=(
            "1. Insurance Amendment\n"
            "The Provider shall maintain professional liability "
            "insurance of not less than $500,000.\n"
        ),
    )
    required, setters = _compose_requirement_set([root, lowered])
    # A short amendment round stating the cover at a different
    # amount sets the requirement anew — at its amount, cited
    # from it. (Within one round the reader takes the highest
    # statement, per the round-13 placement rule; across rounds
    # the composed value is the setter round's statement.)
    assert required["professional_liability"] == 500_000
    assert setters["professional_liability"].contract_id == "CTR-CC-5"
    # What the amendment round does not state stays the root's.
    assert setters["general_liability"].contract_id == "CTR-CC-1"


# ---------------------------------------------------------------------------
# The renewal judged on the composed set
# ---------------------------------------------------------------------------


def test_prior_round_alone_states_nothing_but_the_chain_judges(samples):
    root, amendment, transmittal = _chain(samples)
    renewal = _round(
        samples, "CTR-CC-6",
        _cert(pl="$1,500,000", year="2027"), supersedes="CTR-CC-3",
    )
    # The pre-composition reading: the transmittal round states
    # no requirement set, so there is nothing to check against.
    assert build_certificate_renewal(transmittal, renewal) == []
    composed = build_certificate_renewal(
        transmittal, renewal, chain=[root, amendment, transmittal]
    )
    assert len(composed) == 1
    check = composed[0]
    assert check.status == "mismatch"
    # $1,500,000 clears the root's $1,000,000 and drops the
    # amendment's $3,000,000 — judged, and cited, from round 2.
    assert (
        "professional liability cover stands at $1,500,000, below "
        "the $3,000,000 the original terms require "
        "(set by CTR-CC-2 §9 (Insurance Uplift))"
    ) in check.detail


def test_carried_uplift_is_cited_from_the_amendment_not_the_restatement(samples):
    root, amendment, _ = _chain(samples)
    carried = _round(
        samples, "CTR-CC-7", _cert(), supersedes="CTR-CC-2",
        agreement=samples["CTR-2029"].agreement_text() + _UPLIFT,
    )
    renewal = _round(
        samples, "CTR-CC-8",
        _cert(pl="$3,000,000", year="2027"), supersedes="CTR-CC-7",
    )
    composed = build_certificate_renewal(
        carried, renewal, chain=[root, amendment, carried]
    )
    check = composed[0]
    assert check.status == "consistent"
    assert "professional liability at $3,000,000 (set by CTR-CC-2 §9 (Insurance Uplift))" in check.detail
    assert "commercial general liability at $2,000,000 (set by CTR-CC-1 §7 (Insurance))" in check.detail


# ---------------------------------------------------------------------------
# Through the service: the walk is the store's chain
# ---------------------------------------------------------------------------


def test_service_composes_across_the_stored_chain(samples):
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    root, amendment, transmittal = _chain(samples)
    svc.analyze(root)
    svc.analyze(amendment)
    svc.analyze(transmittal)
    result = svc.analyze(
        _round(samples, "CTR-CC-9", _cert(pl="$1,500,000", year="2027"),
               supersedes="CTR-CC-3")
    )
    assert len(result.certificate_renewal) == 1
    check = result.certificate_renewal[0]
    assert check.status == "mismatch"
    assert "set by CTR-CC-2 §9 (Insurance Uplift)" in check.detail
    step = next(t for t in result.trace if t.name == "certificate_renewal")
    assert "chain-composed" in step.title
