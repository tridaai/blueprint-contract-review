"""Who drops the line? (round 10): walk-away outcomes say what the
counterparty's next draft did with a line. They do not say what
counsel's own desk did with it first — and a line counsel rejected
at decision time was lost before the counterparty ever saw it.
The origin join reads the decision record's per-edit outcomes for
the edits that carried each line, classifies where the line stood
when it left counsel's desk, and the concession profile aggregates
held rates by origin, so "the line keeps being lost" resolves
into whose desk loses it."""

from __future__ import annotations

import pytest

from contract_agent.concessions import (
    build_concession_profile,
    concession_evidence_lines,
)
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import (
    AgentResult,
    ClauseDiff,
    EditOutcome,
    ProposedEdit,
    WalkAwayOutcome,
)
from contract_agent.service import ContractService
from contract_agent.store import ApprovalRecord, InMemoryStore
from contract_agent.walkaway import (
    capture_walk_away_outcomes,
    classify_line_origin,
    resolve_walk_away_outcomes,
)


def _result(edits=(), outcomes=(), diffs=(), **kwargs) -> AgentResult:
    return AgentResult(
        contract_id=kwargs.pop("contract_id", "CTR-ORG-1"),
        proposed_edits=list(edits),
        edit_outcomes=list(outcomes),
        clause_diffs=list(diffs),
        **kwargs,
    )


def _line(clause_id="C-1", clause_type="liability_cap") -> WalkAwayOutcome:
    return WalkAwayOutcome(clause_id=clause_id, clause_type=clause_type, status="pending")


def _edit(index, tier=2, clause_id="C-1", clause_type="liability_cap") -> ProposedEdit:
    return ProposedEdit(
        clause_id=clause_id, clause_type=clause_type, tier=tier, kind="replace_clause"
    )


def _outcome(index, outcome) -> EditOutcome:
    return EditOutcome(edit_index=index, outcome=outcome)


# ---------------------------------------------------------------------------
# The classification itself
# ---------------------------------------------------------------------------


def test_sent_intact_when_every_line_edit_was_accepted():
    result = _result(
        edits=[_edit(0), _edit(1)],
        outcomes=[_outcome(0, "accepted"), _outcome(1, "accepted")],
    )
    origin, detail = classify_line_origin(result, _line())
    assert origin == "sent_intact"
    assert "exactly as the packet drafted it" in detail


def test_modified_by_counsel_when_counsel_settled_their_own_language():
    result = _result(
        edits=[_edit(0)], outcomes=[_outcome(0, "modified")]
    )
    origin, _ = classify_line_origin(result, _line())
    assert origin == "modified_by_counsel"


def test_rejected_by_counsel_wins_over_accepted_sibling_edits():
    # One rejected edit among the line's edits means the line as
    # proposed never went out — partial acceptance does not soften
    # that fact.
    result = _result(
        edits=[_edit(0), _edit(1)],
        outcomes=[_outcome(0, "accepted"), _outcome(1, "rejected")],
    )
    origin, _ = classify_line_origin(result, _line())
    assert origin == "rejected_by_counsel"


def test_unmarked_when_no_outcomes_and_when_only_some_edits_marked():
    result = _result(edits=[_edit(0)])
    assert classify_line_origin(result, _line())[0] == "unmarked"
    result = _result(
        edits=[_edit(0), _edit(1)], outcomes=[_outcome(0, "accepted")]
    )
    assert classify_line_origin(result, _line())[0] == "unmarked"


def test_no_edit_on_record_and_the_clause_type_fallback():
    result = _result(edits=[_edit(0, tier=1)])
    assert classify_line_origin(result, _line())[0] == "no_edit_on_record"
    # The clause was renumbered in the follow-up's alignment: the
    # type-level fallback still finds the line's edit.
    result = _result(
        edits=[_edit(0, clause_id="C-9")], outcomes=[_outcome(0, "accepted")]
    )
    assert classify_line_origin(result, _line(clause_id="C-1"))[0] == "sent_intact"


def test_tier_one_edits_do_not_carry_the_line():
    # A rejected FIRST-fallback edit is not the walk-away line
    # being dropped — only tier-2 edits carry this line.
    result = _result(
        edits=[_edit(0, tier=1), _edit(1, tier=2)],
        outcomes=[_outcome(0, "rejected"), _outcome(1, "accepted")],
    )
    assert classify_line_origin(result, _line())[0] == "sent_intact"


# ---------------------------------------------------------------------------
# Capture stamps it; resolution refreshes it when outcomes land late
# ---------------------------------------------------------------------------


def test_capture_and_resolution_keep_the_origin_current():
    diff = ClauseDiff(
        clause_id="C-1", clause_type="liability_cap", position="walk_away",
        proposed_tier=2, tier_language="the walk-away line",
    )
    decided = _result(
        edits=[_edit(0)], diffs=[diff], decision="approved", decided_by="C. Ounsel"
    )
    outcomes = capture_walk_away_outcomes(decided)
    assert outcomes[0].origin == "unmarked"
    # Counsel marks the edit AFTER the decision (the standing
    # record_edit_outcomes flow): re-resolution must not leave the
    # stale origin standing.
    decided.edit_outcomes = [_outcome(0, "modified")]
    follow_up = _result(contract_id="CTR-ORG-2")
    chain = [
        ApprovalRecord(result=decided),
        ApprovalRecord(result=follow_up, contract={"supersedes": "CTR-ORG-1"}),
    ]
    resolved = resolve_walk_away_outcomes(outcomes, chain, "CTR-ORG-1")
    assert resolved[0].origin == "modified_by_counsel"


