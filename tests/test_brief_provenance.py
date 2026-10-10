"""Origin-aware brief warnings (round 11): a walk-away line can
be lost at counsel's own desk — modified or rejected before it
goes out — or at the counterparty's. Round 10 recorded each
decided line's origin; the negotiation brief now reads them back
as warnings, before counsel concedes: lines counsel moved, lines
the counterparty moved anyway, and the desk's own pattern when
most of a type's decided lines were counsel-modified and the
current packet proposes the playbook's line for that type again.
"""

from __future__ import annotations

from contract_agent.brief import (
    build_negotiation_brief,
    line_provenance_warnings,
)
from contract_agent.schemas import (
    AgentResult,
    ClauseDiff,
    WalkAwayOutcome,
)


def _outcome(clause_type="liability_cap", origin="sent_intact",
             status="held", **kwargs) -> WalkAwayOutcome:
    return WalkAwayOutcome(
        clause_id=kwargs.pop("clause_id", "C-1"),
        clause_type=clause_type,
        status=status,
        origin=origin,
        origin_detail=kwargs.pop(
            "origin_detail",
            {
                "modified_by_counsel": (
                    "Counsel modified the language of at least one edit "
                    "carrying this line before it went out."
                ),
                "rejected_by_counsel": (
                    "Counsel rejected at least one of the edits carrying "
                    "this line at decision time."
                ),
                "sent_intact": (
                    "Counsel accepted every edit carrying this line as "
                    "proposed."
                ),
            }.get(origin, ""),
        ),
        **kwargs,
    )


def _result(contract_id, outcomes=(), diffs=()) -> AgentResult:
    return AgentResult(
        contract_id=contract_id,
        walk_away_outcomes=list(outcomes),
        clause_diffs=list(diffs),
    )


def _tier2(clause_type="liability_cap") -> ClauseDiff:
    return ClauseDiff(
        clause_id="C-1", clause_type=clause_type,
        position="walk_away", proposed_tier=2,
    )


# ---------------------------------------------------------------------------
# The three provenances
# ---------------------------------------------------------------------------


def test_counsel_modified_line_warns_with_its_outcome():
    decided = _result("CTR-PRV-1", [_outcome(origin="modified_by_counsel", status="softened")])
    latest = _result("CTR-PRV-2")
    warnings = line_provenance_warnings([decided, latest], latest)
    assert len(warnings) == 1
    assert "liability_cap" in warnings[0]
    assert "CTR-PRV-1" in warnings[0]
    assert "modified" in warnings[0]
    assert "softened it" in warnings[0]
    assert "counsel's settled language" in warnings[0]


def test_counsel_rejected_line_warns_it_never_reached_the_counterparty():
    decided = _result("CTR-PRV-1", [_outcome(origin="rejected_by_counsel", status="dropped")])
    latest = _result("CTR-PRV-2")
    warnings = line_provenance_warnings([decided, latest], latest)
    assert len(warnings) == 1
    assert "never reached the counterparty" in warnings[0]
    assert "lost at counsel's desk first" in warnings[0]


def test_intact_line_the_counterparty_moved_warns_the_other_way():
    decided = _result("CTR-PRV-1", [_outcome(origin="sent_intact", status="dropped")])
    latest = _result("CTR-PRV-2")
    warnings = line_provenance_warnings([decided, latest], latest)
    assert len(warnings) == 1
    assert "exactly as the packet proposed it" in warnings[0]
    assert "the movement is the counterparty's" in warnings[0]


def test_intact_line_that_held_draws_no_warning():
    decided = _result("CTR-PRV-1", [_outcome(origin="sent_intact", status="held")])
    latest = _result("CTR-PRV-2")
    assert line_provenance_warnings([decided, latest], latest) == []


def test_unmarked_origins_say_nothing():
    decided = _result("CTR-PRV-1", [_outcome(origin="unmarked", status="dropped")])
    latest = _result("CTR-PRV-2")
    assert line_provenance_warnings([decided, latest], latest) == []


# ---------------------------------------------------------------------------
# The desk's own pattern
# ---------------------------------------------------------------------------


def test_desk_pattern_warns_when_the_current_packet_proposes_the_line_again():
    chain = [
        _result("CTR-PRV-1", [_outcome(origin="modified_by_counsel", status="softened")]),
        _result("CTR-PRV-2", [_outcome(origin="modified_by_counsel", status="held")]),
    ]
    latest = _result("CTR-PRV-3", diffs=[_tier2()])
    warnings = line_provenance_warnings([*chain, latest], latest)
    pattern = [w for w in warnings if "Pattern at counsel's own desk" in w]
    assert len(pattern) == 1
    assert "2 of the 2 decided" in pattern[0]
    assert "liability_cap" in pattern[0]


def test_desk_pattern_needs_the_current_line_to_warn_about():
    chain = [
        _result("CTR-PRV-1", [_outcome(origin="modified_by_counsel", status="softened")]),
        _result("CTR-PRV-2", [_outcome(origin="modified_by_counsel", status="held")]),
    ]
    latest = _result("CTR-PRV-3")  # no tier-2 line for the type this round
    warnings = line_provenance_warnings([*chain, latest], latest)
    assert all("Pattern at counsel's own desk" not in w for w in warnings)


def test_a_single_modified_line_is_not_a_pattern():
    chain = [
        _result("CTR-PRV-1", [_outcome(origin="modified_by_counsel", status="softened")]),
        _result("CTR-PRV-2", [_outcome(origin="sent_intact", status="held")]),
    ]
    latest = _result("CTR-PRV-3", diffs=[_tier2()])
    warnings = line_provenance_warnings([*chain, latest], latest)
    assert all("Pattern at counsel's own desk" not in w for w in warnings)


# ---------------------------------------------------------------------------
# In the brief itself
# ---------------------------------------------------------------------------


def test_brief_carries_the_provenance_section_before_the_edits():
    decided = _result("CTR-PRV-1", [_outcome(origin="modified_by_counsel", status="softened")])
    latest = _result("CTR-PRV-2")
    md = build_negotiation_brief([decided, latest], latest)
    assert "## Line provenance — read before conceding" in md
    assert md.index("## Line provenance") < md.index("## Proposed edits")


def test_brief_without_decided_lines_has_no_provenance_section():
    latest = _result("CTR-PRV-1")
    md = build_negotiation_brief([latest], latest)
    assert "Line provenance" not in md
