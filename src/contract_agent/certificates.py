"""Certificate renewal watch: a certificate that passed review
still expires.

The exhibit checks (``exhibit_review.py``) answer the review-time
question — is the certificate of insurance consistent with the
clause that relies on it *now*? They cannot answer the operational
question that follows: the certificate was in force at review, the
agreement runs on, and the cover runs out mid-term. Nothing in the
review record notices the day approaching.

This module is the watch. A contract RELIES on a certificate when
its stored ``insurance_certificate_expiry`` check came back
``consistent`` — the cover was in force at review and counsel's
review proceeded on it. The watch reads those stored checks (one
per chain, from the chain's latest round: a renewal certificate
arriving as a later round supersedes the earlier one), parses the
expiry the check recorded, and flags every relied-upon certificate
expiring within a configured window (``CERTIFICATE_EXPIRY_WATCH_DAYS``,
default 90) of the sweep date — overdue included, like the
obligations register's due view, because an already-lapsed
certificate is the most important row, not one to age out.

Certificates whose checks did NOT pass (mismatch, missing) are not
watched: they are already findings on the record, and a watch that
re-announced them would bury the signal this one exists for — the
certificate nobody had a reason to doubt. The watch surfaces
through the deadline sweep (``ContractService.dispatch_obligation_sweep``)
as one ``certificate_expiring`` event per watch, and on its own
read surface (``ContractService.certificate_watches`` /
``GET /certificates/expiring``). Report-only, like the sweep: the
watch announces; it chases no one.
"""

from __future__ import annotations

from datetime import date

from .config import env_int
from .store import ApprovalRecord

WATCH_WINDOW_ENV = "CERTIFICATE_EXPIRY_WATCH_DAYS"
DEFAULT_WATCH_WINDOW_DAYS = 90


def certificate_watch_window_days() -> int:
    """The configured watch window, in days."""
    return env_int(WATCH_WINDOW_ENV, DEFAULT_WATCH_WINDOW_DAYS)


def _latest_rounds(records: list[ApprovalRecord]) -> list[ApprovalRecord]:
    """The latest round of every negotiation chain in the record
    set (plus every unchained record): the round whose certificate
    the contract currently relies on. Chains follow the
    ``supersedes`` links, as everywhere (``concessions._chains``)."""
    from .concessions import _chains

    return [chain[-1] for chain in _chains(records)]


def certificate_watches(
    records: list[ApprovalRecord], today: date, window_days: int
) -> list[dict]:
    """Relied-upon certificates expiring within ``window_days`` of
    ``today``, soonest first. Pure over the stored records: the
    expiry is the one the stored expiry check recorded in its
    ``found`` field — an unparseable value yields no watch, never
    a guessed date."""
    rows: list[dict] = []
    for record in _latest_rounds(records):
        result = record.result
        for check in result.exhibit_checks:
            if check.name != "insurance_certificate_expiry":
                continue
            if check.status != "consistent":
                continue  # not relied upon — already a finding, not a watch
            try:
                expiry = date.fromisoformat(check.found or "")
            except ValueError:
                continue
            days_until = (expiry - today).days
            if days_until > window_days:
                continue
            rows.append(
                {
                    "contract_id": result.contract_id,
                    "counterparty_name": result.counterparty_name,
                    "status": result.status,
                    "expiry_date": expiry.isoformat(),
                    "days_until": days_until,
                    "overdue": days_until < 0,
                    "exhibit_document_id": check.exhibit_document_id,
                }
            )
            break  # one expiry check per contract at most
    rows.sort(key=lambda row: (row["expiry_date"], row["contract_id"]))
    return rows
