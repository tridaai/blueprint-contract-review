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

Each diff also carries the fallback-tier proposal
(``fallback_tiers.py``): tier 1 (first fallback) in round one, tier
2 (the walk-away line) for walk-away verdicts, later rounds, and
re-traded clause types. ``round_number``/``re_traded_types`` come
from the caller — the graph knows whether this contract supersedes
an earlier round; the service refines both from the full chain when
it attaches the chain report.
"""

from __future__ import annotations

from .fallback_tiers import propose_tier
from .schemas import ClauseDiff, ClauseUnit, ClauseVerdict

FLAGGED_POSITIONS = {"acceptable_fallback", "review_required", "walk_away"}


def build_clause_diffs(
    units: list[ClauseUnit],
    verdicts: list[ClauseVerdict],
    round_number: int = 1,
    re_traded_types: frozenset[str] | set[str] = frozenset(),
    reflection_escalated_types: frozenset[str] | set[str] = frozenset(),
) -> list[ClauseDiff]:
    """Pair each flagged clause's counterparty text with its playbook
    fallback, in document order (verdicts are built in clause order).
    ``reflection_escalated_types`` are the clause types under a
    reflection pattern (``reflection_patterns.py``) — their tier
    proposals escalate to the walk-away line, and the diff is marked
    ``pattern_escalated`` so surfaces can badge the reason."""
    by_id = {unit.clause_id: unit for unit in units}
    diffs: list[ClauseDiff] = []
    for verdict in verdicts:
        if verdict.position not in FLAGGED_POSITIONS:
            continue
        unit = by_id.get(verdict.clause_id)
        pattern = verdict.clause_type in reflection_escalated_types
        tier, tier_name, tier_language, tier_reason = propose_tier(
            verdict.clause_type,
            verdict.position,
            verdict.fallback_language or "",
            round_number=round_number,
            re_traded=verdict.clause_type in re_traded_types,
            reflection_escalated=pattern,
        )
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
                proposed_tier=tier,
                tier_name=tier_name,
                tier_language=tier_language,
                tier_reason=tier_reason,
                pattern_escalated=pattern,
            )
        )
    return diffs
