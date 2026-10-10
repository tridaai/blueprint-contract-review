"""Certificate biography: the certificate chain's whole
timeline in one view.

The annex history (round 15) gave the two living lists — the
sub-processor list, the prior-invention schedule — their
timelines. The certificate of insurance is the third living
document of a negotiation, and until now it read only round by
round: each round's own checks against its own draft, the
renewal check on the latest step, the watch standing on
whichever expiry the evidence supports. Across a long chain
nobody could see the certificate's whole life in one place —
what each round's agreement required, what each round's
certificate actually stated, which renewals evidenced the
bargain and which quietly dropped part of it, and when.

This module is that view, in the annex history's shape: one
row per round (the requirement set its agreement stated, the
certificate it carried and what that certificate states, the
stored single-round verdicts), one step per round pair (a
renewal arrived or it did not; the renewal check's verdict;
the drops named compactly), and a summary. Everything is
computed with the other modules' own readings — the exhibit
checks' certificate readings (``exhibit_review.py``), the
renewal composition's composed requirement set and provenance
(``certificate_renewal.py``), the stored results' own checks —
so the biography can never disagree with the findings it
narrates. The operative watch reading (which expiry the
renewal watch stands on for this chain) rides on the view as
``watch``, computed by the watch's own rule in the service
(``certificates.py``): the biography narrates; the watch
decides.
"""

from __future__ import annotations

from .certificate_renewal import _compose_requirement_set, _composed_provenance
from .exhibit_review import (
    _CERT_DOC,
    _COVERAGE_KEYS,
    _certificate_expiry,
    _coverage_amounts,
    _endorsement_present,
    _find_schedule_document,
    _fmt_money,
    _named_insured,
    _normalise_name,
)
from .schemas import AgentResult, ContractInput

_LABELS = {key: label for key, label, _ in _COVERAGE_KEYS}


def _round_requirements(contract: ContractInput) -> dict:
    """What one round's OWN agreement requires of the insurance:
    the coverage limits it states, the endorsements it requires,
    the counterparty it requires as named insured. (What a
    renewal is JUDGED against is the composed set across the
    chain — that lives on the steps; this is the round's own
    bargain, stated as it stood.)"""
    agreement = contract.agreement_text()
    lower = agreement.lower()
    limits = _coverage_amounts(agreement)
    return {
        "limits": [
            {"key": key, "label": _LABELS[key], "amount": limits[key]}
            for key in sorted(limits)
        ],
        "additional_insured": "additional insured" in lower,
        "waiver_of_subrogation": "waiver of subrogation" in lower,
        "named_insured": contract.counterparty_name or "",
    }


def _certificate_state(contract: ContractInput) -> dict | None:
    """The certificate one round carries, read with the exhibit
    checks' own readings: its expiry, the limits it states, the
    insured it names, the endorsements it shows. None when the
    round carries no certificate document at all."""
    cert = _find_schedule_document(contract, _CERT_DOC)
    if cert is None:
        return None
    text = cert.raw_text
    lower = text.lower()
    expiry = _certificate_expiry(text)
    return {
        "exhibit_document_id": cert.document_id or cert.doc_type,
        "expiry": expiry.isoformat() if expiry is not None else "",
        "limits": [
            {"key": key, "label": _LABELS[key], "amount": amount}
            for key, amount in sorted(_coverage_amounts(text).items())
        ],
        "named_insured": _named_insured(text),
        "endorsements": {
            "additional_insured": _endorsement_present(
                lower, "additional insured"
            ),
            "waiver_of_subrogation": _endorsement_present(
                lower, "waiver of subrogation"
            ),
        },
    }


