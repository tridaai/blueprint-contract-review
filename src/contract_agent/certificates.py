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

**Closing the watch takes evidence** (round 12). A renewal
certificate arriving as a later round supersedes the earlier one
for the watch — but only when the renewal check
(``certificate_renewal.py``) evidences the ORIGINAL requirement
set in full. A renewal that quietly dropped a limit or an
endorsement fails that check, and the watch does not close on
its arrival: it stays open on the last relied-upon expiry, with
the failed renewal named on the row. Arrival is not evidence;
the check is.

**The watch has a follow-through** (round 17): the covenant
lapse got its closing acts in round 16, and the watch is the
same shape of open loop — it announces, and until now nothing
recorded that anyone acted. A named act on the watch's record
(``CertificateWatchFollowThrough``, on the approval record the
watch stands on) records ``renewal_chased`` — counsel chased
the renewal — or ``risk_accepted`` — counsel recorded, with a
note, that the lapsing cover is an accepted risk.
:func:`watch_follow_through_state` reads the acts into the one
state the watch surfaces. The acts close the question of
whether anyone acted; they do not close the watch — only
evidenced renewal does that.
"""

from __future__ import annotations

from datetime import date

from .config import env_int
from .store import ApprovalRecord

WATCH_WINDOW_ENV = "CERTIFICATE_EXPIRY_WATCH_DAYS"
DEFAULT_WATCH_WINDOW_DAYS = 90

# The follow-through states a watch can be in, in the order
# the loop closes (round 17): the renewal is chased, or the
# risk of the lapsing cover is accepted on the record. An act
# is recorded under one of these names; the watch's reading is
# the furthest state its acts have reached, or ``open`` with
# no acts — the watch announced and nobody has acted yet.
CERTIFICATE_WATCH_FOLLOW_THROUGH_STATES = (
    "renewal_chased",
    "risk_accepted",
)


def certificate_watch_window_days() -> int:
    """The configured watch window, in days."""
    return env_int(WATCH_WINDOW_ENV, DEFAULT_WATCH_WINDOW_DAYS)


def watch_follow_through_state(acts: list[dict]) -> str:
    """One watch's follow-through state, read from the acts
    recorded against it: the furthest closing state reached,
    or ``open`` with no acts. The state names what is true of
    the record, so a watch nobody acted on is never read as
    handled, and a chased renewal is never read as accepted
    risk — the two acts answer different questions."""
    states = [a.get("state", "") for a in acts]
    if "risk_accepted" in states:
        return "risk_accepted"
    if "renewal_chased" in states:
        return "renewal_chased"
    return "open"


def _relied_upon_expiry(record: ApprovalRecord):
    """The expiry a record's review relied on: its consistent
    ``insurance_certificate_expiry`` check's recorded date, or
    None when the record's certificate was not relied upon."""
    for check in record.result.exhibit_checks:
        if check.name != "insurance_certificate_expiry":
            continue
        if check.status != "consistent":
            return None
        try:
            return date.fromisoformat(check.found or ""), check
        except ValueError:
            return None
    return None


def certificate_watches(
    records: list[ApprovalRecord], today: date, window_days: int
) -> list[dict]:
    """Relied-upon certificates expiring within ``window_days`` of
    ``today``, soonest first. Pure over the stored records: the
    expiry is the one the stored expiry check recorded in its
    ``found`` field — an unparseable value yields no watch, never
    a guessed date.

    Per chain, the operative certificate is the latest round's —
    unless that round's renewal failed the evidence check
    (``certificate_renewal.py``): then the renewal evidenced
    nothing, and the watch stays open on the most recent earlier
    round whose certificate WAS relied on, with the failed
    renewal named on the row (``renewal_evidence_failed``). A
    passing renewal marks its row ``renewed`` — the watch moved
    to the new expiry on evidence, not on arrival."""
    from .concessions import _chains

    rows: list[dict] = []
    for chain in _chains(records):
        latest = chain[-1]
        renewal = next(
            (
                c
                for c in latest.result.certificate_renewal
                if c.name == "insurance_certificate_renewal"
            ),
            None,
        )
        source = latest
        renewal_failed = False
        if renewal is not None and renewal.status != "consistent":
            for earlier in reversed(chain[:-1]):
                if _relied_upon_expiry(earlier) is not None:
                    source = earlier
                    renewal_failed = True
                    break
        relied = _relied_upon_expiry(source)
        if relied is None:
            continue  # not relied upon — already a finding, not a watch
        expiry, check = relied
        result = source.result
        days_until = (expiry - today).days
        if days_until > window_days:
            continue
        row = {
            "contract_id": result.contract_id,
            "counterparty_name": result.counterparty_name,
            "status": result.status,
            "expiry_date": expiry.isoformat(),
            "days_until": days_until,
            "overdue": days_until < 0,
            "exhibit_document_id": check.exhibit_document_id,
            "renewed": renewal is not None and renewal.status == "consistent",
        }
        if renewal_failed:
            row["renewal_evidence_failed"] = True
            row["renewal_contract_id"] = latest.result.contract_id
            row["renewal_status"] = renewal.status
        rows.append(row)
    rows.sort(key=lambda row: (row["expiry_date"], row["contract_id"]))
    return rows
