"""Obligations register: the dated, recurring duties a contract creates.

Verdicts answer "does this clause match our playbook?" — the register
answers the operational question that follows: *what must happen, by
when, under this contract if it is signed?* Renewal dates, notice
periods, payment terms, breach-notification windows, reporting
duties, confidentiality tails.

Everything here is code, built from the extraction signals (already
regex-extracted typed facts) plus a small set of clause-text patterns
for the values signals do not carry (initial-term length, renewal
term length, explicit calendar dates). Counts are validated as
positive integers — a garbled signal yields no entry, never a wrong
one. A register ``date`` is either quoted from the clause (an explicit
renewal date) or derived by date arithmetic in code — renewal date
minus the notice period is the deadline to give notice — and the
``computed`` flag says which, so counsel never mistakes a derived
deadline for a quoted one.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from .schemas import ClauseExtraction, ClauseUnit, Obligation

_MONTHS = {
    name: idx
    for idx, name in enumerate(
        [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ],
        start=1,
    )
}

_ISO_DATE = re.compile(r"\b(20\d{2})-(0[1-9]|1[0-2])-([0-3]\d)\b")
_LONG_DATE = re.compile(
    r"\b(january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+(\d{1,2}),\s*(20\d{2})\b"
)
_TERM_LENGTH = re.compile(r"\(?(\d+)\)?\s*(months?|years?)\b")


def _explicit_date(text: str) -> date | None:
    """First explicit calendar date in the clause text, if any."""
    m = _ISO_DATE.search(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _LONG_DATE.search(text.lower())
    if m:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))
        except ValueError:
            return None
    return None


def _positive(value) -> int | None:
    """A signal count is an obligation value only when it is a real,
    positive integer — validation, not trust."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _length_phrase(text: str, start: int = 0) -> str:
    m = _TERM_LENGTH.search(text, start)
    if not m:
        return ""
    n, unit = int(m.group(1)), m.group(2)
    return f"{n} {unit}"


def due_obligations(
    obligations: list[Obligation], today: date, window_days: int
) -> list[dict]:
    """The dated register entries that are due — overdue first, then
    by date — within ``window_days`` of ``today``.

    Only entries carrying a ``date`` can be due; undated duties (net
    30, breach notice within 72 hours) are obligations of performance,
    not of the calendar, and stay out of this view. Overdue entries
    are included however old they are: a missed notice deadline is
    the most important row in the list, not one to age out.
    """
    due: list[dict] = []
    for obligation in obligations:
        if not obligation.date:
            continue
        try:
            obligation_date = date.fromisoformat(obligation.date)
        except ValueError:
            continue
        days_until = (obligation_date - today).days
        if days_until > window_days:
            continue
        row = obligation.model_dump()
        row["days_until"] = days_until
        row["overdue"] = days_until < 0
        due.append(row)
    due.sort(key=lambda row: (row["date"], row["kind"], row["source_clause_id"]))
    return due


def build_obligations(
    units: list[ClauseUnit], extractions: list[ClauseExtraction]
) -> list[Obligation]:
    """Build the register from clause units + their extractions, in
    document order. Deterministic; a clause with no dated/recurring
    duty contributes nothing."""
    by_id = {unit.clause_id: unit for unit in units}
    register: list[Obligation] = []
    seen: set[tuple[str, str, str]] = set()

    def add(obligation: Obligation) -> None:
        key = (obligation.kind, obligation.source_clause_id, obligation.detail)
        if key not in seen:
            seen.add(key)
            register.append(obligation)

    for extraction in extractions:
        unit = by_id.get(extraction.clause_id)
        if unit is None:
            continue
        text = unit.text or ""
        lower = text.lower()
        signals = extraction.signals or {}
        ref = {"source_clause_id": unit.clause_id, "source_ref": unit.ref}

        if extraction.clause_type == "term_termination":
            notice_days = _positive(signals.get("notice_days"))
            renewal_date = _explicit_date(text) if "renew" in lower else None

            m = re.search(r"initial term of[^.]{0,40}", lower)
            if m:
                length = _length_phrase(lower, m.start())
                if length:
                    add(Obligation(
                        kind="term",
                        description=f"The initial term runs {length} from the effective date.",
                        detail=f"initial term: {length}",
                        **ref,
                    ))

            if signals.get("auto_renewal"):
                length = ""
                idx = lower.find("successive")
                if idx >= 0:
                    length = _length_phrase(lower, idx)
                add(Obligation(
                    kind="renewal",
                    description=(
                        "The agreement auto-renews"
                        + (f" for successive {length} terms" if length else "")
                        + " unless notice of non-renewal is given."
                    ),
                    detail=f"successive terms: {length}" if length else "auto-renewal",
                    date=renewal_date.isoformat() if renewal_date else None,
                    **ref,
                ))

            if notice_days:
                deadline = None
                computed = False
                if renewal_date is not None:
                    deadline = (renewal_date - timedelta(days=notice_days)).isoformat()
                    computed = True
                convenience = bool(signals.get("termination_for_convenience"))
                add(Obligation(
                    kind="notice_period",
                    description=(
                        f"Written notice of {notice_days} days is required"
                        + (" to terminate for convenience" if convenience else " to terminate or not renew")
                        + (
                            f"; against the renewal date on record the notice "
                            f"deadline computes to {deadline}."
                            if deadline
                            else "."
                        )
                    ),
                    detail=f"{notice_days} days' written notice",
                    days=notice_days,
                    date=deadline,
                    computed=computed,
                    **ref,
                ))

        elif extraction.clause_type == "payment_terms":
            net_days = _positive(signals.get("net_days"))
            if net_days:
                add(Obligation(
                    kind="payment",
                    description=f"Undisputed invoices are payable net {net_days} days from the invoice date.",
                    detail=f"Net {net_days}",
                    days=net_days,
                    **ref,
                ))
            if signals.get("prepay_full"):
                add(Obligation(
                    kind="payment",
                    description="Fees are payable in full in advance.",
                    detail="full prepayment",
                    **ref,
                ))
            late = signals.get("late_fee_pct_monthly")
            if isinstance(late, (int, float)) and late > 0:
                add(Obligation(
                    kind="payment",
                    description=f"Late amounts bear interest at {late}% per month.",
                    detail=f"late interest {late}%/month",
                    **ref,
                ))

        elif extraction.clause_type == "data_protection":
            hours = _positive(signals.get("breach_notice_hours"))
            if hours:
                add(Obligation(
                    kind="breach_notice",
                    description=(
                        f"A party must notify the other of a personal data "
                        f"breach within {hours} hours of discovery."
                    ),
                    detail=f"breach notice within {hours} hours",
                    hours=hours,
                    **ref,
                ))
            m = re.search(r"\b(annual|quarterly|monthly)\s+(security\s+)?reports?\b", lower)
            if m:
                add(Obligation(
                    kind="reporting",
                    description=f"Recurring reporting duty: {m.group(0)}.",
                    detail=m.group(0),
                    **ref,
                ))

        elif extraction.clause_type == "confidentiality":
            years = _positive(signals.get("duration_years"))
            if years:
                add(Obligation(
                    kind="confidentiality_term",
                    description=(
                        f"Confidentiality obligations survive {years} "
                        f"year{'s' if years != 1 else ''} following disclosure."
                    ),
                    detail=f"{years} years following disclosure",
                    **ref,
                ))
            elif signals.get("perpetual"):
                add(Obligation(
                    kind="confidentiality_term",
                    description="Confidentiality obligations are perpetual.",
                    detail="perpetual",
                    **ref,
                ))

    return register
