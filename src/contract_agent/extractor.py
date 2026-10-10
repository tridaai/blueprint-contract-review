"""Per-clause extraction.

Every clause unit the segmenter cut is read twice:

- **Deterministic rules** always run: keyword scoring assigns a clause
  type, and typed signals (cap amounts, notice periods, net days,
  jurisdictions, ...) are regex-extracted from the clause text. These
  signals are what the playbook rules in ``clause_review.py`` judge —
  code judging code-extracted facts, fully auditable.
- **In provider mode**, the LLM reads the clause too
  (``ModelBackend.extract_clause_fields``) and returns its own type,
  signals, and key facts. The two reads are cross-checked: type or
  signal disagreements are recorded on the extraction as discrepancies
  for counsel, and the LLM's type is adopted only when it is confident
  and the rules were not.

Extraction never fails a run: a provider error or unusable reply
leaves the rules extraction standing alone.
"""

from __future__ import annotations

import re

from .schemas import CLAUSE_TYPES, ClauseExtraction, ClauseUnit

# ---------------------------------------------------------------------------
# Rules-based clause typing (keyword scoring over heading + text)
# ---------------------------------------------------------------------------

_TYPE_KEYWORDS: dict[str, list[str]] = {
    "liability_cap": [
        "limitation of liability", "liability cap", "aggregate liability",
        "in no event shall", "consequential damages", "liability shall not exceed",
        "liability is limited", "cap on liability", "liability",
    ],
    "indemnity": [
        "indemnify", "indemnification", "indemnity", "hold harmless", "defend and hold",
    ],
    "ip_ownership": [
        "intellectual property", "ownership of", "work product", "deliverables shall",
        "assigns to", "title and interest", "proprietary rights",
    ],
    "confidentiality": [
        "confidential information", "confidentiality", "non-disclosure",
        "shall not disclose", "hold in confidence", "proprietary information",
    ],
    "term_termination": [
        "term and termination", "termination", "terminate", "auto-renew",
        "automatically renew", "renewal term", "initial term", "expire",
    ],
    "payment_terms": [
        "fees and payment", "payment terms", "invoices", "net 30", "net 45", "net 60",
        "late fee", "late payment", "payable within", "fees shall be paid",
    ],
    "governing_law": [
        "governing law", "governed by the laws", "jurisdiction", "venue",
        "choice of law", "courts located", "arbitration",
    ],
    "data_protection": [
        "data protection", "personal data", "data processing", "privacy",
        "security measures", "data breach", "sub-processor", "subprocessor", "gdpr",
    ],
    "limitation_of_warranty": [
        "warranty", "warranties", "as is", "disclaims", "merchantability",
        "fitness for a particular purpose",
    ],
    "assignment": [
        "assignment", "may assign", "shall not assign", "change of control",
        "assign this agreement", "delegate",
    ],
}


def classify_clause_type_rules(unit: ClauseUnit) -> tuple[str, float]:
    """Keyword-scored clause typing. Returns (type, confidence)."""
    heading = (unit.heading or "").lower()
    text = (unit.text or "").lower()
    scores: dict[str, int] = {}
    for clause_type, keywords in _TYPE_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if kw in heading:
                score += 3
            if kw in text:
                score += 1
        if score:
            scores[clause_type] = score
    if not scores:
        return "other", 0.4
    best = max(scores, key=lambda k: scores[k])
    runner_up = max((s for t, s in scores.items() if t != best), default=0)
    confidence = 0.92 if scores[best] >= 3 and scores[best] > runner_up else 0.7
    return best, confidence


# ---------------------------------------------------------------------------
# Rules-based signal extraction (typed facts the playbook rules judge)
# ---------------------------------------------------------------------------


def _notice_days(text: str) -> int | None:
    patterns = [
        r"at least (\d+)\s*days",
        r"(\d+)\s*days['’]?\s*(?:prior\s+)?(?:written\s+)?notice",
        r"notice[^.]{0,60}?(\d+)\s*days",
        r"(\d+)\s*days before",
    ]
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return int(m.group(1))
    return None


def _money(text: str) -> float | None:
    m = re.search(r"\$\s?([\d,]+(?:\.\d+)?)\s*(million|m)?\b", text)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    if m.group(2):
        value *= 1_000_000
    return value


