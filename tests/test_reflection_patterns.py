"""Reflection patterns (round 7): an agreed edit flagged
agreed-but-not-reflected in TWO consecutive follow-ups is a pattern,
not an oversight — the clause's proposed tier escalates to the
walk-away line and the pattern is named on the result."""

from __future__ import annotations

from contract_agent.clause_diffs import build_clause_diffs
from contract_agent.fallback_tiers import propose_tier
from contract_agent.reflection_patterns import find_reflection_patterns
from contract_agent.schemas import (
    AgentResult,
    ClauseUnit,
    ClauseVerdict,
    EditOutcomeInput,
    EditReflection,
)
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore
from helpers import contract

# The same net-90 payment clause in every round: counsel agrees the
# payment edit each round, and the counterparty's next draft never
# shows the agreed language.
_NET90 = """1. Limitation of Liability
Each party's aggregate liability shall not exceed the fees paid in the
twelve (12) months preceding the claim.

2. Payment Terms
The Customer shall pay all invoices within ninety (90) days of the invoice date.
"""


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


def _edit_index(result, clause_type: str) -> int:
    return next(
        i for i, e in enumerate(result.proposed_edits) if e.clause_type == clause_type
    )


def _agree_payment_edit(svc: ContractService, result) -> None:
    svc.record_edit_outcomes(
        result.contract_id,
        [
            EditOutcomeInput(
                edit_index=_edit_index(result, "payment_terms"),
                outcome="accepted",
                note="agreed on the call",
            )
        ],
        actor="C. Ounsel",
    )


def _three_rounds(svc: ContractService):
    r1 = svc.analyze(contract(_NET90, contract_id="CTR-PAT-1"))
    _agree_payment_edit(svc, r1)
    r2 = svc.analyze(
        contract(_NET90, contract_id="CTR-PAT-2", supersedes="CTR-PAT-1")
    )
    _agree_payment_edit(svc, r2)
    r3 = svc.analyze(
        contract(_NET90, contract_id="CTR-PAT-3", supersedes="CTR-PAT-2")
    )
    return r1, r2, r3


# ---------------------------------------------------------------------------
# The pattern, end to end
# ---------------------------------------------------------------------------


def test_two_consecutive_misses_form_a_pattern_and_escalate_the_tier():
    svc = _service()
    _, r2, r3 = _three_rounds(svc)
    # Each follow-up flagged the agreed payment edit on its own.
    assert any(
        e.clause_type == "payment_terms" and e.status == "agreed_not_reflected"
        for e in r2.edit_reflection
    )
    assert any(
        e.clause_type == "payment_terms" and e.status == "agreed_not_reflected"
        for e in r3.edit_reflection
    )
    # The third round names the pattern, with its round history.
    patterns = {p.clause_type: p for p in r3.reflection_patterns}
    assert "payment_terms" in patterns
    pattern = patterns["payment_terms"]
    assert pattern.flagged_in == ["CTR-PAT-2", "CTR-PAT-3"]
    assert pattern.agreed_in == ["CTR-PAT-1", "CTR-PAT-2"]
    assert pattern.flags == 2
    # And the packet answers it: the payment clause proposes the
    # walk-away line, marked as pattern-escalated, reason naming it.
    diff = next(d for d in r3.clause_diffs if d.clause_type == "payment_terms")
    assert diff.proposed_tier == 2
    assert diff.pattern_escalated is True
    assert "reflection pattern" in diff.tier_reason
    assert "net sixty (60)" in diff.tier_language


def test_one_miss_is_not_a_pattern():
    svc = _service()
    r1 = svc.analyze(contract(_NET90, contract_id="CTR-PAT-1"))
    _agree_payment_edit(svc, r1)
    r2 = svc.analyze(
        contract(_NET90, contract_id="CTR-PAT-2", supersedes="CTR-PAT-1")
    )
    assert any(e.status == "agreed_not_reflected" for e in r2.edit_reflection)
    assert r2.reflection_patterns == []


def test_pattern_lands_in_the_trace():
    svc = _service()
    _, _, r3 = _three_rounds(svc)
    steps = [s for s in r3.trace if s.name == "reflection_pattern"]
    assert len(steps) == 1
    assert "payment_terms" in steps[0].details[0]


