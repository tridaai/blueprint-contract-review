"""Negotiation-round diffing: what the last round actually won.

A revised draft from the counterparty is not a new contract — it is
round N+1 of a negotiation counsel is already inside. Re-reviewing it
from scratch answers "where does this draft stand?" but not the
question counsel asks first: *"what moved?"* This module aligns the
clauses of the superseded round with the clauses of the new round and
reports the verdict movement per clause — walk_away → preferred is a
win to bank, preferred → acceptable_fallback is ground given up, and
a clause type that appears or disappears is a change in the deal's
shape, not just its wording.

Alignment is deterministic, in code: clauses pair up by clause type,
and within a type a matching heading wins over document order (a
counterparty who renumbers sections between rounds should not break
the pairing); leftovers pair in document order. Movement is the
severity comparison along the position ladder (``POSITION_SEVERITY``)
— no model judgement anywhere in it.
"""

from __future__ import annotations

import re

from .schemas import (
    POSITION_SEVERITY,
    AgentResult,
    ClauseMovement,
    RoundDiff,
)


def _heading_key(heading: str) -> str:
    return re.sub(r"\s+", " ", (heading or "").strip().lower()).strip(" .:§")


def align_clauses(
    prior: AgentResult, current: AgentResult
) -> list[tuple[str, str]]:
    """Pair clause ids across the two rounds.

    Returns ``(prior_clause_id, current_clause_id)`` pairs; an empty
    string on one side marks a removed (prior-only) or new
    (current-only) clause. Deterministic for identical inputs.
    """
    prior_units = {u.clause_id: u for u in prior.clauses}
    current_units = {u.clause_id: u for u in current.clauses}
    prior_type = {v.clause_id: v.clause_type for v in prior.clause_verdicts}
    current_type = {v.clause_id: v.clause_type for v in current.clause_verdicts}

    unmatched_prior: dict[str, list[str]] = {}
    for verdict in prior.clause_verdicts:
        unmatched_prior.setdefault(verdict.clause_type, []).append(verdict.clause_id)

    pairs: list[tuple[str, str]] = []
    for verdict in current.clause_verdicts:
        candidates = unmatched_prior.get(verdict.clause_type) or []
        if not candidates:
            pairs.append(("", verdict.clause_id))
            continue
        current_heading = _heading_key(
            current_units.get(verdict.clause_id).heading
            if current_units.get(verdict.clause_id)
            else ""
        )
        chosen = candidates[0]
        if current_heading:
            for candidate in candidates:
                unit = prior_units.get(candidate)
                if unit and _heading_key(unit.heading) == current_heading:
                    chosen = candidate
                    break
        candidates.remove(chosen)
        pairs.append((chosen, verdict.clause_id))

    paired_prior = {p for p, _ in pairs if p}
    for verdict in prior.clause_verdicts:
        if verdict.clause_id not in paired_prior:
            pairs.append((verdict.clause_id, ""))
    return pairs


def build_round_diff(prior: AgentResult, current: AgentResult) -> RoundDiff:
    """The verdict-movement report between a superseded round and its
    successor. Both inputs are completed analysis results."""
    prior_units = {u.clause_id: u for u in prior.clauses}
    current_units = {u.clause_id: u for u in current.clauses}
    prior_verdicts = {v.clause_id: v for v in prior.clause_verdicts}
    current_verdicts = {v.clause_id: v for v in current.clause_verdicts}

    movements: list[ClauseMovement] = []
    for prior_id, current_id in align_clauses(prior, current):
        prior_v = prior_verdicts.get(prior_id)
        current_v = current_verdicts.get(current_id)
        clause_type = (current_v or prior_v).clause_type
        if prior_v is not None and current_v is not None:
            delta = POSITION_SEVERITY[current_v.position] - POSITION_SEVERITY[prior_v.position]
            movement = "improved" if delta < 0 else "worsened" if delta > 0 else "unchanged"
        elif current_v is not None:
            movement = "new"
        else:
            movement = "removed"
        prior_unit = prior_units.get(prior_id)
        current_unit = current_units.get(current_id)
        movements.append(
            ClauseMovement(
                clause_type=clause_type,
                heading=(current_unit.heading if current_unit else "")
                or (prior_unit.heading if prior_unit else ""),
                prior_clause_id=prior_id,
                clause_id=current_id,
                prior_ref=prior_unit.ref if prior_unit else "",
                ref=current_unit.ref if current_unit else "",
                prior_position=prior_v.position if prior_v else "",
                position=current_v.position if current_v else "",
                movement=movement,
            )
        )

    counts = {name: 0 for name in ("improved", "worsened", "unchanged", "new", "removed")}
    for entry in movements:
        counts[entry.movement] += 1
    summary = []
    for entry in movements:
        label = entry.heading or entry.clause_type
        if entry.movement in ("improved", "worsened", "unchanged"):
            summary.append(
                f"{label} ({entry.clause_type}): {entry.prior_position} → "
                f"{entry.position} — {entry.movement}"
            )
        elif entry.movement == "new":
            summary.append(
                f"{label} ({entry.clause_type}): new clause — {entry.position}"
            )
        else:
            summary.append(
                f"{label} ({entry.clause_type}): clause removed "
                f"(was {entry.prior_position})"
            )
    return RoundDiff(
        previous_contract_id=prior.contract_id,
        contract_id=current.contract_id,
        movements=movements,
        improved=counts["improved"],
        worsened=counts["worsened"],
        unchanged=counts["unchanged"],
        new_clauses=counts["new"],
        removed_clauses=counts["removed"],
        summary=summary,
    )
