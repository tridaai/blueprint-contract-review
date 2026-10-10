"""Insurance-certificate exhibit checks (round 8): the agreement
states the insurance the counterparty must carry and furnish; the
certificate exhibit is checked against every statement — named
insured, limits per coverage, expiry, and the endorsements."""

from __future__ import annotations

import pytest

from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


def _by_name(result) -> dict[str, object]:
    return {c.name: c for c in result.exhibit_checks}


_INSURANCE_CHECKS = {
    "insurance_named_insured",
    "insurance_limits_general_liability",
    "insurance_limits_professional_liability",
    "insurance_certificate_expiry",
    "insurance_additional_insured",
    "insurance_waiver_of_subrogation",
}


def test_consistent_certificate_passes_clean(samples):
    result = _service().analyze(samples["CTR-2027"])
    checks = _by_name(result)
    assert set(checks) == _INSURANCE_CHECKS
    assert all(c.status == "consistent" for c in result.exhibit_checks)
    assert checks["insurance_named_insured"].found == "Halcyon Integration Services LLC"
    assert checks["insurance_limits_general_liability"].found == "$2,000,000"
    assert checks["insurance_certificate_expiry"].found == "2027-03-31"
    assert all(c.exhibit_document_id == "Exhibit D" for c in result.exhibit_checks)
    assert result.facts.exhibit_mismatches == []
    assert result.facts.posture == "aligned"


def test_bad_certificate_is_flagged_line_by_line(samples):
    result = _service().analyze(samples["CTR-2028"])
    checks = _by_name(result)
    assert set(checks) == _INSURANCE_CHECKS
    # A differently-suffixed entity is a different insured.
    named = checks["insurance_named_insured"]
    assert named.status == "mismatch"
    assert named.found == "Halcyon Integration Services, Inc."
    assert named.promised == "Halcyon Integration Services LLC"
    # A quarter of the required limit is not the required limit.
    limits = checks["insurance_limits_general_liability"]
    assert limits.status == "mismatch"
    assert limits.found == "$500,000"
    # Cover the certificate never mentions is missing, not mismatched.
    assert checks["insurance_limits_professional_liability"].status == "missing_in_exhibit"
    # Expired before the agreement was even received.
    expiry = checks["insurance_certificate_expiry"]
    assert expiry.status == "mismatch"
    assert expiry.found == "2026-03-31"
    # Endorsements the agreement requires and the certificate omits.
    assert checks["insurance_additional_insured"].status == "missing_in_exhibit"
    assert checks["insurance_waiver_of_subrogation"].status == "missing_in_exhibit"
    # The clauses themselves are preferred — the contradiction is
    # between the agreement and its exhibit, and it floors the posture.
    assert result.facts.posture == "negotiable"
    assert sorted(result.facts.exhibit_mismatches) == sorted(_INSURANCE_CHECKS)


def test_promised_certificate_absent_is_exhibit_missing(samples):
    contract = samples["CTR-2027"].model_copy(deep=True)
    contract.documents = [d for d in contract.documents if d.doc_type == "agreement"]
    result = _service().analyze(contract)
    checks = _by_name(result)
    assert set(checks) == _INSURANCE_CHECKS
    assert all(c.status == "exhibit_missing" for c in result.exhibit_checks)
    assert result.facts.exhibit_absent is True
    assert result.facts.package_complete is False


def test_no_insurance_reliance_generates_no_checks(samples):
    # CTR-2001 states no insurance requirement and promises no
    # certificate — nothing cross-document to check.
    result = _service().analyze(samples["CTR-2001"])
    assert not any(
        c.name.startswith("insurance_") for c in result.exhibit_checks
    )


def test_two_coverages_in_one_sentence_keep_their_own_amounts(samples):
    # The agreement states both coverages in ONE sentence with two
    # amounts; the parser must not hand the first amount to both.
    result = _service().analyze(samples["CTR-2027"])
    checks = _by_name(result)
    assert checks["insurance_limits_general_liability"].promised.endswith("$2,000,000")
    assert checks["insurance_limits_professional_liability"].promised.endswith("$1,000,000")