def _renewal_drops(
    chain_prefix: list[ContractInput],
    prior: ContractInput,
    current: ContractInput,
) -> list[dict]:
    """What a renewal certificate fails to evidence, named
    compactly: the requirement set composed across the chain up
    to ``prior`` (each requirement from the round that last set
    it, with its provenance), read against the certificate
    ``current`` carries — the renewal check's own composition
    and the exhibit readings, so the drops here are the drops
    the check found, in timeline form."""
    current_cert = _find_schedule_document(current, _CERT_DOC)
    if current_cert is None:
        return []
    required_limits, setters = _compose_requirement_set(chain_prefix)
    provenance = _composed_provenance(required_limits, setters)
    cert_text = current_cert.raw_text
    cert_lower = cert_text.lower()
    cert_limits = _coverage_amounts(cert_text)
    counterparty = prior.counterparty_name or current.counterparty_name or ""
    drops: list[dict] = []

    def drop(requirement: str, detail: str) -> None:
        entry = {"requirement": requirement, "detail": detail}
        if requirement in provenance:
            entry["set_by"] = provenance[requirement]
        drops.append(entry)

    if counterparty:
        found_name = _named_insured(cert_text)
        if not found_name:
            drop(
                "named_insured",
                f"named insured: the certificate names no insured, "
                f"where the terms require {counterparty}",
            )
        elif _normalise_name(found_name) != _normalise_name(counterparty):
            drop(
                "named_insured",
                f"named insured: the certificate names {found_name}, "
                f"not {counterparty}",
            )
    for key in sorted(required_limits):
        required = required_limits[key]
        stated = cert_limits.get(key)
        if stated is None:
            drop(
                key,
                f"{_LABELS[key]}: required at {_fmt_money(required)}, "
                "not stated on the certificate",
            )
        elif stated < required:
            drop(
                key,
                f"{_LABELS[key]}: required at {_fmt_money(required)}, "
                f"stated at {_fmt_money(stated)}",
            )
    for phrase in ("additional insured", "waiver of subrogation"):
        if phrase in setters and _endorsement_present(cert_lower, phrase) is not True:
            drop(
                phrase,
                f"the {phrase} endorsement: required, not shown on "
                "the certificate",
            )
    return drops


