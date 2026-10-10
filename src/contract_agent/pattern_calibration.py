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

**Serial renewals** (round 15): re-confirmation (round 13) made
renewing a set-aside one quiet act — and a quiet act can repeat
forever, one review at a time, until a "temporary" set-aside is
permanent in everything but name. The report therefore reads
each suspension's renewals AS A SERIES: the count, the span
from the original act to the latest renewal, and the full
history ride on the row, and a suspension renewed at least
``SERIAL_RENEWAL_THRESHOLD`` times is named in the headline
list ``serially_renewed_types`` — a permanent set-aside
wearing a review date is a different fact from a fresh
suspension, and the report says which one counsel is looking
at. Naming changes nothing: the scorer honours the suspension
exactly as before.

**On the queue** (round 16): the report named the pattern, but
counsel works the queue — and the queue carried only the
due-review alerts, so a serially renewed set-aside was visible
everywhere except where the re-confirm act happens.
``serial_renewal_alerts`` is the queue's own reading of the
same series: each active suspension past the threshold, with
its count and span, as an alert list beside the due reviews —
the same attention, in the room where the next renewal would
be signed.
"""

from __future__ import annotations

from datetime import date, datetime

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

# Renewed this many times, a set-aside is no longer a decision
# being revisited — it is a standing arrangement being
# maintained, and the report names it as a serial renewal.
SERIAL_RENEWAL_THRESHOLD = 3


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


def serial_renewal_alerts(suspensions) -> list[dict]:
    """The queue's serial-renewal alerts (round 16): each ACTIVE
    suspension whose renewal series has crossed
    ``SERIAL_RENEWAL_THRESHOLD``, with the series attached —
    the count, the span from the original set-aside, and the
    latest renewal's act. A serially renewed set-aside is the
    same kind of attention as a due review, and it belongs in
    the same room: the queue, where counsel would sign the
    next renewal. A lifted suspension is history, not a
    standing pattern, and never alerts; the scorer is
    untouched either way — the alert weighs the pattern, it
    does not move the term."""
    alerts: list[dict] = []
    for suspension in suspensions:
        if suspension.status != "active":
            continue
        series = renewal_series(suspension)
        if series is None or not series["serially_renewed"]:
            continue
        last = suspension.reconfirmations[-1]
        alerts.append(
            {
                "clause_type": suspension.clause_type,
                "renewal_count": series["renewal_count"],
                "renewal_span_days": series["renewal_span_days"],
                "renewal_span_months": series["renewal_span_months"],
                "suspended_by": suspension.suspended_by,
                "suspended_at": suspension.suspended_at,
                "reason": suspension.reason,
                "last_reconfirmed_by": last.actor,
                "last_reconfirmed_at": last.at,
                "last_reconfirmation_note": last.note,
                "review_by": suspension.review_by or "",
            }
        )
    alerts.sort(key=lambda a: (-a["renewal_count"], a["clause_type"]))
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


def _stamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value or "")
    except ValueError:
        return None


def renewal_series(suspension) -> dict | None:
    """A suspension's renewals read AS A SERIES (round 15), or
    None when it has never been re-confirmed: the count, the
    span from the original set-aside to the latest renewal, the
    full history, and whether the series has crossed the
    serial threshold — a set-aside renewed that often is a
    standing decision wearing a review date, and the report
    names it as one instead of letting each quiet renewal pass
    as a fresh look."""
    reconfirmations = getattr(suspension, "reconfirmations", None) or []
    if not reconfirmations:
        return None
    first = _stamp(suspension.suspended_at)
    last = _stamp(reconfirmations[-1].at)
    span_days = (
        max(0, (last - first).days)
        if first is not None and last is not None
        else None
    )
    return {
        "renewal_count": len(reconfirmations),
        "renewal_span_days": span_days,
        "renewal_span_months": (
            round(span_days / 30.44, 1) if span_days is not None else None
        ),
        "serially_renewed": len(reconfirmations) >= SERIAL_RENEWAL_THRESHOLD,
        "renewal_history": [
            {
                "actor": act.actor,
                "at": act.at,
                "note": act.note,
                "review_by": act.review_by,
            }
            for act in reconfirmations
        ],
    }


def _span_phrase(span_days: int | None) -> str:
    if span_days is None:
        return ""
    if span_days >= 60:
        months = round(span_days / 30.44, 1)
        text = f"{months:.1f}".rstrip("0").rstrip(".")
        return f" across {text} months"
    if span_days >= 30:
        return " across 1 month"
    return f" across {span_days} day(s)"


def _reconfirmation_sentence(suspension) -> str:
    """The set-aside's renewals as a sentence on a suspended
    row: how often counsel has re-confirmed it, across what
    span, and the latest act — the continuity of the one
    suspension, in the open. A series past the serial threshold
    is named as what it is: a permanent set-aside wearing a
    review date."""
    series = renewal_series(suspension)
    if series is None:
        return ""
    last = suspension.reconfirmations[-1]
    sentence = (
        f" Re-confirmed {series['renewal_count']} time(s)"
        f"{_span_phrase(series['renewal_span_days'])}, most "
        f"recently by {last.actor} on {last.at} ({last.note})."
    )
    if series["serially_renewed"]:
        sentence += (
            " Renewed this often, the set-aside reads as a "
            "standing decision wearing a review date — a serial "
            "renewal, named here for counsel to weigh as one."
        )
    return sentence


def _suspension_block(suspension, today: date) -> dict:
    """A suspension as the report carries it: the act, its
    renewal series when it has been re-confirmed, and its
    review position when it carries a review date."""
    block = {
        "suspended_by": suspension.suspended_by,
        "suspended_at": suspension.suspended_at,
        "reason": suspension.reason,
    }
    reconfirmations = getattr(suspension, "reconfirmations", None) or []
    if reconfirmations:
        last = reconfirmations[-1]
        block.update(
            {
                "reconfirmations": len(reconfirmations),
                "last_reconfirmed_by": last.actor,
                "last_reconfirmed_at": last.at,
                "last_reconfirmation_note": last.note,
            }
        )
        series = renewal_series(suspension)
        if series is not None:
            block.update(
                {
                    "renewal_count": series["renewal_count"],
                    "renewal_span_days": series["renewal_span_days"],
                    "renewal_span_months": series["renewal_span_months"],
                    "serially_renewed": series["serially_renewed"],
                    "renewal_history": series["renewal_history"],
                }
            )
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
            ) + _reconfirmation_sentence(suspension) + _review_sentence(
                suspension_review_state(suspension, today)
            )
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
                ) + _reconfirmation_sentence(suspension) + _review_sentence(
                    suspension_review_state(suspension, today)
                ),
            }
        )
    rows.sort(key=lambda row: (-row["applications"], row["clause_type"]))
    alerts = suspension_review_alerts(suspensions, today)
    serially_renewed = sorted(
        clause_type
        for clause_type, suspension in active.items()
        if (series := renewal_series(suspension)) is not None
        and series["serially_renewed"]
    )
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
        "serially_renewed_types": serially_renewed,
    }
