"""Clause diffs: counterparty text vs playbook fallback, side by side.

A verdict row tells counsel *that* a clause misses the playbook
position; the diff shows the two texts the negotiation is actually
about — the counterparty's own words and the fallback language the
playbook proposes — paired clause by clause. Counsel reads the pair,
not a summary of it.

Assembled deterministically from the segmented clause units and the
computed verdicts: the counterparty side is the clause text as
received (never sanitised, never rewritten — this is the record, not
a prompt), and the fallback side is the playbook's stored wording.
Only flagged clauses appear (any position other than ``preferred``);
a flagged clause whose playbook entry proposes no fallback says so —
counsel drafts that one.
"""

from __future__ import annotations

from .schemas import ClauseDiff, ClauseUnit, ClauseVerdict

FLAGGED_POSITIONS = {"acceptable_fallback", "review_required", "walk_away"}


def build_clause_diffs(
    units: list[ClauseUnit], verdicts: list[ClauseVerdict]
) -> list[ClauseDiff]:
    """Pair each flagged clause's counterparty text with its playbook
    fallback, in document order (verdicts are built in clause order)."""
    by_id = {unit.clause_id: unit for unit in units}
    diffs: list[ClauseDiff] = []
    for verdict in verdicts:
        if verdict.position not in FLAGGED_POSITIONS:
            continue
        unit = by_id.get(verdict.clause_id)
        diffs.append(
            ClauseDiff(
                clause_id=verdict.clause_id,
                ref=unit.ref if unit else "",
                heading=unit.heading if unit else "",
                clause_type=verdict.clause_type,
                position=verdict.position,
                counterparty_text=(unit.text if unit else "").strip(),
                playbook_fallback=verdict.fallback_language or "",
                playbook_refs=list(verdict.playbook_refs),
                rationale=verdict.rationale,
            )
        )
    return diffs
