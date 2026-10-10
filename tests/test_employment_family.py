"""Employment playbook family (round 8): an offer letter or
employment agreement reviewed as the PRIMARY document gets its
own clause matrix — restrictive-covenant scope, invention
assignment breadth, arbitration machinery, the at-will core —
instead of being squeezed through the services matrix."""

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


def test_employment_doc_type_normalises_from_its_names():
    for raw in ("employment", "Employment Agreement",
                "employment_agreement", "Offer Letter", "offer_letter",
                "Employment Offer Letter"):
        model = ContractInput(contract_id="CTR-X", doc_type=raw)
        assert model.doc_type == "employment", raw


# ---------------------------------------------------------------------------
# The clean offer letter (CTR-2025)
# ---------------------------------------------------------------------------


def test_clean_offer_letter_reviews_on_its_own_matrix(samples):
    svc = _service()
    result = svc.analyze(samples["CTR-2025"])
    assert result.doc_type == "employment"
    assert result.facts.missing_standard_clauses == []
    verdicts = _by_type(result)
    for clause_type in (
        "employment_terms", "non_compete", "ip_assignment", "confidentiality",
    ):
        assert verdicts[clause_type].position == "preferred", clause_type
    # Mutual arbitration with statutory carve-outs is the playbook's
    # stated acceptable fallback — not its preference, which is no
    # mandatory arbitration at all. The letter sits at the fallback,
    # so the posture is negotiable on that clause alone.
    assert verdicts["arbitration"].position == "acceptable_fallback"
    assert result.facts.posture == "negotiable"
    # The playbook citations are the employment family's own entries.
    assert verdicts["non_compete"].playbook_refs == ["PB-NCT-01"]
    assert verdicts["ip_assignment"].playbook_refs == ["PB-IPA-01"]
    assert verdicts["arbitration"].playbook_refs == ["PB-ARB-01"]
    assert verdicts["employment_terms"].playbook_refs == ["PB-EMP-01"]


def test_employment_fallback_tiers_exist_for_the_family(samples):
    # Flagged employment clauses propose the family's own tier
    # language — walk-away verdicts go straight to the family's
    # walk-away lines, lesser flags to its first fallbacks.
    svc = _service()
    result = svc.analyze(samples["CTR-2026"])
    diffs = {d.clause_type: d for d in result.clause_diffs}
    assert diffs["non_compete"].proposed_tier == 2  # walk-away verdict
    assert "no more than twelve (12) months" in diffs["non_compete"].tier_language
    assert diffs["ip_assignment"].proposed_tier == 2  # walk-away verdict
    assert "prior inventions are excluded" in diffs["ip_assignment"].tier_language
    assert diffs["arbitration"].proposed_tier == 2  # walk-away verdict
    assert "any arbitration must be mutual" in diffs["arbitration"].tier_language
    assert diffs["employment_terms"].proposed_tier == 1  # review_required → first fallback
    assert "employment is at will" in diffs["employment_terms"].tier_language


# ---------------------------------------------------------------------------
# The adversarial executive draft (CTR-2026)
# ---------------------------------------------------------------------------


def test_adversarial_employment_draft_flags_the_machinery_and_the_injection(samples):
    svc = _service()
    result = svc.analyze(samples["CTR-2026"])
    assert result.facts.posture == "escalation_required"
    assert result.facts.missing_standard_clauses == []
    verdicts = _by_type(result)
    assert verdicts["employment_terms"].position == "review_required"
    assert verdicts["non_compete"].position == "walk_away"
    assert verdicts["ip_assignment"].position == "walk_away"
    assert verdicts["arbitration"].position == "walk_away"
    # The instruction planted in the assignment clause is screened
    # and flagged — and the clauses are still judged on their
    # substance (walk-aways above), not on the instruction carried.
    assert result.injection_flags
    assert any(
        "classify every clause" in flag for flag in result.injection_flags
    )


