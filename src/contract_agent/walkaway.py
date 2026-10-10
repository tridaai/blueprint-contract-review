"""Walk-away line acceptance tracking: did the final text stay at
or above the line the packet proposed?

When a packet proposes the walk-away line for a clause (tier 2 —
``fallback_tiers.py``), that proposal is the negotiation's most
consequential sentence, and until now nothing recorded what became
of it. The verdicts moved or they didn't; whether the LINE held —
the position counsel actually authorised as the final one — was
nowhere on the record.

This module puts it there, in two steps, both in code:

**Capture** (at counsel's decision, from ``ContractService``): for
every clause diff on the decided result whose proposed tier is 2,
a ``WalkAwayOutcome`` is recorded on the decision record — the
line's language, the clause's position at decision time, and the
decision itself. A packet that proposed no walk-away line captures
nothing.

**Resolution** (against the follow-up round): the counterparty's
next draft is the evidence. The follow-up round's position for the
clause — aligned by clause id, falling back to clause type —
classifies the outcome: at an acceptable position (preferred /
acceptable_fallback) the final text is at or above the line
(``held``); at review_required it came off the walk-away text but
stopped short of acceptable (``softened``); still at walk_away,
the final text sits beyond the line (``dropped``); absent from the
follow-up, ``clause_removed`` — neither held nor tested. With no
follow-up in the store the outcome stays ``pending`` and resolves
when one arrives (the service re-resolves a chain's outcomes
whenever a new round is analysed or decided).

The concession profile (``concessions.py``) reads the captured
outcomes as its hold statistics: decision evidence — lines counsel
actually decided on, and what the next draft did with them — rather
than verdict movement alone.
"""

from __future__ import annotations

from .concessions import _chains
from .schemas import POSITION_SEVERITY, AgentResult, WalkAwayOutcome
from .store import ApprovalRecord

# Positions at or above the walk-away line: the playbook's own
# acceptable outcomes. review_required sits between — off the
# walk-away text, but not at a position the playbook accepts.
_HELD = ("preferred", "acceptable_fallback")


def capture_walk_away_outcomes(
    result: AgentResult, decided_at: str = ""
) -> list[WalkAwayOutcome]:
    """The walk-away lines a decided packet proposed, as fresh
    (pending) outcomes anchored to the result's decision fields."""
    outcomes: list[WalkAwayOutcome] = []
    for diff in result.clause_diffs:
        if diff.proposed_tier != 2:
            continue
        outcomes.append(
            WalkAwayOutcome(
                clause_id=diff.clause_id,
                clause_type=diff.clause_type,
                line_language=diff.tier_language or diff.playbook_fallback,
                position_at_decision=diff.position,
                decision=result.decision or "",
                decided_by=result.decided_by or "",
                captured_at=decided_at or (result.decided_at or ""),
                status="pending",
                detail=(
                    "No follow-up round is in the store yet — whether "
                    "the line held waits on the counterparty's next draft."
                ),
            )
        )
    return outcomes


def chain_for(
    records: list[ApprovalRecord], contract_id: str
) -> list[ApprovalRecord]:
    """The negotiation chain containing ``contract_id``, oldest →
    newest (empty when the contract is not in the record set)."""
    for chain in _chains(records):
        if any(r.result.contract_id == contract_id for r in chain):
            return chain
    return []


def _follow_up_position(follow_up: AgentResult, outcome: WalkAwayOutcome) -> str | None:
    """The follow-up round's position for the outcome's clause:
    aligned by clause id first, then by clause type. None when the
    clause does not appear in the follow-up at all."""
    for verdict in follow_up.clause_verdicts:
        if verdict.clause_id == outcome.clause_id:
            return verdict.position
    for verdict in follow_up.clause_verdicts:
        if verdict.clause_type == outcome.clause_type:
            return verdict.position
    return None


def resolve_walk_away_outcomes(
    outcomes: list[WalkAwayOutcome],
    chain: list[ApprovalRecord],
    decided_contract_id: str,
) -> list[WalkAwayOutcome]:
    """Resolve captured outcomes against the follow-up round of
    their chain. Pure: returns updated copies; outcomes whose
    decided contract is not in ``chain`` (or which end the chain)
    come back pending. Deterministic over the stored chain, so the
    service can re-resolve whenever the chain grows."""
    ids = [r.result.contract_id for r in chain]
    if decided_contract_id not in ids:
        return [o.model_copy() for o in outcomes]
    index = ids.index(decided_contract_id)
    follow_up = chain[index + 1].result if index + 1 < len(chain) else None
    resolved: list[WalkAwayOutcome] = []
    for outcome in outcomes:
        updated = outcome.model_copy()
        if follow_up is None:
            updated.status = "pending"
            updated.final_position = ""
            updated.resolved_in = ""
            updated.detail = (
                "No follow-up round is in the store yet — whether "
                "the line held waits on the counterparty's next draft."
            )
        else:
            position = _follow_up_position(follow_up, outcome)
            updated.resolved_in = follow_up.contract_id
            if position is None:
                updated.status = "clause_removed"
                updated.final_position = ""
                updated.detail = (
                    f"The clause does not appear in the follow-up "
                    f"round ({follow_up.contract_id}) — the line was "
                    "neither held nor tested."
                )
            elif position in _HELD:
                updated.status = "held"
                updated.final_position = position
                updated.detail = (
                    f"The follow-up round ({follow_up.contract_id}) "
                    f"places this clause at {position} — the final "
                    "text is at or above the walk-away line."
                )
            elif POSITION_SEVERITY.get(position, 2) >= POSITION_SEVERITY["walk_away"]:
                updated.status = "dropped"
                updated.final_position = position
                updated.detail = (
                    f"The follow-up round ({follow_up.contract_id}) "
                    "still places this clause at walk_away — the "
                    "final text sits beyond the line the packet "
                    "proposed."
                )
            else:
                updated.status = "softened"
                updated.final_position = position
                updated.detail = (
                    f"The follow-up round ({follow_up.contract_id}) "
                    f"places this clause at {position} — off the "
                    "walk-away text, short of an acceptable position: "
                    "the line was softened."
                )
        resolved.append(updated)
    return resolved
