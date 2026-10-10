"""Walk-away line acceptance tracking (round 9): when counsel
decides a packet that proposed walk-away lines, the decision record
captures the lines; the follow-up round's draft then resolves them —
held (final text at or above the line), softened, dropped, or the
clause removed — and the concession profile counts them as decision
evidence."""

from __future__ import annotations

import pytest

from contract_agent.samples import sample_contract_models
from contract_agent.schemas import AgentResult, ClauseDiff, ClauseVerdict
from contract_agent.service import ContractService
from contract_agent.store import ApprovalRecord, InMemoryStore
from contract_agent.walkaway import (
    capture_walk_away_outcomes,
    resolve_walk_away_outcomes,
    chain_for,
)
from helpers import contract


@pytest.fixture(scope="module")
def samples() -> dict:
    return {c.contract_id: c for c in sample_contract_models()}


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


# ---------------------------------------------------------------------------
# The real chain: capture at the decision, resolution on the next round
# ---------------------------------------------------------------------------


def test_capture_at_decision_then_resolution_on_the_follow_up(samples):
    svc = _service()
    svc.analyze(samples["CTR-2014"])
    svc.approve("CTR-2014", approver="C. Ounsel", reason="send the packet")
    decided = svc.get("CTR-2014")
    # Two clauses sat at walk_away in round 1, so the packet
    # proposed the line for both; nothing can resolve them yet.
    assert [(o.clause_type, o.status) for o in decided.walk_away_outcomes] == [
        ("liability_cap", "pending"),
        ("ip_ownership", "pending"),
    ]
    line = decided.walk_away_outcomes[0]
    assert line.decision == "approved"
    assert line.decided_by == "C. Ounsel"
    assert line.position_at_decision == "walk_away"
    assert line.line_language  # the line's own text is on the record
    # The follow-up round arrives: both clauses land at preferred —
    # the final text is at or above the line, and the stored
    # outcomes resolve without counsel touching the old record.
    svc.analyze(samples["CTR-2015"])
    decided = svc.get("CTR-2014")
    assert [(o.clause_type, o.status, o.final_position) for o in decided.walk_away_outcomes] == [
        ("liability_cap", "held", "preferred"),
        ("ip_ownership", "held", "preferred"),
    ]
    assert all(o.resolved_in == "CTR-2015" for o in decided.walk_away_outcomes)


def test_capture_resolves_immediately_when_the_follow_up_is_stored(samples):
    svc = _service()
    svc.analyze(samples["CTR-2014"])
    svc.analyze(samples["CTR-2015"])
    svc.analyze(samples["CTR-2016"])
    svc.approve("CTR-2015", approver="C. Ounsel")
    decided = svc.get("CTR-2015")
    # Round 2 proposed the walk-away line on term_termination (the
    # first fallback had not closed it); round 3's text sits at
    # preferred — held, resolved at capture time.
    assert [(o.clause_type, o.status) for o in decided.walk_away_outcomes] == [
        ("term_termination", "held")
    ]


def test_rejection_captures_too_and_a_clean_packet_captures_nothing(samples):
    svc = _service()
    svc.analyze(samples["CTR-2014"])
    svc.reject("CTR-2014", reviewer="C. Ounsel", reason="not on this paper")
    decided = svc.get("CTR-2014")
    assert decided.walk_away_outcomes
    assert all(o.decision == "rejected" for o in decided.walk_away_outcomes)

    clean = _service()
    result = clean.analyze(contract())
    assert not [d for d in result.clause_diffs if d.proposed_tier == 2]
    clean.approve(result.contract_id, approver="C. Ounsel")
    assert clean.get(result.contract_id).walk_away_outcomes == []


def test_profile_counts_the_decision_evidence(samples):
    svc = _service()
    svc.analyze(samples["CTR-2014"])
    svc.approve("CTR-2014", approver="C. Ounsel")
    svc.analyze(samples["CTR-2015"])
    svc.analyze(samples["CTR-2016"])
    svc.approve("CTR-2015", approver="C. Ounsel")
    profile = svc.counterparty_profile("Vertex Analytics Ltd")
    lines = profile["walk_away_lines"]
    assert lines["proposed"] == 3
    assert lines["held"] == 3
    assert lines["pending"] == 0
    assert lines["by_clause_type"]["liability_cap"] == {"held": 1}
    from contract_agent.concessions import concession_evidence_lines

    evidence = concession_evidence_lines(profile)
    assert any(line.startswith("walk-away lines: 3 proposed") for line in evidence)


# ---------------------------------------------------------------------------
# Fabricated chains: the remaining resolutions
# ---------------------------------------------------------------------------


def _record(cid, supersedes, *, diffs=None, verdicts=None):
    result = AgentResult(
        contract_id=cid,
        counterparty_name="Fabric Co",
        clause_diffs=diffs or [],
        clause_verdicts=verdicts or [],
    )
    return ApprovalRecord(
        result=result,
        contract={"supersedes": supersedes, "counterparty_name": "Fabric Co"},
    )


def _tier2_diff(position="walk_away"):
    return ClauseDiff(
        clause_id="C-01",
        clause_type="liability_cap",
        position=position,
        proposed_tier=2,
        tier_language="Final position: liability capped at twelve months' fees.",
    )


@pytest.mark.parametrize(
    "follow_position,expected",
    [
        ("acceptable_fallback", "held"),
        ("review_required", "softened"),
        ("walk_away", "dropped"),
        (None, "clause_removed"),
    ],
)
def test_resolution_against_the_follow_up(follow_position, expected):
    svc = _service()
    store = svc._get_store()
    store.save(_record("CTR-WA-1", "", diffs=[_tier2_diff()]))
    verdicts = (
        []
        if follow_position is None
        else [
            ClauseVerdict(
                clause_id="C-01", clause_type="liability_cap", position=follow_position
            )
        ]
    )
    store.save(_record("CTR-WA-2", "CTR-WA-1", verdicts=verdicts))
    svc.approve("CTR-WA-1", approver="C. Ounsel")
    (outcome,) = svc.get("CTR-WA-1").walk_away_outcomes
    assert outcome.status == expected
    assert outcome.resolved_in == "CTR-WA-2"
    if follow_position is not None:
        assert outcome.final_position == follow_position


def test_pure_capture_and_resolution_units():
    result = AgentResult(
        contract_id="CTR-WA-9",
        decision="approved",
        decided_by="C. Ounsel",
        decided_at="2026-10-10T12:00:00+00:00",
        clause_diffs=[
            _tier2_diff(),
            ClauseDiff(clause_id="C-02", clause_type="payment_terms", proposed_tier=1),
        ],
    )
    outcomes = capture_walk_away_outcomes(result)
    assert len(outcomes) == 1  # tier-1 proposals are not walk-away lines
    assert outcomes[0].captured_at == "2026-10-10T12:00:00+00:00"
    # No follow-up in the chain: still pending, unchanged.
    chain = [ApprovalRecord(result=result)]
    (still,) = resolve_walk_away_outcomes(outcomes, chain, "CTR-WA-9")
    assert still.status == "pending"
    assert chain_for([], "CTR-WA-9") == []