def extract_signals_rules(unit: ClauseUnit, clause_type: str) -> dict:
    text = (unit.text or "").lower()
    signals: dict = {}
    if clause_type == "liability_cap":
        signals["uncapped"] = any(
            p in text
            for p in (
                "unlimited liability", "liability shall not be limited",
                "shall not be limited", "liability is unlimited",
                "no cap shall apply", "uncapped", "shall be unlimited",
            )
        )
        # Cap expressed as months of fees: "fees paid in the twelve (12)
        # months", "12 months of fees" — the digit may be parenthesised
        # or spelled alongside the word form.
        m = None
        if "fees" in text:
            m = re.search(r"\(?(\d+)\)?\s*months?", text)
        if m:
            signals["cap_months_fees"] = int(m.group(1))
        amount = _money(text)
        if amount is not None and ("liabilit" in text or "exceed" in text):
            signals["cap_amount"] = amount
        signals["cap_present"] = bool(
            signals.get("cap_months_fees") or signals.get("cap_amount")
            or "shall not exceed" in text or "limited to" in text
        )
    elif clause_type == "indemnity":
        signals["mutual"] = "each party" in text and "indemnif" in text
        signals["against_us"] = bool(
            re.search(r"(provider|vendor|supplier|contractor) shall indemnify", text)
        ) and not signals["mutual"]
        signals["covers_counterparty_negligence"] = (
            "negligence" in text and ("indemnif" in text)
        )
    elif clause_type == "ip_ownership":
        signals["assigns_ip"] = any(
            p in text for p in ("hereby assigns", "assigns to", "shall vest in", "assignment of all")
        )
        signals["covers_background_ip"] = any(
            p in text
            for p in ("pre-existing", "background intellectual property", "background ip",
                      "all right, title and interest")
        ) and signals["assigns_ip"]
        signals["deliverables_only"] = "deliverable" in text and signals["assigns_ip"]
        signals["retains_ip"] = any(
            p in text for p in ("retains ownership", "shall remain the property",
                                "retains all right", "no assignment")
        )
    elif clause_type == "confidentiality":
        signals["mutual"] = any(
            p in text for p in ("each party", "either party", "mutual", "both parties")
        )
        m = re.search(r"(\d+)\s*years?", text)
        signals["duration_years"] = int(m.group(1)) if m else None
        signals["perpetual"] = any(
            p in text for p in ("perpetual", "in perpetuity", "indefinitely", "without time limit")
        )
    elif clause_type == "term_termination":
        signals["termination_for_convenience"] = "for convenience" in text
        signals["auto_renewal"] = any(
            p in text for p in ("auto-renew", "automatically renew", "renew for successive",
                                 "successive renewal", "shall renew")
        )
        signals["notice_days"] = _notice_days(text)
    elif clause_type == "payment_terms":
        m = re.search(r"net\s*(\d+)", text) or re.search(
            r"within\s+(\d+)\s+days\s+of\s+(?:the\s+)?invoice", text
        ) or re.search(r"payable\s+within\s+(\d+)\s+days", text)
        signals["net_days"] = int(m.group(1)) if m else None
        m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*per\s+month", text) or re.search(
            r"(\d+(?:\.\d+)?)\s*percent\s+per\s+month", text
        )
        signals["late_fee_pct_monthly"] = float(m.group(1)) if m else None
        signals["prepay_full"] = any(
            p in text for p in ("payable in advance", "paid in advance", "payment in advance")
        )
    elif clause_type == "governing_law":
        foreign = [
            "england and wales", "laws of england", "laws of germany", "german law",
            "laws of france", "laws of singapore", "laws of ireland", "laws of india",
            "laws of ontario", "laws of the netherlands", "swiss law",
        ]
        signals["foreign_law"] = any(p in text for p in foreign)
        m = re.search(r"laws of the state of ([a-z ]+?)[.,;]", text) or re.search(
            r"laws of ([a-z ]+?)(?:\.|,|;)", text
        )
        signals["jurisdiction"] = m.group(1).strip() if m else ""
        signals["arbitration"] = "arbitrat" in text
    elif clause_type == "data_protection":
        signals["dpa_referenced"] = any(
            p in text for p in ("data processing addendum", "data protection addendum",
                                "dpa", "processing addendum")
        )
        m = re.search(r"within\s+(\d+)\s*hours", text)
        signals["breach_notice_hours"] = int(m.group(1)) if m else None
        m = re.search(r"within\s+(\d+)\s*(?:business\s+)?days", text)
        if signals["breach_notice_hours"] is None and m and "breach" in text:
            signals["breach_notice_hours"] = int(m.group(1)) * 24
        signals["security_measures"] = "security" in text
    elif clause_type == "limitation_of_warranty":
        m = re.search(r"(\d+)\s*months?", text)
        signals["warranty_months"] = int(m.group(1)) if m and "warrant" in text else None
        signals["disclaims_all"] = "as is" in text or "disclaims all" in text
    elif clause_type == "assignment":
        signals["consent_required"] = any(
            p in text for p in ("without the prior written consent",
                                 "without prior written consent",
                                 "may not assign", "shall not assign")
        )
        signals["free_assignment"] = any(
            p in text for p in ("may freely assign", "freely assign",
                                 "may assign this agreement without",
                                 "without consent")
        ) and not signals["consent_required"]
        signals["change_of_control"] = "change of control" in text
        signals["affiliates_only"] = "affiliate" in text
    return signals


