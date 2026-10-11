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

**The chase ages** (round 18): a chase is a question asked of
the counterparty, and a question can go unanswered. A renewal
chased ``CERTIFICATE_WATCH_FOLLOW_THROUGH_STALE_DAYS`` (default
30) ago with the watch still standing — no evidenced renewal
has arrived, or the watch would have moved — is a chase that
went quiet, and :func:`follow_through_ageing` reads it as
**stale**: the stale-failure discipline applied to a human
wait. The queue surfaces the stale chases beside its other
attention lists. An accepted risk never goes stale — accepting
was the answer, not a question — and a watch nobody has acted
on is not a stale chase either: it is an open watch, which the
row already says.

**The announcements are follow-through-aware** (round 19):
the deadline sweep announced every watched certificate the
same way on every run — a watch nobody had acted on, a watch
whose renewal stood chased, and a watch counsel had accepted
the risk on all drew the identical ``certificate_expiring``
event, forever. The sweep now announces what is true: the
event carries the watch's follow-through state; a stale chase
draws its own announcement (``certificate_watch_stale_chase``,
:func:`stale_chase_due`) — one signed event per stale chase and
one signed webhook per sweep naming them all, the chase act
stamped (``stale_announced_at``) so it is announced once per
stale period, the outbox stale sweep's discipline — and a
watch whose risk counsel accepted stops announcing at all:
the decision closed the loop the announcement was chasing,
so the sweep counts it accepted instead, the way the covenant
sweep counts a confirmed lapse closed.

**The row carries the coverage, and drafts the ask**: a watch
row named a contract and a date, but not the cover at stake —
counsel reading the sweep still had to open the review record
to learn what limits lapse, and then write the renewal request
from scratch. Each row now carries the coverage facts AS
RECORDED on the review it stands on (the named insured the
certificate showed, each coverage limit the agreement required
beside what the certificate stated, the endorsements required
beside what was shown — read off the stored checks, never
recomputed), an explicit ``state`` — ``expired`` once the date
has passed, ``expiring`` while it approaches, so a lapsed
certificate is never lumped in with the merely upcoming — the
chain's root beside the operative round, and a deterministic
renewal-request draft composed from those same recorded facts:
the certificate, its expiry, the required coverage in the
agreement's own recorded terms. The draft is a draft — headed
as one, sent nowhere, and it names no fact the record does not
carry: a record whose review stored no limits gives the draft
no limits to quote.
"""

from __future__ import annotations

from datetime import date, datetime

from .config import env_int
from .store import ApprovalRecord

WATCH_WINDOW_ENV = "CERTIFICATE_EXPIRY_WATCH_DAYS"
DEFAULT_WATCH_WINDOW_DAYS = 90

FOLLOW_THROUGH_STALE_ENV = "CERTIFICATE_WATCH_FOLLOW_THROUGH_STALE_DAYS"
DEFAULT_FOLLOW_THROUGH_STALE_DAYS = 30

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


def certificate_watch_follow_through_stale_days() -> int:
    """How long a chased renewal may go unanswered before the
    chase itself reads as stale, in days."""
    return env_int(
        FOLLOW_THROUGH_STALE_ENV, DEFAULT_FOLLOW_THROUGH_STALE_DAYS
    )


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


def _act_date(value: str) -> date | None:
    """An act's ISO stamp as a date, or None when it does not
    parse — an unstamped act has no age, and no age means never
    stale: the reading fails closed, like the outbox's."""
    try:
        return datetime.fromisoformat(value or "").date()
    except ValueError:
        return None


