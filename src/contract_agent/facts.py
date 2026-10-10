"""Computed document facts — in code, never by the model.

Posture, position counts, absence detection, and package completeness
are the numbers the whole pipeline reasons over. They are computed
here from the clause units, extractions, and verdicts,
deterministically, so the same contract always produces the same
facts.

Absence detection matters: a services agreement with no liability cap
is not "silent", it is unprotected. The standard-clause expectations
per contract type live in ``STANDARD_CLAUSES``; liability-cap and
data-protection absences are critical — they escalate the posture on
their own. Failed exhibit consistency checks (``exhibit_review.py``)
land here the same way: a contradiction between the agreement and
its own exhibit floors the posture at negotiable, and a promised
exhibit that never arrived makes the package incomplete.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from .schemas import (
    POSITION_SEVERITY,
    ClauseExtraction,
    ClauseUnit,
    ClauseVerdict,
    ContractFacts,
    ContractInput,
    ExhibitCheck,
)

# Standard clauses expected per contract type. Absence of any of these
# is a finding; absence of a CRITICAL one escalates the posture.
STANDARD_CLAUSES: dict[str, list[str]] = {
    "msa": [
        "liability_cap", "indemnity", "confidentiality",
        "term_termination", "governing_law", "data_protection",
    ],
    "sow": [
        "liability_cap", "ip_ownership", "confidentiality",
        "payment_terms", "term_termination", "data_protection",
    ],
    "nda": ["confidentiality", "governing_law", "term_termination"],
    "amendment": ["governing_law"],
    "order_form": ["payment_terms", "term_termination", "liability_cap"],
    "general": ["liability_cap", "confidentiality", "governing_law"],
}

CRITICAL_ABSENCES = {"liability_cap", "data_protection"}

_EXHIBIT_REF = re.compile(r"\bexhibit\s+([A-Z])\b", re.I)


def _parse_date(s: str) -> date | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(s)[:10])
        except ValueError:
            return None


def compute_facts(
    contract: ContractInput,
    units: list[ClauseUnit],
    extractions: list[ClauseExtraction],
    verdicts: list[ClauseVerdict],
    today: date | None = None,
    exhibit_checks: list[ExhibitCheck] | None = None,
) -> ContractFacts:
    by_type: dict[str, int] = {}
    for extraction in extractions:
        by_type[extraction.clause_type] = by_type.get(extraction.clause_type, 0) + 1

    positions: dict[str, int] = {}
    for verdict in verdicts:
        positions[verdict.position] = positions.get(verdict.position, 0) + 1
    worst = "preferred"
    for position in positions:
        if POSITION_SEVERITY[position] > POSITION_SEVERITY[worst]:
            worst = position

    expected = STANDARD_CLAUSES.get(contract.doc_type, STANDARD_CLAUSES["general"])
    missing = [c for c in expected if by_type.get(c, 0) == 0]
    critical = [c for c in missing if c in CRITICAL_ABSENCES]

    if worst == "walk_away" or critical:
        posture = "escalation_required"
    elif worst in {"review_required", "acceptable_fallback"} or missing:
        posture = "negotiable"
    else:
        posture = "aligned"

    discrepancies: list[str] = []
    for extraction in extractions:
        for d in extraction.discrepancies:
            discrepancies.append(f"{extraction.clause_id}: {d}")

    full_text = contract.agreement_text()
    referenced = sorted({m.group(1).upper() for m in _EXHIBIT_REF.finditer(full_text)})
    attached = {
        (doc.document_id or "").strip().upper()
        for doc in contract.documents
        if doc.doc_type in {"exhibit", "schedule"}
    }
    attached_letters = {a[-1] for a in attached if a}
    exhibits_missing = [r for r in referenced if r not in attached_letters]
    signature_present = any(
        doc.doc_type == "signature_page" for doc in contract.documents
    ) or "in witness whereof" in full_text.lower()

    # Exhibit consistency feeds the facts like an absence does: a
    # failed cross-document check means the package does not hang
    # together, so an otherwise-aligned draft is at best negotiable,
    # and a promised-but-absent exhibit makes the package incomplete
    # (the information request then asks for it by name).
    failed_checks = [
        c for c in (exhibit_checks or []) if c.status != "consistent"
    ]
    exhibit_mismatches = sorted({c.name for c in failed_checks})
    exhibit_absent = any(c.status == "exhibit_missing" for c in failed_checks)
    if exhibit_mismatches and posture == "aligned":
        posture = "negotiable"

    received = _parse_date(contract.received_at)
    age = ((today or date.today()) - received).days if received else 0

    return ContractFacts(
        clause_count=len(units),
        clauses_by_type=by_type,
        positions_count=positions,
        worst_position=worst if verdicts else "preferred",
        posture=posture,
        missing_standard_clauses=missing,
        critical_absences=critical,
        unheaded_units=sum(1 for u in units if not u.heading and not u.ref),
        discrepancy_count=len(discrepancies),
        discrepancies=discrepancies,
        exhibits_referenced=referenced,
        exhibits_missing=exhibits_missing,
        exhibit_mismatches=exhibit_mismatches,
        exhibit_absent=exhibit_absent,
        signature_pages_present=signature_present,
        package_complete=not exhibits_missing and not exhibit_absent,
        contract_age_days=max(0, age),
    )
