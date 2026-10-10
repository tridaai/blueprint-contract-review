"""Tiered fallbacks: the packet proposes the right concession tier
for the round and the counterparty's history (round 4)."""

from __future__ import annotations

import pytest

from contract_agent.fallback_tiers import propose_tier, walk_away_line
from contract_agent.playbook_data import PLAYBOOK
from contract_agent.retriever import KeywordRetriever
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


def _diff(result, clause_type: str):
    return next(d for d in result.clause_diffs if d.clause_type == clause_type)


# -- the proposal rule ---------------------------------------------------


def test_preferred_clauses_get_no_proposal():
    assert propose_tier("payment_terms", "preferred", "anything") == (0, "", "", "")


def test_round_one_opens_with_the_first_fallback():
    tier, name, language, reason = propose_tier(
        "payment_terms", "acceptable_fallback", "FALLBACK TEXT", round_number=1
    )
    assert tier == 1
    assert name == "first fallback"
    assert language == "FALLBACK TEXT"
    assert "round one" in reason


def test_later_rounds_propose_the_walk_away_line():
    tier, name, language, reason = propose_tier(
        "payment_terms", "acceptable_fallback", "FALLBACK TEXT", round_number=2
    )
    assert tier == 2
    assert name == "walk-away line"
    assert language.startswith("Final position on payment")
    assert "round 2" in reason


def test_re_traded_clauses_propose_the_final_line_in_any_round():
    tier, _, language, reason = propose_tier(
        "indemnity", "review_required", "FALLBACK TEXT",
        round_number=1, re_traded=True,
    )
    assert tier == 2
    assert language.startswith("Final position on indemnity")
    assert "re-traded" in reason


def test_walk_away_verdicts_propose_the_final_line_directly():
    tier, _, language, reason = propose_tier(
        "liability_cap", "walk_away", "FALLBACK TEXT", round_number=1
    )
    assert tier == 2
    assert language.startswith("Final position on liability")
    assert "walk-away position" in reason


def test_type_without_a_line_keeps_tier_one_and_says_why():
    tier, _, language, reason = propose_tier(
        "other", "review_required", "FALLBACK TEXT", round_number=3
    )
    assert tier == 1
    assert language == "FALLBACK TEXT"
    assert "no walk-away line" in reason


# -- the data -------------------------------------------------------------


def test_every_fallback_bearing_type_carries_a_walk_away_line():
    types_with_fallback = {
        e["clause_type"] for e in PLAYBOOK if e.get("fallback_language")
    }
    types_with_lines = {e["clause_type"] for e in PLAYBOOK if e.get("walk_away_line")}
    assert types_with_fallback <= types_with_lines
    for clause_type in types_with_lines:
        entry_id, line = walk_away_line(clause_type)
        assert entry_id.startswith("PB-")
        assert line.startswith("Final position")


def test_retrieval_carries_both_tiers():
    results = KeywordRetriever().retrieve("liability", top_k=10)
    lia03 = next(r for r in results if r.playbook_id == "PB-LIA-03")
    assert lia03.fallback_language.startswith("Each party's aggregate liability")
    assert lia03.walk_away_line.startswith("Final position on liability")


# -- end to end ------------------------------------------------------------


def test_round_one_sample_proposes_first_fallbacks(samples):
    result = _service().analyze(samples["CTR-2014"])
    payment = _diff(result, "payment_terms")
    assert payment.position == "acceptable_fallback"
    assert payment.proposed_tier == 1
    assert payment.tier_language == payment.playbook_fallback
    # The uncapped liability is a walk-away verdict: final line directly.
    liability = _diff(result, "liability_cap")
    assert liability.position == "walk_away"
    assert liability.proposed_tier == 2
    assert liability.tier_language.startswith("Final position on liability")
    assert "Proposed tier: tier 1" in result.drafts.review_packet
    assert "Proposed tier: tier 2" in result.drafts.review_packet


def test_second_round_sample_proposes_final_lines(samples):
    service = _service()
    service.analyze(samples["CTR-2014"])
    result = service.analyze(samples["CTR-2015"])
    # Round 2's only flagged clause (the 120-day renewal notice) is an
    # acceptable fallback — in round 2 the packet proposes the final
    # line for it, not the first fallback again.
    term = _diff(result, "term_termination")
    assert term.position == "acceptable_fallback"
    assert term.proposed_tier == 2
    assert term.tier_language.startswith("Final position on term")
    assert "round 2" in term.tier_reason


def test_chain_refinement_uses_re_trades(samples):
    service = _service()
    for cid in ("CTR-2014", "CTR-2015"):
        service.analyze(samples[cid])
    result = service.analyze(samples["CTR-2016"])
    assert result.round_chain is not None
    # Payment was re-traded across the chain; its round-3 diff
    # proposes the walk-away line and says why.
    payment = _diff(result, "payment_terms")
    assert payment.proposed_tier == 2
    assert "re-traded" in payment.tier_reason
    indemnity = _diff(result, "indemnity")
    assert indemnity.proposed_tier == 2
    assert "re-traded" in indemnity.tier_reason
