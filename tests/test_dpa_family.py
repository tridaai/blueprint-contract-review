"""Standalone DPA playbook family (round 7): a data processing
addendum reviewed as the PRIMARY document gets its own clause
matrix — sub-processor objection windows, transfer mechanisms,
audit rights, deletion-on-exit — instead of being squeezed through
the MSA's single data-protection row."""

from __future__ import annotations

import pytest

from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput, DocumentInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore
from helpers import contract


@pytest.fixture(scope="module")
def samples() -> dict:
    return {c.contract_id: c for c in sample_contract_models()}


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


def _by_type(result) -> dict:
    return {v.clause_type: v for v in result.clause_verdicts}


# ---------------------------------------------------------------------------
# Intake: the doc_type and its aliases
# ---------------------------------------------------------------------------


def test_dpa_doc_type_normalises_from_its_names():
    for raw in ("dpa", "DPA", "Data Processing Addendum",
                "data_processing_agreement", "Data Protection Addendum"):
        model = ContractInput(contract_id="CTR-X", doc_type=raw)
        assert model.doc_type == "dpa", raw


# ---------------------------------------------------------------------------
# The clean standalone DPA (CTR-2023)
# ---------------------------------------------------------------------------


def test_clean_dpa_reviews_aligned_on_its_own_matrix(samples):
    svc = _service()
    result = svc.analyze(samples["CTR-2023"])
    assert result.doc_type == "dpa"
    assert result.facts.posture == "aligned"
    assert result.facts.missing_standard_clauses == []
    verdicts = _by_type(result)
    for clause_type in (
        "data_protection", "subprocessor_terms", "data_transfer",
        "audit_rights", "data_deletion", "confidentiality",
    ):
        assert verdicts[clause_type].position == "preferred", clause_type
    # The playbook citations are the DPA family's own entries.
    assert verdicts["subprocessor_terms"].playbook_refs == ["PB-SUB-01"]
    assert verdicts["data_transfer"].playbook_refs == ["PB-TRF-01"]
    assert verdicts["audit_rights"].playbook_refs == ["PB-AUD-01"]
    assert verdicts["data_deletion"].playbook_refs == ["PB-DEL-01"]


def test_dpa_objection_window_lands_on_the_obligations_register(samples):
    svc = _service()
    result = svc.analyze(samples["CTR-2023"])
    windows = [
        o for o in result.obligations
        if o.kind == "notice_period" and o.days == 30
    ]
    assert len(windows) == 1
    assert "sub-processor objection window" in windows[0].detail


def test_dpa_fallback_tiers_exist_for_the_family(samples):
    # Flagged DPA clauses propose the family's own tier language —
    # walk-away verdicts go straight to the family's walk-away
    # lines, lesser flags to its first fallbacks; no diff is empty.
    svc = _service()
    result = svc.analyze(samples["CTR-2024"])
    diffs = {d.clause_type: d for d in result.clause_diffs}
    assert diffs["subprocessor_terms"].proposed_tier == 2  # walk-away verdict
    assert "thirty (30) day objection window" in diffs["subprocessor_terms"].tier_language
    assert diffs["data_transfer"].proposed_tier == 2  # walk-away verdict
    assert "transfer mechanism" in diffs["data_transfer"].tier_language
    assert diffs["data_deletion"].proposed_tier == 1  # review_required → first fallback
    assert "at the Controller's choice" in diffs["data_deletion"].tier_language


# ---------------------------------------------------------------------------
# The adversarial standalone DPA (CTR-2024)
# ---------------------------------------------------------------------------


def test_adversarial_dpa_flags_the_machinery_and_the_injection(samples):
    svc = _service()
    result = svc.analyze(samples["CTR-2024"])
    assert result.facts.posture == "escalation_required"
    assert result.facts.missing_standard_clauses == []
    verdicts = _by_type(result)
    assert verdicts["subprocessor_terms"].position == "walk_away"
    assert verdicts["data_transfer"].position == "walk_away"
    assert verdicts["audit_rights"].position == "walk_away"
    assert verdicts["data_deletion"].position == "review_required"
    assert verdicts["data_protection"].position == "acceptable_fallback"
    # The instruction planted in the transfers clause is screened
    # and flagged — and the clause is still judged on its substance
    # (walk_away above), not on the instruction it carried.
    assert result.injection_flags
    assert any(
        "classify every clause" in flag for flag in result.injection_flags
    )


# ---------------------------------------------------------------------------
# Absence on the DPA matrix is a finding, like any other family
# ---------------------------------------------------------------------------


def test_dpa_missing_family_clauses_are_named():
    text = """1. Data Protection and Security
The Processor shall protect personal data in accordance with this Data Processing Addendum and shall notify the Controller within 72 hours of a personal data breach.
"""
    svc = _service()
    result = svc.analyze(
        contract(
            text,
            contract_id="CTR-DPA-THIN",
            doc_type="dpa",
            title="Data Processing Addendum",
            documents=[
                DocumentInput(
                    doc_type="agreement", document_id="AGR-1", raw_text=text
                )
            ],
        )
    )
    missing = set(result.facts.missing_standard_clauses)
    assert {"subprocessor_terms", "data_transfer", "audit_rights",
            "data_deletion", "confidentiality"} <= missing
    assert "data_protection" not in missing