def key_facts_for(clause_type: str, signals: dict) -> list[str]:
    facts = []
    for key, value in signals.items():
        if value in (None, "", False):
            continue
        facts.append(f"{key}={value}")
    return facts


# ---------------------------------------------------------------------------
# Extraction entry point (rules always; LLM cross-checked in provider mode)
# ---------------------------------------------------------------------------


def extract_clauses(units: list[ClauseUnit], backend) -> list[ClauseExtraction]:
    """Extract every clause unit. Rules run always; in provider mode
    the LLM's read is cross-checked against them."""
    extract = getattr(backend, "extract_clause_fields", None)
    results: list[ClauseExtraction] = []
    for unit in units:
        rule_type, rule_confidence = classify_clause_type_rules(unit)
        rule_signals = extract_signals_rules(unit, rule_type)
        extraction = ClauseExtraction(
            clause_id=unit.clause_id,
            clause_type=rule_type,
            type_confidence=rule_confidence,
            type_source="rules",
            signals=rule_signals,
            key_facts=key_facts_for(rule_type, rule_signals),
        )
        if extract is None:
            results.append(extraction)
            continue
        try:
            llm = extract(unit)
        except Exception as exc:  # extraction is evidence, never a run failure
            extraction.note = f"LLM clause extraction failed ({exc}) — rules extraction used"
            results.append(extraction)
            continue
        if not llm:
            extraction.note = "LLM clause extraction reply was unusable — rules extraction used"
            results.append(extraction)
            continue
        llm_type = str(llm.get("clause_type") or "other")
        if llm_type not in CLAUSE_TYPES:
            llm_type = "other"
        llm_confidence = float(llm.get("confidence") or 0.0)
        llm_signals = llm.get("signals") or {}
        discrepancies: list[str] = []
        if llm_type != rule_type:
            discrepancies.append(
                f"clause type: rules read '{rule_type}', LLM read '{llm_type}'"
            )
        merged = dict(rule_signals)
        for key, value in llm_signals.items():
            if key in rule_signals and rule_signals[key] not in (None, "") \
                    and value not in (None, "") and rule_signals[key] != value:
                discrepancies.append(
                    f"signal '{key}': rules extracted {rule_signals[key]!r}, "
                    f"LLM extracted {value!r}"
                )
            elif value not in (None, ""):
                merged[key] = value
        final_type = rule_type
        if llm_type != rule_type and llm_confidence >= 0.8:
            final_type = llm_type
            merged = extract_signals_rules(unit, final_type)
            for key, value in llm_signals.items():
                if value not in (None, ""):
                    merged[key] = value
        extraction.clause_type = final_type
        extraction.type_confidence = max(rule_confidence, llm_confidence)
        extraction.type_source = "llm" if final_type == llm_type and llm_type != rule_type else "rules"
        extraction.signals = merged
        extraction.key_facts = [str(f) for f in (llm.get("key_facts") or [])] or key_facts_for(
            final_type, merged
        )
        extraction.discrepancies = discrepancies
        results.append(extraction)
    return results


def extraction_discrepancies(extractions: list[ClauseExtraction]) -> list[str]:
    """Human-readable discrepancy lines for the trace and assessment."""
    lines: list[str] = []
    for extraction in extractions:
        for discrepancy in extraction.discrepancies:
            lines.append(f"{extraction.clause_id}: {discrepancy}")
    return lines
