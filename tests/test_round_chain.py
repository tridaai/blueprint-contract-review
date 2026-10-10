"""Round chains: whole-negotiation movement history + re-trades."""

from __future__ import annotations

from contract_agent.round_diff import build_round_chain
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


def test_re_trade_detected_when_ground_is_given_back():
    rounds = [
        _result("R1", [("C-01", "Indemnity", "indemnity", "review_required")]),
        _result("R2", [("C-01", "Indemnity", "indemnity", "preferred")]),
        _result("R3", [("C-01", "Indemnity", "indemnity", "acceptable_fallback")]),
    ]
    chain = build_round_chain(rounds)
    history = chain.histories[0]
    assert history.re_traded is True
    assert "round 2" in history.re_trade_detail  # improved there
    assert "round 3" in history.re_trade_detail  # worsened there
    assert history.movements == ["improved", "worsened"]
    # Net over the chain it still ends better than it started.
    assert history.cumulative_movement == "improved"
    assert chain.re_trades == 1
    assert chain.re_traded_clause_types == ["indemnity"]
    assert chain.contract_ids == ["R1", "R2", "R3"]


def test_worsen_then_recover_is_not_a_re_trade():
    """Ground lost and then won back is a recovery — the re-trade
    flag is specifically for ground won and then surrendered."""
    rounds = [
        _result("R1", [("C-01", "Term", "term_termination", "preferred")]),
        _result("R2", [("C-01", "Term", "term_termination", "review_required")]),
        _result("R3", [("C-01", "Term", "term_termination", "preferred")]),
    ]
    chain = build_round_chain(rounds)
    history = chain.histories[0]
    assert history.movements == ["worsened", "improved"]
    assert history.re_traded is False
    assert history.cumulative_movement == "unchanged"
    assert chain.re_trades == 0


def test_new_and_removed_tracks_across_the_chain():
    rounds = [
        _result("R1", [("C-01", "Assignment", "assignment", "review_required")]),
        _result("R2", [
            ("C-01", "Assignment", "assignment", "review_required"),
            ("C-02", "Data Protection", "data_protection", "preferred"),
        ]),
        _result("R3", [("C-01", "Data Protection", "data_protection", "preferred")]),
    ]
    chain = build_round_chain(rounds)
    by_type = {h.clause_type: h for h in chain.histories}
    assignment = by_type["assignment"]
    assert [p.position for p in assignment.track] == ["review_required", "review_required", ""]
    assert assignment.cumulative_movement == "removed"
    dp = by_type["data_protection"]
    assert [p.position for p in dp.track] == ["", "preferred", "preferred"]
    assert dp.movements == ["new", "unchanged"]
    assert dp.cumulative_movement == "new"
    assert chain.rounds_to_no_walk_away == 1  # no walk-away anywhere


def test_rounds_to_no_walk_away_counts_from_the_first_clean_round():
    rounds = [
        _result("R1", [("C-01", "Liability", "liability_cap", "walk_away")]),
        _result("R2", [("C-01", "Liability", "liability_cap", "walk_away")]),
        _result("R3", [("C-01", "Liability", "liability_cap", "preferred")]),
    ]
    chain = build_round_chain(rounds)
    assert chain.rounds_to_no_walk_away == 3
    assert chain.cumulative_improved == 1


def test_vertex_chain_end_to_end_through_the_service():
    samples = {c.contract_id: c for c in sample_contract_models()}
    service = ContractService(store=InMemoryStore(), checkpointer=False)
    first = service.analyze(samples["CTR-2014"])
    assert first.round_chain is None
    second = service.analyze(samples["CTR-2015"])
    assert second.round_chain is None  # two rounds: the pairwise diff says it all
    third = service.analyze(samples["CTR-2016"])
    chain = third.round_chain
    assert chain is not None
    assert chain.contract_ids == ["CTR-2014", "CTR-2015", "CTR-2016"]
    assert chain.rounds == 3
    assert chain.re_trades == 2
    assert chain.re_traded_clause_types == ["indemnity", "payment_terms"]
    assert (chain.cumulative_improved, chain.cumulative_worsened,
            chain.cumulative_unchanged) == (3, 0, 5)
    assert chain.rounds_to_no_walk_away == 2
    by_type = {h.clause_type: h for h in chain.histories}
    assert [p.position for p in by_type["indemnity"].track] == [
        "review_required", "preferred", "review_required",
    ]
    assert by_type["term_termination"].re_traded is False  # worsened, then recovered
    assert by_type["data_protection"].cumulative_movement == "new"
    assert by_type["assignment"].cumulative_movement == "removed"
    assert any(step.name == "round_chain" for step in third.trace)
    # The last pairwise step (round 2 -> 3) is its own story: term
    # recovered, three clauses slipped.
    diff = third.round_diff
    assert diff is not None and diff.previous_contract_id == "CTR-2015"
    assert (diff.improved, diff.worsened) == (1, 3)


def test_chain_walk_stops_at_a_missing_link():
    """Round 3 analysed without round 2 in the store: the chain the
    store actually holds has two rounds, so no chain report — the
    pairwise diff is absent too (its predecessor is round 2)."""
    samples = {c.contract_id: c for c in sample_contract_models()}
    service = ContractService(store=InMemoryStore(), checkpointer=False)
    service.analyze(samples["CTR-2014"])
    third = service.analyze(samples["CTR-2016"])  # supersedes CTR-2015, never analysed
    assert third.round_diff is None
    assert third.round_chain is None