def follow_through_ageing(
    acts: list[dict], today: date, threshold_days: int | None = None
) -> dict:
    """One watch's follow-through ageing (round 18): the state,
    the chase that was made (by whom, when), its age in days,
    and whether the chase has gone quiet — chased at least
    ``threshold_days`` ago with the state still
    ``renewal_chased``. The watch row carrying this reading is
    itself the proof no evidenced renewal arrived: a renewal
    that evidenced the requirement set would have moved the
    watch and superseded the record the acts stand on. An
    accepted risk is a decision, not a question, and never
    reads stale; an open watch has no chase to age."""
    threshold = (
        certificate_watch_follow_through_stale_days()
        if threshold_days is None
        else threshold_days
    )
    state = watch_follow_through_state(acts)
    chased = next(
        (a for a in acts if a.get("state") == "renewal_chased"), None
    )
    chased_on = _act_date(chased.get("at", "")) if chased else None
    age = (
        max(0, (today - chased_on).days) if chased_on is not None else None
    )
    return {
        "state": state,
        "chased_at": chased.get("at", "") if chased else "",
        "chased_by": chased.get("actor", "") if chased else "",
        "age_days": age,
        "stale_threshold_days": threshold,
        "stale": bool(
            state == "renewal_chased"
            and age is not None
            and age >= threshold
        ),
    }


def chase_act(acts: list[dict]) -> dict | None:
    """The ``renewal_chased`` act among a watch's follow-through
    acts, or None when the renewal was never chased. The chase
    act is the stale period's identity (round 19): it carries
    the announcement stamp, so a chase is announced once, and
    a chase recorded later — on the record a renewal moved the
    watch to — is a new period with its own announcement."""
    return next(
        (a for a in acts if a.get("state") == "renewal_chased"), None
    )


def stale_chase_due(row: dict) -> bool:
    """Whether a watch row's stale chase is due its announcement
    (round 19): the row's ageing reads the chase stale, and the
    chase act carries no ``stale_announced_at`` stamp yet. A
    stamped chase was announced for this stale period — the
    outbox stale sweep's dedupe, on the act whose period it is.
    Rows are the watch view's (``ContractService.
    certificate_watches``): follow-through acts and ageing
    attached."""
    ageing = row.get("follow_through_ageing") or {}
    if not ageing.get("stale"):
        return False
    act = chase_act(row.get("follow_through") or [])
    return act is not None and not act.get("stale_announced_at")


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


_ENDORSEMENT_CHECKS = (
    "insurance_additional_insured",
    "insurance_waiver_of_subrogation",
)


def coverage_facts(record: ApprovalRecord) -> dict:
    """The coverage facts a record's review recorded, read off
    its stored insurance checks: the named insured the
    certificate showed, each coverage limit — what the agreement
    required (the check's ``promised``) beside what the
    certificate stated (its ``found``) — and each endorsement
    the agreement required beside what the certificate showed.
    Facts as recorded, never recomputed and never completed: a
    check the review did not run contributes nothing, so the
    facts a watch row carries are exactly the facts its review
    left on the record."""
    facts: dict = {"named_insured": "", "limits": [], "endorsements": []}
    for check in record.result.exhibit_checks:
        if check.name == "insurance_named_insured":
            facts["named_insured"] = check.found or ""
        elif check.name.startswith("insurance_limits_"):
            facts["limits"].append(
                {
                    "coverage": check.name.removeprefix(
                        "insurance_limits_"
                    ).replace("_", " "),
                    "required": check.promised,
                    "found": check.found,
                    "status": check.status,
                }
            )
        elif check.name in _ENDORSEMENT_CHECKS:
            facts["endorsements"].append(
                {
                    "name": check.name.removeprefix("insurance_").replace(
                        "_", " "
                    ),
                    "required": check.promised,
                    "found": check.found,
                    "status": check.status,
                }
            )
    return facts


