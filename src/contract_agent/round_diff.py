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

``build_round_chain`` threads those pairwise alignments through a
whole negotiation (three rounds and up): every clause gets a
movement history, a cumulative first→latest verdict, and a re-trade
flag when ground won in an earlier round is given back later.
"""

from __future__ import annotations

import re

from .schemas import (
    POSITION_SEVERITY,
    AgentResult,
    ClauseHistory,
    ClauseMovement,
    RoundChain,
    RoundDiff,
    RoundPosition,
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


# ---------------------------------------------------------------------------
# Round chains: the whole negotiation, not just the last step
# ---------------------------------------------------------------------------


def _step_movement(before: str, after: str) -> str:
    """Movement across one round-to-round step; ``""`` when the clause
    was absent from both rounds (its track had not started)."""
    if before and after:
        delta = POSITION_SEVERITY[after] - POSITION_SEVERITY[before]
        return "improved" if delta < 0 else "worsened" if delta > 0 else "unchanged"
    if after:
        return "new"
    if before:
        return "removed"
    return ""


def build_round_chain(rounds: list[AgentResult]) -> RoundChain:
    """Track every clause across a whole negotiation chain.

    ``rounds`` is the chain oldest → newest (two or more completed
    results). Clauses are threaded through consecutive pairwise
    alignments (``align_clauses``): a clause keeps its track while it
    survives from one round to the next; a clause that disappears
    ends its track, and a clause that (re)appears starts a new one —
    the alignment only ever sees adjacent rounds, and the chain does
    not pretend otherwise.

    On top of the per-step movements the chain reports what a single
    diff cannot: the cumulative movement (first seat → latest seat)
    and RE-TRADES — a clause that improved in an earlier round and
    worsened again later. Counsel settled that ground once; the
    counterparty taking it back is a finding about how they negotiate,
    not a footnote in a diff.
    """
    n = len(rounds)
    tracks: list[dict] = []
    live: dict[str, dict] = {}  # clause_id in the previous round -> track

    for idx, result in enumerate(rounds):
        units = {u.clause_id: u for u in result.clauses}
        verdicts = {v.clause_id: v for v in result.clause_verdicts}
        if idx == 0:
            for verdict in result.clause_verdicts:
                unit = units.get(verdict.clause_id)
                track = {
                    "clause_type": verdict.clause_type,
                    "heading": unit.heading if unit else "",
                    "positions": [verdict.position],
                    "clause_ids": [verdict.clause_id],
                }
                tracks.append(track)
                live[verdict.clause_id] = track
            continue
        new_live: dict[str, dict] = {}
        for prior_id, current_id in align_clauses(rounds[idx - 1], result):
            if prior_id and current_id:
                track = live[prior_id]
                verdict = verdicts[current_id]
                track["positions"].append(verdict.position)
                track["clause_ids"].append(current_id)
                unit = units.get(current_id)
                if unit and unit.heading:
                    track["heading"] = unit.heading
                new_live[current_id] = track
            elif current_id:
                verdict = verdicts[current_id]
                unit = units.get(current_id)
                track = {
                    "clause_type": verdict.clause_type,
                    "heading": unit.heading if unit else "",
                    "positions": [""] * idx + [verdict.position],
                    "clause_ids": [""] * idx + [current_id],
                }
                tracks.append(track)
                new_live[current_id] = track
            else:  # removed in this round: the track ends here
                track = live[prior_id]
                track["positions"].append("")
                track["clause_ids"].append("")
        live = new_live

    # A track that ended mid-chain (removed, never reappearing) is
    # absent from every later round — pad it out so every history
    # spans the whole chain.
    for track in tracks:
        while len(track["positions"]) < n:
            track["positions"].append("")
            track["clause_ids"].append("")

    histories: list[ClauseHistory] = []
    re_traded_types: list[str] = []
    cumulative_counts = {"improved": 0, "worsened": 0, "unchanged": 0}
    for track in tracks:
        positions: list[str] = track["positions"]
        movements = [
            _step_movement(positions[i], positions[i + 1]) for i in range(n - 1)
        ]
        present = [i for i, p in enumerate(positions) if p]
        first, last = present[0], present[-1]
        if first == 0 and last == n - 1:
            cumulative = _step_movement(positions[0], positions[-1])
        elif last < n - 1:
            cumulative = "removed"
        else:
            cumulative = "new"
        if cumulative in cumulative_counts:
            cumulative_counts[cumulative] += 1

        re_traded = False
        detail = ""
        improved_steps = [i for i, m in enumerate(movements) if m == "improved"]
        if improved_steps:
            first_improve = improved_steps[0]
            later_worse = next(
                (j for j, m in enumerate(movements) if m == "worsened" and j > first_improve),
                None,
            )
            if later_worse is not None:
                re_traded = True
                detail = (
                    f"improved at round {first_improve + 2} "
                    f"({positions[first_improve]} → {positions[first_improve + 1]}), "
                    f"then worsened at round {later_worse + 2} "
                    f"({positions[later_worse]} → {positions[later_worse + 1]})"
                )
                re_traded_types.append(track["clause_type"])
        histories.append(
            ClauseHistory(
                clause_type=track["clause_type"],
                heading=track["heading"],
                track=[
                    RoundPosition(
                        contract_id=rounds[i].contract_id,
                        clause_id=track["clause_ids"][i],
                        position=positions[i],
                    )
                    for i in range(n)
                ],
                movements=movements,
                cumulative_movement=cumulative,
                re_traded=re_traded,
                re_trade_detail=detail,
            )
        )

    rounds_to_clean: int | None = None
    for idx, result in enumerate(rounds):
        if all(v.position != "walk_away" for v in result.clause_verdicts):
            rounds_to_clean = idx + 1
            break

    summary = [
        f"{n} rounds ({rounds[0].contract_id} → {rounds[-1].contract_id}) · "
        f"{len(re_traded_types)} re-traded clause(s) · "
        + (
            f"first round with no walk-away clause: round {rounds_to_clean}"
            if rounds_to_clean is not None
            else "no round so far has been free of walk-away clauses"
        )
    ]
    for history in histories:
        label = history.heading or history.clause_type
        path = " → ".join(p.position or "—" for p in history.track)
        line = f"{label} ({history.clause_type}): {path} — cumulative: {history.cumulative_movement}"
        if history.re_traded:
            line += f" — RE-TRADED ({history.re_trade_detail})"
        summary.append(line)

    return RoundChain(
        contract_ids=[r.contract_id for r in rounds],
        rounds=n,
        histories=histories,
        re_trades=len(re_traded_types),
        re_traded_clause_types=re_traded_types,
        cumulative_improved=cumulative_counts["improved"],
        cumulative_worsened=cumulative_counts["worsened"],
        cumulative_unchanged=cumulative_counts["unchanged"],
        rounds_to_no_walk_away=rounds_to_clean,
        summary=summary,
    )
