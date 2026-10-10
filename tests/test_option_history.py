"""Concession-aware option scoring (round 7): the counterparty's
own concession record moves option scores by a small, explicit,
capped history term — visible in every option's breakdown, and
pinned so it can order close calls but never dominate the
playbook-driven score."""

from __future__ import annotations

from contract_agent.options import (
    HISTORY_TERM_CAP,
    history_adjustment,
    score_option,
    score_options,
    template_proposals,
)
from contract_agent.schemas import ClauseVerdict, ContractFacts
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore
from helpers import contract

_FACTS = ContractFacts(posture="negotiable", package_complete=True)


def _verdict(clause_id: str, clause_type: str, position: str) -> ClauseVerdict:
    return ClauseVerdict(
        clause_id=clause_id,
        clause_type=clause_type,
        position=position,
        fallback_language="fallback language",
    )


def _scored(verdicts, tendencies=None):
    return score_options(
        template_proposals(verdicts, _FACTS), verdicts, _FACTS, tendencies
    )


def _option(options, clause_id: str, kind: str):
    return next(o for o in options if o.clause_id == clause_id and o.kind == kind)


# ---------------------------------------------------------------------------
# The term itself
# ---------------------------------------------------------------------------


def test_history_adjustment_moves_only_the_two_named_kinds():
    concedes = {"tendency": "concedes", "re_trades": 0}
    holds = {"tendency": "holds", "re_trades": 0}
    assert history_adjustment("propose_fallback_language", concedes) == HISTORY_TERM_CAP
    assert history_adjustment("escalate_to_senior_counsel", concedes) == 0.0
    assert history_adjustment("escalate_to_senior_counsel", holds) == HISTORY_TERM_CAP
    assert history_adjustment("propose_fallback_language", holds) == 0.0
    assert history_adjustment("accept_as_preferred", concedes) == 0.0
    assert history_adjustment("request_counterparty_info", holds) == 0.0
    assert history_adjustment("propose_fallback_language", None) == 0.0
    assert history_adjustment("propose_fallback_language", {"tendency": "mixed", "re_trades": 0}) == 0.0
    assert history_adjustment("propose_fallback_language", {"tendency": "uncontested", "re_trades": 0}) == 0.0


def test_retraded_types_raise_escalation_and_the_cap_holds():
    # Held AND re-traded: the two signals never stack past the cap.
    entry = {"tendency": "holds", "re_trades": 2}
    assert history_adjustment("escalate_to_senior_counsel", entry) == HISTORY_TERM_CAP
    # A re-trade alone (tendency mixed) is enough for the escalate term.
    entry = {"tendency": "mixed", "re_trades": 1}
    assert history_adjustment("escalate_to_senior_counsel", entry) == HISTORY_TERM_CAP


def test_score_option_without_history_is_the_playbook_score():
    # review_required + negotiable: escalate = 60*.9 + 40*.7 = 82.
    assert score_option("escalate_to_senior_counsel", "review_required", _FACTS) == 82.0
    assert score_option("propose_fallback_language", "review_required", _FACTS) == 76.0


def test_breakdown_sums_to_the_score_and_shows_the_term():
    verdicts = [_verdict("C-01", "payment_terms", "review_required")]
    options = _scored(verdicts, {"payment_terms": {"tendency": "concedes", "re_trades": 0}})
    propose = _option(options, "C-01", "propose_fallback_language")
    assert propose.breakdown["history"] == HISTORY_TERM_CAP
    assert propose.breakdown["position"] == 36.0
    assert propose.breakdown["posture"] == 40.0
    assert round(sum(propose.breakdown.values()), 2) == propose.score
    escalate = _option(options, "C-01", "escalate_to_senior_counsel")
    assert escalate.breakdown["history"] == 0.0


# ---------------------------------------------------------------------------
# The cap, pinned both ways: orders a close call, never dominates
# ---------------------------------------------------------------------------


def test_history_term_orders_a_close_call():
    # Two review_required clauses score identically on the playbook
    # terms; the held type's escalation wins once history is read.
    verdicts = [
        _verdict("C-01", "liability_cap", "review_required"),
        _verdict("C-02", "payment_terms", "review_required"),
    ]
    plain = _scored(verdicts)
    assert next(o for o in plain if o.recommended).clause_id == "C-01"
    aware = _scored(verdicts, {"payment_terms": {"tendency": "holds", "re_trades": 0}})
    recommended = next(o for o in aware if o.recommended)
    assert recommended.clause_id == "C-02"
    assert recommended.kind == "escalate_to_senior_counsel"
    assert recommended.score == 82.0 + HISTORY_TERM_CAP


def test_history_term_cannot_dominate_the_playbook_score():
    # A walk-away clause's escalation (88) vs a conceded type's
    # proposal (76 + the full term): the playbook gap (12) is wider
    # than the cap, so the recommendation does not move.
    verdicts = [
        _verdict("C-01", "liability_cap", "walk_away"),
        _verdict("C-02", "payment_terms", "review_required"),
    ]
    aware = _scored(verdicts, {"payment_terms": {"tendency": "concedes", "re_trades": 0}})
    recommended = next(o for o in aware if o.recommended)
    assert recommended.clause_id == "C-01"
    assert recommended.kind == "escalate_to_senior_counsel"
    boosted = _option(aware, "C-02", "propose_fallback_language")
    assert boosted.score == 76.0 + HISTORY_TERM_CAP
    assert boosted.score < recommended.score


# ---------------------------------------------------------------------------
# End to end: the profile from a real chain reaches a fresh review
# ---------------------------------------------------------------------------

_NET90 = """1. Limitation of Liability
Each party's aggregate liability shall not exceed the fees paid in the
twelve (12) months preceding the claim.

2. Payment Terms
The Customer shall pay all invoices within ninety (90) days of the invoice date.
"""

_NET45 = _NET90.replace("ninety (90)", "forty-five (45)")


def test_conceded_type_boosts_propose_fallback_on_the_next_contract():
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(contract(_NET90, contract_id="CTR-HIST-1"))
    svc.analyze(contract(_NET45, contract_id="CTR-HIST-2", supersedes="CTR-HIST-1"))
    # A fresh contract with the same counterparty: net 90 again.
    result = svc.analyze(contract(_NET90, contract_id="CTR-HIST-3"))
    payment_clause = next(
        v.clause_id for v in result.clause_verdicts if v.clause_type == "payment_terms"
    )
    propose = _option(result.options, payment_clause, "propose_fallback_language")
    # Northwind conceded payment terms across the chain (net 90 →
    # net 45), so proposing the fallback scores the history term.
    assert propose.breakdown["history"] == HISTORY_TERM_CAP
    assert round(sum(propose.breakdown.values()), 2) == propose.score


def test_no_chain_no_history_term():
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    result = svc.analyze(contract(_NET90, contract_id="CTR-HIST-9"))
    assert result.options
    assert all(o.breakdown.get("history", 0.0) == 0.0 for o in result.options)
