"""Pattern-aware option scoring (round 8): a clause type under an
active reflection pattern in the chain — an agreed edit flagged
agreed-but-not-reflected in two consecutive follow-ups — raises
``escalate_to_senior_counsel`` by a capped pattern term, visible in
every breakdown, so the options tell the same story the tiers
already tell (the pattern escalates the clause's proposed tier to
the walk-away line)."""

from __future__ import annotations

from contract_agent.options import (
    HISTORY_TERM_CAP,
    PATTERN_TERM_CAP,
    pattern_adjustment,
    score_options,
    template_proposals,
)
from contract_agent.schemas import (
    ClauseVerdict,
    ContractFacts,
    EditOutcomeInput,
)
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


def _scored(verdicts, tendencies=None, pattern_types=None):
    return score_options(
        template_proposals(verdicts, _FACTS), verdicts, _FACTS,
        tendencies, pattern_types,
    )


def _option(options, clause_id: str, kind: str):
    return next(o for o in options if o.clause_id == clause_id and o.kind == kind)


# ---------------------------------------------------------------------------
# The term itself
# ---------------------------------------------------------------------------


def test_pattern_adjustment_moves_only_escalation_on_pattern_types():
    patterns = {"payment_terms"}
    assert pattern_adjustment("escalate_to_senior_counsel", "payment_terms", patterns) == PATTERN_TERM_CAP
    assert pattern_adjustment("propose_fallback_language", "payment_terms", patterns) == 0.0
    assert pattern_adjustment("accept_as_preferred", "payment_terms", patterns) == 0.0
    assert pattern_adjustment("request_counterparty_info", "payment_terms", patterns) == 0.0
    assert pattern_adjustment("escalate_to_senior_counsel", "liability_cap", patterns) == 0.0
    assert pattern_adjustment("escalate_to_senior_counsel", "payment_terms", None) == 0.0
    assert pattern_adjustment("escalate_to_senior_counsel", "payment_terms", set()) == 0.0


def test_breakdown_shows_the_pattern_term_and_sums_to_the_score():
    verdicts = [_verdict("C-01", "payment_terms", "review_required")]
    options = _scored(verdicts, pattern_types={"payment_terms"})
    escalate = _option(options, "C-01", "escalate_to_senior_counsel")
    assert escalate.breakdown["pattern"] == PATTERN_TERM_CAP
    assert escalate.score == 82.0 + PATTERN_TERM_CAP
    assert round(sum(escalate.breakdown.values()), 2) == escalate.score
    propose = _option(options, "C-01", "propose_fallback_language")
    assert propose.breakdown["pattern"] == 0.0
    assert propose.score == 76.0


def test_pattern_and_history_terms_are_separately_capped():
    # A held type under a pattern: both cousin signals fire on the
    # escalation, each at its own cap — neither swells past it, and
    # both show in the breakdown.
    verdicts = [_verdict("C-01", "payment_terms", "review_required")]
    options = _scored(
        verdicts,
        tendencies={"payment_terms": {"tendency": "holds", "re_trades": 0}},
        pattern_types={"payment_terms"},
    )
    escalate = _option(options, "C-01", "escalate_to_senior_counsel")
    assert escalate.breakdown["history"] == HISTORY_TERM_CAP
    assert escalate.breakdown["pattern"] == PATTERN_TERM_CAP
    assert escalate.score == 82.0 + HISTORY_TERM_CAP + PATTERN_TERM_CAP
    assert round(sum(escalate.breakdown.values()), 2) == escalate.score


# ---------------------------------------------------------------------------
# The cap, pinned both ways: orders a close call, never dominates
# ---------------------------------------------------------------------------


def test_pattern_term_orders_a_close_call():
    # Two review_required clauses score identically on the playbook
    # terms; the pattern type's escalation wins once patterns are read.
    verdicts = [
        _verdict("C-01", "liability_cap", "review_required"),
        _verdict("C-02", "payment_terms", "review_required"),
    ]
    plain = _scored(verdicts)
    assert next(o for o in plain if o.recommended).clause_id == "C-01"
    aware = _scored(verdicts, pattern_types={"payment_terms"})
    recommended = next(o for o in aware if o.recommended)
    assert recommended.clause_id == "C-02"
    assert recommended.kind == "escalate_to_senior_counsel"
    assert recommended.score == 82.0 + PATTERN_TERM_CAP