def build_certificate_history(
    chain: list[ContractInput],
    results: dict[str, AgentResult] | None = None,
) -> dict | None:
    """The certificate timeline for one negotiation chain,
    oldest round first: each round's requirement set, the
    certificate it carried and what that certificate states,
    and its stored insurance verdicts; each step's renewal
    reading — whether cover was extended, the renewal check's
    verdict, and what the renewal dropped against the composed
    requirement set. ``results`` maps contract_id → the stored
    analysis result, the source of the verdicts; without a
    result a round still shows its documents, with no verdicts
    claimed for it. None when no round of the chain carries a
    certificate or an insurance verdict — the view does not
    invent a biography for cover the negotiation never had."""
    results = results or {}
    rounds: list[dict] = []
    for contract in chain:
        result = results.get(contract.contract_id)
        checks = []
        renewal = None
        if result is not None:
            checks = [
                {"name": c.name, "status": c.status, "found": c.found}
                for c in result.exhibit_checks
                if c.name.startswith("insurance_")
            ]
            renewal_check = next(
                (
                    c
                    for c in result.certificate_renewal
                    if c.name == "insurance_certificate_renewal"
                ),
                None,
            )
            if renewal_check is not None:
                renewal = {
                    "status": renewal_check.status,
                    "found": renewal_check.found,
                }
        rounds.append(
            {
                "contract_id": contract.contract_id,
                "received_at": contract.received_at or "",
                "requirements": _round_requirements(contract),
                "certificate": _certificate_state(contract),
                "checks": checks,
                "renewal": renewal,
            }
        )
    if not any(
        row["certificate"] is not None or row["checks"] or row["renewal"]
        for row in rounds
    ):
        return None

    steps: list[dict] = []
    renewals = 0
    failed_renewals: list[str] = []
    for index, (prior_row, current_row) in enumerate(
        zip(rounds, rounds[1:]), start=1
    ):
        prior = chain[index - 1]
        current = chain[index]
        step: dict = {
            "from_contract_id": prior_row["contract_id"],
            "to_contract_id": current_row["contract_id"],
        }
        prior_cert = prior_row["certificate"]
        current_cert = current_row["certificate"]
        verdict = (
            current_row["renewal"]["status"] if current_row["renewal"] else None
        )
        if prior_cert is None and current_cert is None:
            step.update(
                {
                    "renewal_arrived": False,
                    "verdict": None,
                    "drops": [],
                    "summary": (
                        "Neither round carries a certificate — no "
                        "renewal can be read across this step."
                    ),
                }
            )
        elif prior_cert is None:
            step.update(
                {
                    "renewal_arrived": False,
                    "verdict": None,
                    "drops": [],
                    "summary": (
                        f"A certificate first appears in "
                        f"{current_row['contract_id']}"
                        + (
                            f", expiring {current_cert['expiry']}"
                            if current_cert["expiry"]
                            else ""
                        )
                        + " — there is no earlier certificate for "
                        "it to renew."
                    ),
                }
            )
        elif current_cert is None:
            step.update(
                {
                    "renewal_arrived": False,
                    "verdict": None,
                    "drops": [],
                    "summary": (
                        f"{current_row['contract_id']} carries no "
                        f"certificate — the cover "
                        f"{prior_row['contract_id']} evidenced is "
                        "unevidenced in this round."
                    ),
                }
            )
        else:
            arrived = bool(
                prior_cert["expiry"]
                and current_cert["expiry"]
                and current_cert["expiry"] > prior_cert["expiry"]
            )
            if not arrived:
                step.update(
                    {
                        "renewal_arrived": False,
                        "verdict": verdict,
                        "drops": [],
                        "summary": (
                            f"The certificate in "
                            f"{current_row['contract_id']} expires "
                            f"{current_cert['expiry'] or 'on a date it does not state'}, "
                            f"no later than the "
                            f"{prior_cert['expiry'] or 'unstated'} expiry "
                            f"in {prior_row['contract_id']} — cover "
                            "was not extended, so no renewal arrived "
                            "across this step."
                        ),
                    }
                )
            else:
                renewals += 1
                drops = _renewal_drops(chain[:index], prior, current)
                if verdict == "mismatch":
                    failed_renewals.append(current_row["contract_id"])
                if verdict == "consistent":
                    summary = (
                        f"{current_row['contract_id']} renews cover "
                        f"to {current_cert['expiry']} and evidences "
                        "the requirement set composed across the "
                        "chain in full — the renewal check is "
                        "consistent, and the watch moves to the new "
                        "expiry on that evidence."
                    )
                elif verdict == "mismatch":
                    summary = (
                        f"{current_row['contract_id']} renews cover "
                        f"to {current_cert['expiry']}, but against "
                        "the requirement set composed across the "
                        "chain it drops: "
                        + "; ".join(d["detail"] for d in drops)
                        + " — the renewal check is a mismatch, and "
                        "the watch stays open on the earlier expiry."
                    )
                else:
                    summary = (
                        f"{current_row['contract_id']} extends cover "
                        f"to {current_cert['expiry']}; no renewal "
                        "check is on record for this round."
                    )
                step.update(
                    {
                        "renewal_arrived": True,
                        "verdict": verdict,
                        "drops": drops,
                        "summary": summary,
                    }
                )
        steps.append(step)

    carried = [row for row in rounds if row["certificate"] is not None]
    summary_lines = [
        f"Across {len(rounds)} round(s), {len(carried)} carried a "
        "certificate of insurance."
    ]
    if carried and carried[-1]["certificate"]["expiry"]:
        latest = carried[-1]
        summary_lines.append(
            f"The latest certificate ({latest['contract_id']}) "
            f"expires {latest['certificate']['expiry']}."
        )
    if renewals:
        line = f"{renewals} renewal(s) arrived across the chain"
        if failed_renewals:
            line += (
                f"; {len(failed_renewals)} failed the evidence "
                f"check against the composed requirement set "
                f"({', '.join(failed_renewals)}) — arrival is not "
                "evidence, and the watch did not close on them."
            )
        else:
            line += (
                "; every one evidenced the composed requirement "
                "set in full."
            )
        summary_lines.append(line)
    unevidenced = [
        row["contract_id"]
        for row in rounds
        if row["certificate"] is None
        and (row["requirements"]["limits"] or row["checks"])
    ]
    if unevidenced:
        summary_lines.append(
            "Round(s) whose agreement required insurance the "
            "package never evidenced: " + ", ".join(unevidenced) + "."
        )
    return {"rounds": rounds, "steps": steps, "summary": summary_lines}
