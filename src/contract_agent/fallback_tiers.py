"""Fallback tiers: which concession the packet proposes, and why.

A playbook position is not a single fallback. For every clause type
with fallback language there are two tiers:

- **Tier 1 — the first fallback**: the fallback language the verdict
  already carries. This is the opening concession, proposed in round
  one.
- **Tier 2 — the walk-away line**: the final position, stored on the
  clause type's playbook entry as ``walk_away_line`` ("Final
  position on payment: net sixty (60) days is the outer limit…").
  Beyond this line the playbook's answer is to walk away.

Which tier the packet proposes is decided here, in code, from the
negotiation's state — never by the model:

- a clause already AT a walk-away verdict proposes tier 2 directly:
  there is no softer concession to open with;
- from round two onward (the contract supersedes an earlier round),
  flagged clauses propose tier 2: the first fallback was already on
  the table and did not close the clause;
- a clause type the chain reports as RE-TRADED also proposes tier 2,
  whatever the round: ground that was won and then given back is not
  re-opened with the soft concession;
- a clause type under a REFLECTION PATTERN proposes tier 2 ahead of
  every other rule (``reflection_patterns.py``): counsel agreed an
  edit on it, the follow-up draft did not show it, counsel agreed
  again, and that draft did not show it either — the soft concession
  was agreed twice and honoured neither time;
- everything else proposes tier 1.

The proposal lands on the clause diff (``ClauseDiff.proposed_tier``
and friends) — the packet and the console show counsel not just the
two texts but the tier being proposed and the reason. A clause type
whose playbook carries no walk-away line keeps tier 1 in every
round, and the reason says so.
"""

from __future__ import annotations

from .playbook_data import PLAYBOOK

TIER_NAMES = {1: "first fallback", 2: "walk-away line"}

_WALK_AWAY_BY_TYPE: dict[str, tuple[str, str]] = {}
for _entry in PLAYBOOK:
    if _entry.get("walk_away_line"):
        _WALK_AWAY_BY_TYPE.setdefault(
            _entry["clause_type"], (_entry["id"], _entry["walk_away_line"])
        )


def walk_away_line(clause_type: str) -> tuple[str, str] | None:
    """(entry_id, line) of the clause type's walk-away line, if the
    playbook carries one."""
    return _WALK_AWAY_BY_TYPE.get(clause_type)


def propose_tier(
    clause_type: str,
    position: str,
    fallback_language: str,
    round_number: int = 1,
    re_traded: bool = False,
    reflection_escalated: bool = False,
) -> tuple[int, str, str, str]:
    """The tier proposal for one flagged clause:
    ``(tier, tier_name, tier_language, tier_reason)``.

    Tier 0 (no proposal) is returned for ``preferred`` clauses —
    nothing needs conceding. ``fallback_language`` is the verdict's
    own fallback wording (tier 1's text)."""
    if position == "preferred":
        return 0, "", "", ""
    line = walk_away_line(clause_type)
    if position == "walk_away":
        if line:
            return (
                2,
                TIER_NAMES[2],
                line[1],
                "the clause sits at the playbook's walk-away position — "
                "the proposal is the final line, not an opening fallback",
            )
        return (
            1,
            TIER_NAMES[1],
            fallback_language,
            "the clause sits at a walk-away position and the playbook "
            "carries no walk-away line for this clause type — counsel "
            "drafts the final line",
        )
    if reflection_escalated:
        pattern_reason = (
            "a reflection pattern: an agreed edit on this clause was "
            "flagged agreed-but-not-reflected in two consecutive "
            "follow-ups — the soft concession was agreed twice and "
            "honoured neither time, so the packet proposes the final line"
        )
        if line:
            return 2, TIER_NAMES[2], line[1], pattern_reason
        return (
            1,
            TIER_NAMES[1],
            fallback_language,
            pattern_reason
            + "; the playbook carries no walk-away line for this clause "
            "type, so the first fallback stands and counsel drafts "
            "anything harder",
        )
    escalated_reason = None
    if re_traded:
        escalated_reason = (
            "this clause was re-traded earlier in the chain (won, then "
            "given back) — the packet proposes the final line, not the "
            "opening concession again"
        )
    elif round_number >= 2:
        escalated_reason = (
            f"round {round_number}: the first fallback was already on "
            "the table and did not close this clause — the packet "
            "proposes the final line"
        )
    if escalated_reason is not None:
        if line:
            return 2, TIER_NAMES[2], line[1], escalated_reason
        return (
            1,
            TIER_NAMES[1],
            fallback_language,
            escalated_reason
            + "; the playbook carries no walk-away line for this clause "
            "type, so the first fallback stands and counsel drafts "
            "anything harder",
        )
    return (
        1,
        TIER_NAMES[1],
        fallback_language,
        "round one: open with the playbook's first fallback",
    )