def test_pattern_term_cannot_dominate_the_playbook_score():
    # The pattern term never touches propose_fallback_language, so
    # a walk-away clause's escalation (88) still beats proposing
    # language on the pattern type (76): the pattern cannot talk
    # the packet out of escalating its worst clause.
    verdicts = [
        _verdict("C-01", "liability_cap", "walk_away"),
        _verdict("C-02", "payment_terms", "review_required"),
    ]
    aware = _scored(verdicts, pattern_types={"payment_terms"})
    recommended = next(o for o in aware if o.recommended)
    assert recommended.clause_id == "C-01"
    assert recommended.kind == "escalate_to_senior_counsel"
    propose = _option(aware, "C-02", "propose_fallback_language")
    assert propose.breakdown["pattern"] == 0.0
    assert propose.score == 76.0


# ---------------------------------------------------------------------------
# End to end: a real chain's pattern reaches the NEXT round's scores
# ---------------------------------------------------------------------------

_NET90 = """1. Limitation of Liability
Each party's aggregate liability shall not exceed the fees paid in the
twelve (12) months preceding the claim.

2. Payment Terms
The Customer shall pay all invoices within ninety (90) days of the invoice date.
"""


def _agree_payment_edit(svc: ContractService, result) -> None:
    index = next(
        i for i, e in enumerate(result.proposed_edits)
        if e.clause_type == "payment_terms"
    )
    svc.record_edit_outcomes(
        result.contract_id,
        [EditOutcomeInput(edit_index=index, outcome="accepted",
                          note="agreed on the call")],
        actor="C. Ounsel",
    )


def _payment_option(result, kind: str):
    clause_id = next(
        v.clause_id for v in result.clause_verdicts
        if v.clause_type == "payment_terms"
    )
    return _option(result.options, clause_id, kind)


def test_chain_pattern_raises_escalation_on_the_next_round():
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    r1 = svc.analyze(contract(_NET90, contract_id="CTR-PATO-1"))
    _agree_payment_edit(svc, r1)
    r2 = svc.analyze(contract(_NET90, contract_id="CTR-PATO-2", supersedes="CTR-PATO-1"))
    _agree_payment_edit(svc, r2)
    r3 = svc.analyze(contract(_NET90, contract_id="CTR-PATO-3", supersedes="CTR-PATO-2"))
    # The pattern FORMS on round 3's own result (its tiers move now)…
    assert any(p.clause_type == "payment_terms" for p in r3.reflection_patterns)
    # …but round 3's options were scored before its reflection
    # existed: no pattern term yet, stated rather than backdated.
    assert _payment_option(r3, "escalate_to_senior_counsel").breakdown["pattern"] == 0.0
    # Round 4 is scored with the pattern in force: escalation of the
    # payment clause carries the term; proposing does not.
    r4 = svc.analyze(contract(_NET90, contract_id="CTR-PATO-4", supersedes="CTR-PATO-3"))
    escalate = _payment_option(r4, "escalate_to_senior_counsel")
    assert escalate.breakdown["pattern"] == PATTERN_TERM_CAP
    assert round(sum(escalate.breakdown.values()), 2) == escalate.score
    assert _payment_option(r4, "propose_fallback_language").breakdown["pattern"] == 0.0


def test_pattern_is_chain_scoped_not_counterparty_wide():
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    r1 = svc.analyze(contract(_NET90, contract_id="CTR-PATO-1"))
    _agree_payment_edit(svc, r1)
    r2 = svc.analyze(contract(_NET90, contract_id="CTR-PATO-2", supersedes="CTR-PATO-1"))
    _agree_payment_edit(svc, r2)
    svc.analyze(contract(_NET90, contract_id="CTR-PATO-3", supersedes="CTR-PATO-2"))
    # A fresh negotiation with the same counterparty (no chain link)
    # inherits no pattern: patterns belong to the chain that made them.
    fresh = svc.analyze(contract(_NET90, contract_id="CTR-PATO-9"))
    assert all(o.breakdown.get("pattern", 0.0) == 0.0 for o in fresh.options)
