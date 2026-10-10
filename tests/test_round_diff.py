"""Negotiation-round diffing: clause alignment + verdict movement."""

from __future__ import annotations

from contract_agent.round_diff import align_clauses, build_round_diff
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import (
    AgentResult,
    ClauseUnit,
    ClauseVerdict,
    ContractFacts,
)
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore


def _result(contract_id: str, clauses: list[tuple[str, str, str, str]]) -> AgentResult:
    """Minimal result: (clause_id, heading, clause_type, position) rows."""
    return AgentResult(
        contract_id=contract_id,
        clauses=[
            ClauseUnit(clause_id=cid, ref=str(i + 1), heading=heading, text="x")
            for i, (cid, heading, _, _) in enumerate(clauses)
        ],
        clause_verdicts=[
            ClauseVerdict(clause_id=cid, clause_type=ctype, position=position)
            for cid, _, ctype, position in clauses
        ],
        facts=ContractFacts(),
    )


def test_movement_classes_and_counts():
    prior = _result("CTR-1", [
        ("C-01", "Liability", "liability_cap", "walk_away"),
        ("C-02", "Payment", "payment_terms", "preferred"),
        ("C-03", "Assignment", "assignment", "review_required"),
    ])
    current = _result("CTR-2", [
        ("C-01", "Liability", "liability_cap", "preferred"),
        ("C-02", "Payment", "payment_terms", "review_required"),
        ("C-03", "Data Protection", "data_protection", "preferred"),
    ])
    diff = build_round_diff(prior, current)
    by_type = {m.clause_type: m for m in diff.movements}
    assert by_type["liability_cap"].movement == "improved"
    assert by_type["payment_terms"].movement == "worsened"
    assert by_type["data_protection"].movement == "new"
    assert by_type["data_protection"].prior_position == ""
    assert by_type["assignment"].movement == "removed"
    assert by_type["assignment"].position == ""
    assert (diff.improved, diff.worsened, diff.new_clauses, diff.removed_clauses) == (1, 1, 1, 1)
    assert diff.previous_contract_id == "CTR-1"


def test_heading_match_beats_document_order():
    """Renumbered sections: two clauses of the same type swap places;
    the heading match must pair them, not the order."""
    prior = _result("CTR-1", [
        ("C-01", "Warranty", "limitation_of_warranty", "preferred"),
        ("C-02", "Warranty Disclaimer", "limitation_of_warranty", "review_required"),
    ])
    current = _result("CTR-2", [
        ("C-01", "Warranty Disclaimer", "limitation_of_warranty", "preferred"),
        ("C-02", "Warranty", "limitation_of_warranty", "preferred"),
    ])
    pairs = dict((c, p) for p, c in align_clauses(prior, current))
    assert pairs["C-01"] == "C-02"  # disclaimer ↔ disclaimer
    assert pairs["C-02"] == "C-01"
    diff = build_round_diff(prior, current)
    disclaimer = next(m for m in diff.movements if m.heading == "Warranty Disclaimer")
    assert disclaimer.movement == "improved"


def test_service_attaches_diff_for_superseding_contract():
    samples = {c.contract_id: c for c in sample_contract_models()}
    service = ContractService(store=InMemoryStore(), checkpointer=False)
    first = service.analyze(samples["CTR-2014"])
    assert first.round_diff is None  # round 1 supersedes nothing
    second = service.analyze(samples["CTR-2015"])
    diff = second.round_diff
    assert diff is not None
    assert diff.previous_contract_id == "CTR-2014"
    assert (diff.improved, diff.worsened, diff.new_clauses, diff.removed_clauses) == (5, 1, 1, 1)
    by_type = {m.clause_type: m for m in diff.movements}
    assert by_type["liability_cap"].movement == "improved"
    assert by_type["term_termination"].movement == "worsened"
    assert by_type["data_protection"].movement == "new"
    assert by_type["assignment"].movement == "removed"
    assert any(step.name == "round_diff" for step in second.trace)


def test_no_diff_when_prior_round_not_in_store():
    samples = {c.contract_id: c for c in sample_contract_models()}
    service = ContractService(store=InMemoryStore(), checkpointer=False)
    result = service.analyze(samples["CTR-2015"])  # round 1 never analysed
    assert result.round_diff is None