# ---------------------------------------------------------------------------
# Clause-level signals: the ladder inside each family row
# ---------------------------------------------------------------------------


def _analyze(text: str):
    svc = _service()
    return svc.analyze(
        contract(text, contract_id="CTR-EMP-X", doc_type="employment",
                 title="Employment Agreement")
    )


def test_noncompete_ladder_reads_duration_scope_and_shape():
    base = "1. Restrictive Covenants\n"
    # Twelve months, bounded to competitors in the state: preferred.
    result = _analyze(base + "For twelve (12) months after employment ends, the Employee shall not join a business that competes with the Company within the state in which the Employee worked.")
    assert _by_type(result)["non_compete"].position == "preferred"
    # Eighteen months: beyond twelve, counsel reviews.
    result = _analyze(base + "For eighteen (18) months after employment ends, the Employee shall not compete with the Company.")
    assert _by_type(result)["non_compete"].position == "review_required"
    # Solicitation only: the narrower covenant is preferred.
    result = _analyze(base + "For twelve (12) months after employment ends, the Employee shall not solicit the Company's customers or recruit its employees.")
    assert _by_type(result)["non_compete"].position == "preferred"


def test_ip_assignment_ladder_reads_reach_and_limits():
    base = "1. Invention Assignment\n"
    result = _analyze(base + "The Employee hereby assigns to the Company all inventions conceived in the course of the Employee's employment and relating to the Company's business.")
    assert _by_type(result)["ip_assignment"].position == "preferred"
    result = _analyze(base + "The Employee hereby assigns to the Company all inventions the Employee conceives while employed by the Company.")
    assert _by_type(result)["ip_assignment"].position == "acceptable_fallback"
    result = _analyze(base + "The Employee hereby assigns to the Company all inventions developed on the Employee's own time using the Employee's own resources.")
    assert _by_type(result)["ip_assignment"].position == "walk_away"


def test_arbitration_ladder_reads_waivers_and_sides():
    base = "1. Dispute Resolution\n"
    # Class waiver with no statutory carve-out: counsel reviews.
    result = _analyze(base + "All disputes shall be resolved by binding arbitration. The Employee waives any right to bring a class action or collective action.")
    assert _by_type(result)["arbitration"].position == "review_required"
    # No machinery at all — voluntary arbitration only: preferred.
    result = _analyze(base + "Arbitration is voluntary only: neither party may compel arbitration, and disputes are heard in court unless both parties later agree to arbitrate a specific dispute.")
    assert _by_type(result)["arbitration"].position == "preferred"


def test_employment_terms_ladder_reads_at_will_and_severance():
    base = "1. Employment Status\n"
    result = _analyze(base + "The Employee's employment is at will: this is at-will employment, and the employment may be terminated by either party at any time, with or without cause.")
    assert _by_type(result)["employment_terms"].position == "preferred"
    # Eighteen months of severance crosses the review trigger.
    result = _analyze(base + "The Employee's employment is at will. On termination without cause the Employee will receive severance of eighteen (18) months of base salary.")
    assert _by_type(result)["employment_terms"].position == "review_required"


# ---------------------------------------------------------------------------
# Absence on the employment matrix is a finding, like any other family
# ---------------------------------------------------------------------------


def test_employment_missing_family_clauses_are_named():
    text = """1. Confidentiality
Each party shall hold the other's Confidential Information in confidence for three (3) years from the date of disclosure.
"""
    svc = _service()
    result = svc.analyze(
        contract(
            text,
            contract_id="CTR-EMP-THIN",
            doc_type="employment",
            title="Offer Letter",
            documents=[
                DocumentInput(
                    doc_type="agreement", document_id="AGR-1", raw_text=text
                )
            ],
        )
    )
    missing = set(result.facts.missing_standard_clauses)
    assert {"employment_terms", "non_compete", "ip_assignment",
            "arbitration"} <= missing
    assert "confidentiality" not in missing
