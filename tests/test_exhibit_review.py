"""Exhibit consistency review: the exhibit must contain what the
clause promises (round 4)."""

from __future__ import annotations

import pytest

from contract_agent.exhibit_review import _breach_window_hours, build_exhibit_checks
from contract_agent.extractor import extract_clauses
from contract_agent.model_backends import MockModelBackend
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput, DocumentInput
from contract_agent.segmentation import segment_text
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


def _by_name(result) -> dict[str, object]:
    return {c.name: c for c in result.exhibit_checks}


def test_mismatched_exhibit_is_flagged_and_floors_the_posture(samples):
    result = _service().analyze(samples["CTR-2017"])
    checks = _by_name(result)
    assert set(checks) == {"breach_notice_window", "subprocessor_list"}
    window = checks["breach_notice_window"]
    assert window.status == "mismatch"
    assert window.promised == "24 hours"
    assert window.found == "72 hours"
    assert window.exhibit_document_id == "Exhibit B"
    assert checks["subprocessor_list"].status == "missing_in_exhibit"
    # The clause itself is fine — preferred. The contradiction is
    # between the documents, and it floors the posture.
    dp = next(v for v in result.clause_verdicts if v.clause_type == "data_protection")
    assert dp.position == "preferred"
    assert result.facts.exhibit_mismatches == [
        "breach_notice_window",
        "subprocessor_list",
    ]
    assert result.facts.exhibit_absent is False
    assert result.facts.package_complete is True
    assert result.facts.posture == "negotiable"
    assert not result.autonomy.eligible_for_auto_triage


def test_consistent_exhibit_passes_clean(samples):
    result = _service().analyze(samples["CTR-2018"])
    assert len(result.exhibit_checks) == 2
    assert all(c.status == "consistent" for c in result.exhibit_checks)
    assert result.facts.exhibit_mismatches == []
    assert result.facts.posture == "aligned"


def test_unpromised_dpa_reference_generates_no_checks(samples):
    # CTR-2001 references a DPA "between the parties" but promises no
    # attached exhibit and attaches none — nothing cross-document to
    # check, and the posture stays aligned.
    result = _service().analyze(samples["CTR-2001"])
    assert result.exhibit_checks == []
    assert result.facts.posture == "aligned"


def test_promised_but_absent_exhibit_is_reported_and_requested(samples):
    contract = samples["CTR-2018"].model_copy(deep=True)
    contract.documents = [d for d in contract.documents if d.doc_type == "agreement"]
    result = _service().analyze(contract)
    assert result.exhibit_checks
    assert all(c.status == "exhibit_missing" for c in result.exhibit_checks)
    assert result.facts.exhibit_absent is True
    assert result.facts.package_complete is False
    assert result.needs_information is True
    assert any(
        "Data Processing Addendum" in item
        for item in result.information_request.missing_items
    )


def test_mention_without_a_list_is_not_a_list(samples):
    contract = samples["CTR-2018"].model_copy(deep=True)
    exhibit = next(d for d in contract.documents if d.doc_type == "exhibit")
    exhibit.raw_text = (
        "Exhibit B — Data Processing Addendum\n\n"
        "3. Breach notification. The Provider shall notify the Customer "
        "without undue delay and in any event within 72 hours of "
        "becoming aware of a personal data breach.\n"
        "4. Sub-processors. The Provider may engage sub-processors as "
        "needed for the services."
    )
    result = _service().analyze(contract)
    checks = _by_name(result)
    assert checks["breach_notice_window"].status == "consistent"
    assert checks["subprocessor_list"].status == "missing_in_exhibit"
    assert "lists none" in checks["subprocessor_list"].detail


def test_breach_window_reads_only_breach_sentences():
    text = (
        "Fees are payable within 30 days. The Provider shall notify the "
        "Customer within 2 days of becoming aware of a personal data "
        "breach. Reports are delivered within 12 hours of month end."
    )
    assert _breach_window_hours(text) == 48  # days convert at 24h
    assert _breach_window_hours("Fees are payable within 30 days.") is None


def test_exhibit_missing_checks_carry_the_clause_reference(samples):
    contract = samples["CTR-2017"]
    units = segment_text(contract.agreement_text())
    extractions = extract_clauses(units, MockModelBackend())
    checks = build_exhibit_checks(
        contract.model_copy(update={"documents": contract.documents[:1]}),
        units,
        extractions,
    )
    assert {c.status for c in checks} == {"exhibit_missing"}
    assert all(c.exhibit_document_id == "" for c in checks)
