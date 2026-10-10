"""Reflection patterns: the agreed edit that never lands, twice.

The reflection check (``edit_reflection.py``) answers a per-round
question: the edits counsel agreed last round — are they in this
draft? One ``agreed_not_reflected`` flag is a drafting oversight,
the kind of thing a nudge on the next call fixes. But negotiations
have a second shape: counsel agrees the same ask again on the next
round — the clause is still flagged, the edit is re-proposed, the
call goes well — and the draft that follows STILL does not show it.
Twice is no longer an oversight; it is how this negotiation is
going, and the packet should say so and answer it.

This module reads the chain's stored reflections and names that
pattern, per clause type: the type's two most recent measurable
checks (``reflected`` / ``agreed_not_reflected`` — a
``not_checkable`` entry measures nothing and neither builds nor
breaks a streak) both came back ``agreed_not_reflected``, in
consecutive rounds of the chain. A later ``reflected`` check
resolves the pattern — the language landed, late, and the record
says that instead. Keyed by clause type deliberately: the edit is
re-proposed from the clause's tier language each round, so the ask
counsel keeps agreeing IS the clause type's ask, and the round
history (which rounds agreed, which follow-ups flagged) is carried
on the pattern for the brief.

A pattern moves the packet: the clause's proposed tier escalates
to the walk-away line (``fallback_tiers.py`` reads the pattern set
as an escalation driver of its own, ahead of the round-count rule)
— not because two misses are twice as bad as one, but because the
soft concession has now been agreed twice and honoured neither
time; there is nothing softer left to propose.
"""

from __future__ import annotations

from .schemas import AgentResult, ReflectionPattern

_MEASURABLE = ("reflected", "agreed_not_reflected")


def find_reflection_patterns(chain: list[AgentResult]) -> list[ReflectionPattern]:
    """The reflection patterns over a negotiation chain, oldest →
    newest (``ContractService._chain_rounds`` order). Each result in
    the chain carries the reflection of ITS predecessor's agreed
    edits, so the chain's checks are read off the results in order;
    a pattern needs at least three rounds (an agreement, and two
    follow-ups that checked it)."""
    if len(chain) < 3:
        return []
    # Per clause type: the measurable checks, in chain order, as
    # (round index, follow-up contract id, agreed-in contract id, status).
    checks: dict[str, list[tuple[int, str, str, str]]] = {}
    for index, result in enumerate(chain):
        if index == 0:
            continue  # the first round has no predecessor to reflect
        agreed_in = chain[index - 1].contract_id
        for entry in result.edit_reflection:
            if entry.status not in _MEASURABLE:
                continue
            checks.setdefault(entry.clause_type, []).append(
                (index, result.contract_id, agreed_in, entry.status)
            )
    patterns: list[ReflectionPattern] = []
    for clause_type, entries in checks.items():
        if len(entries) < 2:
            continue
        (prev_index, prev_follow, prev_agreed, prev_status) = entries[-2]
        (last_index, last_follow, last_agreed, last_status) = entries[-1]
        if prev_status != "agreed_not_reflected" or last_status != "agreed_not_reflected":
            continue  # the latest check landed (or the streak is broken)
        if last_index != prev_index + 1:
            continue  # flagged twice, but not in consecutive follow-ups
        flagged_in = [
            follow for _, follow, _, status in entries if status == "agreed_not_reflected"
        ]
        patterns.append(
            ReflectionPattern(
                clause_type=clause_type,
                flagged_in=flagged_in,
                agreed_in=[prev_agreed, last_agreed],
                flags=len(flagged_in),
                detail=(
                    f"{clause_type}: counsel agreed an edit on this clause in "
                    f"{prev_agreed} and the follow-up {prev_follow} did not show "
                    f"it; counsel agreed the edit again in {last_agreed} and "
                    f"{last_follow} does not show it either — agreed, but not "
                    "reflected, in two consecutive follow-ups. The packet "
                    "escalates this clause to the walk-away line."
                ),
            )
        )
    patterns.sort(key=lambda p: p.clause_type)
    return patterns