# ---------------------------------------------------------------------------
# The profile: held rates by origin, and the spoken contrast
# ---------------------------------------------------------------------------


def _decided_record(cid, *, origin_edits, origin_outcomes, status, clause_type="liability_cap", supersedes=""):
    line = WalkAwayOutcome(
        clause_id="C-1", clause_type=clause_type, status=status,
        final_position="preferred" if status == "held" else "walk_away",
    )
    result = _result(
        contract_id=cid,
        edits=origin_edits,
        outcomes=origin_outcomes,
        walk_away_outcomes=[line],
        counterparty_name="Northwind Traders Ltd",
        decision="approved",
    )
    contract_payload = {"counterparty_name": "Northwind Traders Ltd"}
    if supersedes:
        contract_payload["supersedes"] = supersedes
    return ApprovalRecord(result=result, contract=contract_payload)


def test_profile_aggregates_held_rates_by_origin():
    records = [
        # Sent intact, and the follow-up held it.
        _decided_record(
            "CTR-ORG-A", origin_edits=[_edit(0)],
            origin_outcomes=[_outcome(0, "accepted")], status="held",
        ),
        # Rejected at counsel's desk; the follow-up still sits
        # beyond the line — dropped, with the origin on record.
        _decided_record(
            "CTR-ORG-B", origin_edits=[_edit(0)],
            origin_outcomes=[_outcome(0, "rejected")], status="dropped",
            supersedes="CTR-ORG-A",
        ),
        # Modified by counsel; the follow-up softened it.
        _decided_record(
            "CTR-ORG-C", origin_edits=[_edit(0)],
            origin_outcomes=[_outcome(0, "modified")], status="softened",
            supersedes="CTR-ORG-B",
        ),
    ]
    profile = build_concession_profile("Northwind Traders Ltd", records)
    by_origin = profile["walk_away_lines"]["by_origin"]
    assert by_origin["sent_intact"]["held_rate"] == 1.0
    assert by_origin["sent_intact"]["resolved"] == 1
    assert by_origin["rejected_by_counsel"]["held_rate"] == 0.0
    assert by_origin["modified_by_counsel"]["resolved"] == 1
    lines = concession_evidence_lines(profile)
    origin_line = next(l for l in lines if "by origin" in l)
    assert "counsel's own desk" in origin_line


def test_profile_origin_contrast_needs_both_sides_resolved():
    # Only intact lines resolved: no contrast is spoken — one
    # side's rate alone would invite a story the record does not
    # support. (The chain is multi-round, so evidence lines ARE
    # computed; the origin line is the one withheld.)
    records = [
        _decided_record(
            "CTR-ORG-A", origin_edits=[_edit(0)],
            origin_outcomes=[_outcome(0, "accepted")], status="held",
        ),
        ApprovalRecord(
            result=_result(
                contract_id="CTR-ORG-B", counterparty_name="Northwind Traders Ltd"
            ),
            contract={
                "counterparty_name": "Northwind Traders Ltd",
                "supersedes": "CTR-ORG-A",
            },
        ),
    ]
    profile = build_concession_profile("Northwind Traders Ltd", records)
    assert profile["multi_round_chains"] == 1
    assert not any(
        "by origin" in line for line in concession_evidence_lines(profile)
    )


# ---------------------------------------------------------------------------
# End to end on the sample chain: counsel drops one line, sends one intact
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def samples() -> dict:
    return {c.contract_id: c for c in sample_contract_models()}


def test_sample_chain_origins_land_on_the_stored_outcomes(samples):
    from contract_agent.schemas import EditOutcomeInput

    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    result = svc.analyze(samples["CTR-2014"])
    tier_two = [
        (i, e) for i, e in enumerate(result.proposed_edits) if e.tier == 2
    ]
    assert tier_two  # the packet carries walk-away lines as edits
    # Counsel rejects the liability line's edits and accepts the
    # rest, then approves the packet with those outcomes.
    inputs = [
        EditOutcomeInput(
            edit_index=i,
            outcome="rejected" if e.clause_type == "liability_cap" else "accepted",
        )
        for i, e in tier_two
    ]
    svc.approve(
        "CTR-2014", approver="C. Ounsel", reason="send it", edit_outcomes=inputs
    )
    svc.analyze(samples["CTR-2015"])
    decided = svc.get("CTR-2014")
    origins = {o.clause_type: o.origin for o in decided.walk_away_outcomes}
    assert origins["liability_cap"] == "rejected_by_counsel"
    assert origins["ip_ownership"] == "sent_intact"
    # Both lines held in the follow-up — including the one counsel
    # never sent. The profile keeps the two facts separate.
    profile = svc.counterparty_profile(decided.counterparty_name)
    by_origin = profile["walk_away_lines"]["by_origin"]
    assert by_origin["rejected_by_counsel"]["held"] == 1
    assert by_origin["sent_intact"]["held"] == 1
