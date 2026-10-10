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

**Acted on** (round 11): counsel can close the loop the way the
proposals close theirs — suspend the term for a flagged type
(``PatternTermSuspension``: actor + reason, reversible by a
recorded lift). While a suspension is active the scorer does not
apply the term to that type, and this report reads the type as
**suspended** — naming who set it aside, when, and why — instead
of re-flagging it as a candidate every run. The historical facts
(applications, decisive count) are reported as they stand: a
suspension stops future applications, it does not rewrite the
record the decision was taken on.

**Shelf life** (round 12): a suspension may carry a review-by
date — a set-aside taken on one chain's evidence should be
revisited deliberately, not inherited forever. The report reads
each dated suspension against today: ``scheduled`` (review still
ahead), ``due`` (inside the review window), or ``overdue`` (the
date passed with no re-confirmation — the set-aside is stale,
and the report says so on the row and in the headline list
``suspensions_due_review``, which the queue surface carries
too). An overdue suspension still binds the scorer: staleness
is counsel's prompt to act, not the agent quietly restoring a
term counsel set aside.
"""

from __future__ import annotations

from datetime import date

from .options import PATTERN_TERM_CAP
from .store import ApprovalRecord

# "Never decided" needs enough applications to mean anything: a
# term applied once and not decisive is a small sample, not a
# calibration fact.
MIN_APPLICATIONS = 2

# A suspension's review is "due" once its date is this close —
# the same order of warning the certificate watch gives an
# expiry: close enough that the next working session should
# see it.
REVIEW_WINDOW_DAYS = 30


def suspension_review_state(suspension, today: date) -> dict | None:
    """One suspension's review position against ``today``, or
    None when it carries no review date (an undated suspension
    makes no dated promise) or is not active (a lifted one has
    nothing left to review)."""
    if suspension.status != "active" or not suspension.review_by:
        return None
    try:
        review_by = date.fromisoformat(suspension.review_by)
    except ValueError:
        return None
    days_until = (review_by - today).days
    state = (
        "overdue"
        if days_until < 0
        else "due"
        if days_until <= REVIEW_WINDOW_DAYS
        else "scheduled"
    )
    return {
        "clause_type": suspension.clause_type,
        "review_by": suspension.review_by,
        "days_until_review": days_until,
        "review_state": state,
        "suspended_by": suspension.suspended_by,
        "reason": suspension.reason,
    }


def suspension_review_alerts(suspensions, today: date) -> list[dict]:
    """The suspensions whose review is due or overdue, soonest
    first — the headline list the report and the queue surface
    share, so the two never disagree about what is stale."""
    alerts = [
        alert
        for suspension in suspensions
        if (alert := suspension_review_state(suspension, today)) is not None
        and alert["review_state"] in ("due", "overdue")
    ]
    alerts.sort(key=lambda a: (a["review_by"], a["clause_type"]))
    return alerts


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


def _review_sentence(review: dict | None) -> str:
    """The review position as a sentence on a suspended row."""
    if review is None:
        return ""
    if review["review_state"] == "overdue":
        return (
            f" Its review date ({review['review_by']}) has "
            "passed — the set-aside is stale until counsel "
            "re-confirms or lifts it."
        )
    if review["review_state"] == "due":
        return (
            f" Review falls due on {review['review_by']} "
            f"(in {review['days_until_review']} day(s)) — "
            "counsel re-confirms or lifts it."
        )
    return f" Review falls due on {review['review_by']}."


def _suspension_block(suspension, today: date) -> dict:
    """A suspension as the report carries it: the act, and its
    review position when it carries a review date."""
    block = {
        "suspended_by": suspension.suspended_by,
        "suspended_at": suspension.suspended_at,
        "reason": suspension.reason,
    }
    review = suspension_review_state(suspension, today)
    if review is not None:
        block.update(
            {
                "review_by": review["review_by"],
                "review_state": review["review_state"],
                "days_until_review": review["days_until_review"],
            }
        )
    return block


def pattern_calibration(
    records: list[ApprovalRecord], suspensions=(), today: date | None = None
) -> dict:
    """The calibration report over the stored records, per clause
    type the pattern term has ever touched. ``suspensions`` are the
    calibration store's records (``PatternTermSuspension``): an
    actively suspended type is read as suspended — never as a
    candidate — and a type counsel suspended before the term ever
    landed still gets a row, so the set-aside is visible. A dated
    suspension is read against ``today`` (default: today) — due
    and overdue reviews are named on the row and collected in
    ``suspensions_due_review``."""
    today = today or date.today()
    active = {
        s.clause_type: s for s in suspensions if s.status == "active"
    }
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
        suspension = active.get(slot["clause_type"])
        candidate = (
            slot["applications"] >= MIN_APPLICATIONS
            and slot["decisive"] == 0
            and suspension is None
        )
        if candidate:
            candidates.append(slot["clause_type"])
        if suspension is not None:
            detail = (
                f"The pattern term for {slot['clause_type']} is "
                f"SUSPENDED — set aside by {suspension.suspended_by} "
                f"on {suspension.suspended_at} ({suspension.reason}). "
                "The scorer no longer applies it to this type; the "
                f"record it was suspended on stands at "
                f"{slot['applications']} application(s), "
                f"{slot['decisive']} decisive. Counsel can lift the "
                "suspension to restore the term."
            ) + _review_sentence(suspension_review_state(suspension, today))
        elif candidate:
            detail = (
                f"The pattern term has been applied to "
                f"{slot['applications']} scored option(s) for "
                f"{slot['clause_type']} and has never changed "
                "which option the packet recommended — counsel "
                "may want to review whether the term earns its "
                f"{PATTERN_TERM_CAP:.0f} points for this type. "
                "This report proposes; it changes nothing."
            )
        elif slot["decisive"]:
            detail = (
                f"The pattern term decided "
                f"{slot['decisive']} of its {slot['applications']} "
                f"application(s) for {slot['clause_type']}."
            )
        else:
            detail = (
                f"The pattern term has been applied "
                f"{slot['applications']} time(s) for "
                f"{slot['clause_type']} — below the "
                f"{MIN_APPLICATIONS}-application threshold for a "
                "calibration judgement."
            )
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
                "suspended": suspension is not None,
                "suspension": (
                    _suspension_block(suspension, today)
                    if suspension is not None
                    else None
                ),
                "detail": detail,
            }
        )
    # A type counsel suspended before the term ever landed has no
    # applications to build a row from — it still gets one, so the
    # set-aside is visible in the report that governs the term.
    for clause_type, suspension in active.items():
        if clause_type in per_type:
            continue
        rows.append(
            {
                "clause_type": clause_type,
                "applications": 0,
                "contracts": 0,
                "decisive": 0,
                "applied_not_decisive": 0,
                "decided_applications": 0,
                "sent_by_counsel": 0,
                "rejected_by_counsel": 0,
                "unmarked": 0,
                "calibration_candidate": False,
                "suspended": True,
                "suspension": _suspension_block(suspension, today),
                "detail": (
                    f"The pattern term for {clause_type} is "
                    f"SUSPENDED — set aside by "
                    f"{suspension.suspended_by} on "
                    f"{suspension.suspended_at} ({suspension.reason}). "
                    "The term has not been applied to any scored "
                    "option for this type; while the suspension "
                    "stands, the scorer will not apply it."
                ) + _review_sentence(suspension_review_state(suspension, today)),
            }
        )
    rows.sort(key=lambda row: (-row["applications"], row["clause_type"]))
    alerts = suspension_review_alerts(suspensions, today)
    return {
        "term_cap": PATTERN_TERM_CAP,
        "min_applications": MIN_APPLICATIONS,
        "applications": sum(row["applications"] for row in rows),
        "decisive": sum(row["decisive"] for row in rows),
        "clause_types": rows,
        "calibration_candidates": sorted(candidates),
        "suspended_types": sorted(active),
        "suspensions_due_review": [a["clause_type"] for a in alerts],
        "suspension_review_alerts": alerts,
    }
