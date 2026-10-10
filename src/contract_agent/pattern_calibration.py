"""Pattern-term calibration: the score term, audited against the
outcomes it was meant to move.

The reflection-pattern term (``options.py``, ``PATTERN_TERM_CAP``)
is a stated guess: six points, the same size as the history term,
chosen so it can order close calls without ever outweighing the
playbook-driven score. A guess that is never measured becomes a
fact nobody chose. This module measures it, from the stored
records, along the three questions counsel would ask:

- **Applied** — on how many scored options did the term actually
  land, per clause type? The term only exists where a chain's
  reflection pattern put it; an application is one stored option
  whose breakdown carries it.
- **Decisive** — of those, on how many did it CHANGE the outcome:
  the option it rode on is the packet's recommendation, and
  without the term it would not have been (its score minus the
  term falls below the best competing option). A term that is
  applied often and decisive never is decoration.
- **Followed** — where the record was decided, what counsel did
  with the escalation the term argued for: the clause's tier-2
  (walk-away line) edits were settled (accepted or modified —
  counsel sent the line), rejected (counsel set the line aside
  at their own desk), or never marked.

A clause type whose term has been applied at least
``MIN_APPLICATIONS`` times and decided nothing is flagged a
**calibration candidate** — for counsel. The report proposes; it
changes nothing. The cap moves only when a human moves it, with
this evidence on the table — the same discipline the playbook
proposals follow, applied to the scorer itself.
"""

from __future__ import annotations

from .options import PATTERN_TERM_CAP
from .store import ApprovalRecord

# "Never decided" needs enough applications to mean anything: a
# term applied once and not decisive is a small sample, not a
# calibration fact.
MIN_APPLICATIONS = 2


def _follow_through(result, clause_id: str) -> str:
    """What counsel did with one decided packet's walk-away line
    for a clause: the tier-2 edits carrying the line, read against
    their recorded outcomes. ``unmarked`` when the packet was
    decided without per-edit outcomes — the gate decision alone
    does not say what happened to this line."""
    edits = [
        index
        for index, edit in enumerate(result.proposed_edits)
        if edit.clause_id == clause_id and edit.tier == 2
    ]
    if not edits:
        return "unmarked"
    outcomes = {
        outcome.edit_index: outcome.outcome
        for outcome in result.edit_outcomes
        if outcome.edit_index in edits
    }
    if not outcomes:
        return "unmarked"
    if any(outcome == "rejected" for outcome in outcomes.values()):
        return "rejected_by_counsel"
    return "sent_by_counsel"


def pattern_calibration(records: list[ApprovalRecord]) -> dict:
    """The calibration report over the stored records, per clause
    type the pattern term has ever touched."""
    per_type: dict[str, dict] = {}
    for record in records:
        result = record.result
        if not result.options:
            continue
        types = {v.clause_id: v.clause_type for v in result.clause_verdicts}
        for option in result.options:
            term = (option.breakdown or {}).get("pattern", 0.0)
            if not term:
                continue
            clause_type = types.get(option.clause_id, "other")
            slot = per_type.setdefault(
                clause_type,
                {
                    "clause_type": clause_type,
                    "applications": 0,
                    "contracts": set(),
                    "decisive": 0,
                    "decided_applications": 0,
                    "sent_by_counsel": 0,
                    "rejected_by_counsel": 0,
                    "unmarked": 0,
                },
            )
            slot["applications"] += 1
            slot["contracts"].add(result.contract_id)
            others = [o.score for o in result.options if o is not option]
            if (
                option.recommended
                and others
                and option.score - term < max(others)
            ):
                slot["decisive"] += 1
            if result.decision in ("approved", "rejected"):
                slot["decided_applications"] += 1
                slot[_follow_through(result, option.clause_id)] += 1
    rows: list[dict] = []
    candidates: list[str] = []
    for slot in per_type.values():
        candidate = (
            slot["applications"] >= MIN_APPLICATIONS and slot["decisive"] == 0
        )
        if candidate:
            candidates.append(slot["clause_type"])
        rows.append(
            {
                "clause_type": slot["clause_type"],
                "applications": slot["applications"],
                "contracts": len(slot["contracts"]),
                "decisive": slot["decisive"],
                "applied_not_decisive": slot["applications"] - slot["decisive"],
                "decided_applications": slot["decided_applications"],
                "sent_by_counsel": slot["sent_by_counsel"],
                "rejected_by_counsel": slot["rejected_by_counsel"],
                "unmarked": slot["unmarked"],
                "calibration_candidate": candidate,
                "detail": (
                    f"The pattern term has been applied to "
                    f"{slot['applications']} scored option(s) for "
                    f"{slot['clause_type']} and has never changed "
                    "which option the packet recommended — counsel "
                    "may want to review whether the term earns its "
                    f"{PATTERN_TERM_CAP:.0f} points for this type. "
                    "This report proposes; it changes nothing."
                    if candidate
                    else f"The pattern term decided "
                    f"{slot['decisive']} of its {slot['applications']} "
                    f"application(s) for {slot['clause_type']}."
                    if slot["decisive"]
                    else f"The pattern term has been applied "
                    f"{slot['applications']} time(s) for "
                    f"{slot['clause_type']} — below the "
                    f"{MIN_APPLICATIONS}-application threshold for a "
                    "calibration judgement."
                ),
            }
        )
    rows.sort(key=lambda row: (-row["applications"], row["clause_type"]))
    return {
        "term_cap": PATTERN_TERM_CAP,
        "min_applications": MIN_APPLICATIONS,
        "applications": sum(row["applications"] for row in rows),
        "decisive": sum(row["decisive"] for row in rows),
        "clause_types": rows,
        "calibration_candidates": sorted(candidates),
    }