def renewal_request_draft(
    *,
    counterparty_name: str,
    contract_id: str,
    chain_root: str,
    exhibit_document_id: str,
    expiry: date,
    days_until: int,
    today: date,
    coverage: dict,
) -> str:
    """The renewal request a flagged certificate draws: a
    deterministic draft composed from the watch row's recorded
    facts — the certificate and the contract it stands under,
    the expiry and its distance, the coverage the agreement
    requires in the terms the review recorded. Headed as a
    draft and sent nowhere; the wording follows the state (a
    certificate still in force is asked for before it expires,
    a lapsed one is asked for as a matter of urgency), and no
    line names a fact the record does not carry — with no
    limits or endorsements on the record, the draft asks for
    renewal of the recorded cover and quotes none."""
    lines = [
        "DRAFT — renewal request, prepared for counsel to send; not sent.",
        "",
        f"To: {counterparty_name}",
        (
            f"Re: Certificate of insurance under contract {contract_id}"
            + (
                f" (chain root {chain_root})"
                if chain_root and chain_root != contract_id
                else ""
            )
            + (
                f" — certificate {exhibit_document_id}"
                if exhibit_document_id
                else ""
            )
        ),
        "",
    ]
    if days_until < 0:
        lines.append(
            f"The certificate of insurance relied on under this "
            f"contract expired on {expiry.isoformat()} "
            f"({-days_until} days before {today.isoformat()})."
        )
    elif days_until == 0:
        lines.append(
            f"The certificate of insurance relied on under this "
            f"contract expires today, {expiry.isoformat()}."
        )
    else:
        lines.append(
            f"The certificate of insurance relied on under this "
            f"contract expires on {expiry.isoformat()} "
            f"({days_until} days from {today.isoformat()})."
        )
    if coverage.get("named_insured"):
        lines.append(
            f"The certificate names {coverage['named_insured']} "
            f"as the insured."
        )
    required = [f"- {limit['required']}" for limit in coverage.get("limits", []) if limit.get("required")]
    required += [
        f"- {endorsement['required']}"
        for endorsement in coverage.get("endorsements", [])
        if endorsement.get("required")
    ]
    lines.append("")
    if required:
        lines.append(
            "Please furnish a renewal certificate evidencing the "
            "coverage the agreement requires:"
        )
        lines.extend(required)
    else:
        lines.append(
            "Please furnish a renewal certificate for the cover "
            "recorded on the review of this contract."
        )
    lines.append(
        "Please furnish it as a matter of urgency — the cover "
        "this contract relies on has lapsed."
        if days_until < 0
        else "Please furnish it before the current certificate expires."
    )
    return "\n".join(lines)


def certificate_watches(
    records: list[ApprovalRecord], today: date, window_days: int
) -> list[dict]:
    """Relied-upon certificates expiring within ``window_days`` of
    ``today``, soonest first. Pure over the stored records: the
    expiry is the one the stored expiry check recorded in its
    ``found`` field — an unparseable value yields no watch, never
    a guessed date. Each row names its state (``expired`` once
    the date has passed, ``expiring`` while it approaches), its
    chain's root, the coverage facts its review recorded
    (:func:`coverage_facts`), and the renewal-request draft those
    facts compose (:func:`renewal_request_draft`).

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
        coverage = coverage_facts(source)
        row = {
            "contract_id": result.contract_id,
            "chain_root": chain[0].result.contract_id,
            "counterparty_name": result.counterparty_name,
            "status": result.status,
            "expiry_date": expiry.isoformat(),
            "days_until": days_until,
            "overdue": days_until < 0,
            "state": "expired" if days_until < 0 else "expiring",
            "exhibit_document_id": check.exhibit_document_id,
            "renewed": renewal is not None and renewal.status == "consistent",
            "coverage": coverage,
            "renewal_request_draft": renewal_request_draft(
                counterparty_name=result.counterparty_name,
                contract_id=result.contract_id,
                chain_root=chain[0].result.contract_id,
                exhibit_document_id=check.exhibit_document_id,
                expiry=expiry,
                days_until=days_until,
                today=today,
                coverage=coverage,
            ),
        }
        if renewal_failed:
            row["renewal_evidence_failed"] = True
            row["renewal_contract_id"] = latest.result.contract_id
            row["renewal_status"] = renewal.status
        rows.append(row)
    rows.sort(key=lambda row: (row["expiry_date"], row["contract_id"]))
    return rows