def test_late_marked_outcomes_raise_the_pattern_on_refresh():
    # Outcomes marked only after the follow-up rounds were analysed:
    # the refresh path must still surface the pattern on round 3.
    svc = _service()
    r1 = svc.analyze(contract(_NET90, contract_id="CTR-PAT-1"))
    r2 = svc.analyze(
        contract(_NET90, contract_id="CTR-PAT-2", supersedes="CTR-PAT-1")
    )
    r3 = svc.analyze(
        contract(_NET90, contract_id="CTR-PAT-3", supersedes="CTR-PAT-2")
    )
    assert svc.get("CTR-PAT-3").reflection_patterns == []
    _agree_payment_edit(svc, r1)  # refreshes round 2's reflection
    _agree_payment_edit(svc, svc.get("CTR-PAT-2"))  # refreshes round 3
    refreshed = svc.get("CTR-PAT-3")
    assert [p.clause_type for p in refreshed.reflection_patterns] == ["payment_terms"]
    diff = next(
        d for d in refreshed.clause_diffs if d.clause_type == "payment_terms"
    )
    assert diff.pattern_escalated is True


# ---------------------------------------------------------------------------
# The finder, unit-level
# ---------------------------------------------------------------------------


def _result(contract_id: str, entries: list[EditReflection]) -> AgentResult:
    return AgentResult(contract_id=contract_id, edit_reflection=entries)


def _flag(clause_type: str, status: str) -> EditReflection:
    return EditReflection(clause_type=clause_type, status=status, detail="x")


def test_finder_needs_two_consecutive_measurable_flags():
    chain = [
        _result("R1", []),
        _result("R2", [_flag("payment_terms", "agreed_not_reflected")]),
        _result("R3", [_flag("payment_terms", "agreed_not_reflected")]),
    ]
    patterns = find_reflection_patterns(chain)
    assert [p.clause_type for p in patterns] == ["payment_terms"]
    assert patterns[0].flagged_in == ["R2", "R3"]
    assert patterns[0].agreed_in == ["R1", "R2"]


def test_finder_resolves_when_the_language_lands():
    chain = [
        _result("R1", []),
        _result("R2", [_flag("payment_terms", "agreed_not_reflected")]),
        _result("R3", [_flag("payment_terms", "agreed_not_reflected")]),
        _result("R4", [_flag("payment_terms", "reflected")]),
    ]
    assert find_reflection_patterns(chain) == []


def test_finder_ignores_unmeasurable_checks_and_short_chains():
    chain = [
        _result("R1", []),
        _result("R2", [_flag("payment_terms", "agreed_not_reflected")]),
        _result("R3", [_flag("payment_terms", "not_checkable")]),
    ]
    assert find_reflection_patterns(chain) == []
    assert find_reflection_patterns(chain[:2]) == []


def test_finder_requires_consecutive_rounds():
    chain = [
        _result("R1", []),
        _result("R2", [_flag("payment_terms", "agreed_not_reflected")]),
        _result("R3", []),  # no outcomes agreed this round: no check
        _result("R4", [_flag("payment_terms", "agreed_not_reflected")]),
    ]
    assert find_reflection_patterns(chain) == []


# ---------------------------------------------------------------------------
# The tier rule: the pattern is an escalation driver of its own
# ---------------------------------------------------------------------------


def test_pattern_escalates_even_in_round_one_terms():
    tier, name, language, reason = propose_tier(
        "payment_terms",
        "review_required",
        "fallback text",
        round_number=1,
        reflection_escalated=True,
    )
    assert tier == 2
    assert name == "walk-away line"
    assert "net sixty (60)" in language
    assert "reflection pattern" in reason


def test_pattern_without_a_walkaway_line_keeps_tier_one_and_says_why():
    tier, _, _, reason = propose_tier(
        "other", "review_required", "", round_number=1, reflection_escalated=True
    )
    assert tier == 1
    assert "reflection pattern" in reason
    assert "no walk-away line" in reason


def test_diffs_mark_pattern_escalated_clauses():
    units = [
        ClauseUnit(
            clause_id="C-01",
            ref="2",
            heading="Payment Terms",
            text="The Customer shall pay all invoices within ninety (90) days.",
        )
    ]
    verdicts = [
        ClauseVerdict(
            clause_id="C-01",
            clause_type="payment_terms",
            position="review_required",
            fallback_language="fallback text",
        )
    ]
    diffs = build_clause_diffs(
        units, verdicts, round_number=1, reflection_escalated_types={"payment_terms"}
    )
    assert diffs[0].pattern_escalated is True
    assert diffs[0].proposed_tier == 2
