"""Lapsed-covenant readings: a later letter read against the
restrictions that already lapsed.

The register knows a prior letter's covenant lapsed, and —
since round 16 — whether the lapse was followed through and
closed. But a chain does not end at the lapse: a LATER letter
to the same employee arrives carrying a fresh non-compete, and
read on its own it looks like a continuation of the old
restriction. It is not. The old covenant ran its course and
expired; what the new letter asks is a NEW bargain, and counsel
should price it as the new promise it is — a point that is easy
to miss when the two letters sit in the chain as similar paper.

This module makes that reading at analysis time, the way the
drift modules read the annexes across rounds. For each covenant
kind the new letter carries afresh, the nearest prior round of
the chain that carried a DATED covenant of that kind is found;
if that covenant has lapsed (its expiry reached), the reading
is attached to the new result (``AgentResult.
lapsed_covenant_readings``): the prior round named, the expiry
named, and the lapse's evidence named — confirmed lifted on
the record (by whom, when), notified but unconfirmed, or still
standing open. A prior covenant still running is NOT this
reading's subject: the new letter beside a live restriction is
a continuation question for counsel's own read of the
register, not a lapse. The reading is information —
``consistent``, never a finding: nothing is wrong with a new
bargain; it is wrong only to mistake it for the old one.

Since round 18 the reading covers the commercial covenants
too: a later round that re-imposes an exclusivity or a
non-circumvention covenant after the earlier round's one
lapsed is read the same way — the restriction that ran its
course is named, and the fresh covenant is a new bargain
under the new round, for the same reason.
"""

from __future__ import annotations

from datetime import date

from .obligations import ALL_COVENANT_KINDS, follow_through_state
from .schemas import ExhibitCheck, Obligation

_LABELS = {
    "non_compete": "non-compete",
    "non_solicit": "non-solicitation",
    "exclusivity": "exclusivity",
    "non_circumvention": "non-circumvention",
}


def build_lapsed_covenant_readings(
    prior_rounds: list[tuple[str, list[Obligation], list[dict]]],
    current_obligations: list[Obligation],
    today: date,
) -> list[ExhibitCheck]:
    """Read the current round's fresh covenants against the
    chain's lapsed ones. ``prior_rounds`` is (contract_id, the
    round's register, the record's follow-through acts) oldest
    → newest — the store's chain, nothing invented. One reading
    per covenant kind the current letter carries afresh whose
    nearest dated prior covenant of that kind has lapsed by
    ``today``."""
    fresh = {o.kind for o in current_obligations if o.kind in ALL_COVENANT_KINDS}
    readings: list[ExhibitCheck] = []
    for kind in ALL_COVENANT_KINDS:
        if kind not in fresh:
            continue
        label = _LABELS[kind]
        prior_found = None
        for contract_id, obligations, acts in reversed(prior_rounds):
            covenant = next(
                (o for o in obligations if o.kind == kind and o.date),
                None,
            )
            if covenant is not None:
                prior_found = (contract_id, covenant, acts)
                break
        if prior_found is None:
            continue
        prior_id, covenant, acts = prior_found
        try:
            expiry = date.fromisoformat(covenant.date)
        except ValueError:
            continue
        if expiry > today:
            continue  # still running — a continuation, not a lapse
        state = follow_through_state(acts, kind, lapsed=True)
        if state == "restriction_confirmed_lifted":
            act = next(
                a for a in acts
                if a.get("kind") == kind
                and a.get("state") == "restriction_confirmed_lifted"
            )
            evidence = (
                f"its lapse stands confirmed lifted on the record "
                f"(by {act.get('actor', '')}, "
                f"{(act.get('at', '') or '')[:10]})"
            )
        elif state == "counterparty_notified":
            evidence = (
                "the counterparty was notified of its lapse, but "
                "the lapse is not confirmed on the record"
            )
        else:
            evidence = (
                "its lapse stands open on the record — announced, "
                "never followed through"
            )
        readings.append(
            ExhibitCheck(
                name="prior_covenant_lapsed",
                expectation=(
                    f"the {label} covenant in {prior_id} ran to "
                    f"{covenant.date}"
                ),
                promised=(
                    f"{label} covenant as at {prior_id}: expired "
                    f"{covenant.date}"
                ),
                found=f"lapse state on the record: {state}",
                status="consistent",
                detail=(
                    f"The {label} covenant in {prior_id} expired "
                    f"on {covenant.date} ({covenant.detail}); "
                    f"{evidence}. The {label} covenant this letter "
                    "carries is therefore read as a NEW bargain — "
                    "a fresh promise, priced on its own terms — "
                    "not a continuation of the restriction that "
                    "lapsed."
                ),
            )
        )
    return readings
