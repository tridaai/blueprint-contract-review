"""Deterministic per-clause review rules over the playbook matrix.

Rules run first and independently. They read only the typed signals
the extractor pulled from the clause text — never model output — so
the same clause always earns the same verdict. The LLM cross-check
(see ``crosscheck.py``) can only change a verdict under the one
explicit resolution policy.

The matrix mirrors the playbook corpus (``playbook_data.py``); the
entry IDs cited on each verdict are the entries a reviewer can open.
"""

from __future__ import annotations

from .playbook_registry import active_entry_by_id, entries_for_type
from .schemas import ClauseExtraction, ClauseUnit, ClauseVerdict

_FLAGGED = {"review_required", "walk_away", "acceptable_fallback"}


def _verdict(
    unit: ClauseUnit,
    extraction: ClauseExtraction,
    position: str,
    confidence: float,
    rationale: str,
    refs: list[str],
) -> ClauseVerdict:
    fallback = ""
    for ref in refs:
        entry = active_entry_by_id(ref) or {}
        if entry.get("fallback_language"):
            fallback = entry["fallback_language"]
            break
    if not fallback and position in _FLAGGED:
        # The cited entries offer no language. A counsel-activated
        # proposal entry for this clause type (playbook_registry)
        # may: the playbook was extended by a human precisely for
        # this gap, so its language is offered and its id cited.
        for entry in entries_for_type(extraction.clause_type):
            if entry.get("fallback_language"):
                fallback = entry["fallback_language"]
                if entry["id"] not in refs:
                    refs = [*refs, entry["id"]]
                break
    return ClauseVerdict(
        clause_id=unit.clause_id,
        clause_type=extraction.clause_type,
        position=position,
        confidence=confidence,
        rationale=rationale,
        rule_position=position,
        resolution="rules_only",
        playbook_refs=refs,
        fallback_language=fallback if position in _FLAGGED else "",
    )


def review_clause(unit: ClauseUnit, extraction: ClauseExtraction) -> ClauseVerdict:
    """The rule verdict for one clause, from its extracted signals."""
    t = extraction.clause_type
    s = extraction.signals or {}

    if t == "liability_cap":
        if s.get("uncapped"):
            return _verdict(unit, extraction, "walk_away", 0.93,
                            "The clause leaves liability uncapped; the playbook treats "
                            "uncapped liability as a walk-away (PB-LIA-03).",
                            ["PB-LIA-03"])
        months = s.get("cap_months_fees")
        amount = s.get("cap_amount")
        if (months is not None and months >= 12) or (amount is not None and amount >= 1_000_000):
            return _verdict(unit, extraction, "preferred", 0.9,
                            "Liability is capped at or above the playbook's preferred "
                            "level (12 months of fees or 1,000,000) (PB-LIA-01).",
                            ["PB-LIA-01"])
        if s.get("cap_present"):
            return _verdict(unit, extraction, "acceptable_fallback", 0.85,
                            "A stated, mutual cap exists but sits below the preferred "
                            "level — an acceptable fallback (PB-LIA-02).",
                            ["PB-LIA-02"])
        return _verdict(unit, extraction, "review_required", 0.6,
                        "The clause addresses liability but states no cap the rules "
                        "can measure; counsel must read it (PB-LIA-02).",
                        ["PB-LIA-02"])

    if t == "indemnity":
        if s.get("covers_counterparty_negligence") and (s.get("against_us") or s.get("mutual")):
            return _verdict(unit, extraction, "walk_away", 0.88,
                            "The indemnity reaches the counterparty's own negligence; "
                            "the playbook escalates that as a walk-away (PB-IND-02).",
                            ["PB-IND-02"])
        if s.get("against_us"):
            return _verdict(unit, extraction, "review_required", 0.85,
                            "The indemnity runs one way, against us; the playbook "
                            "requires counsel review (PB-IND-02).",
                            ["PB-IND-02"])
        if s.get("mutual"):
            return _verdict(unit, extraction, "preferred", 0.9,
                            "The indemnity is mutual and proportionate — the playbook's "
                            "preferred position (PB-IND-01).",
                            ["PB-IND-01"])
        return _verdict(unit, extraction, "review_required", 0.55,
                        "The indemnity's direction cannot be determined from the "
                        "clause text; counsel must read it (PB-IND-02).",
                        ["PB-IND-02"])

    if t == "ip_ownership":
        if s.get("assigns_ip") and s.get("covers_background_ip"):
            return _verdict(unit, extraction, "walk_away", 0.92,
                            "The clause assigns intellectual property including "
                            "pre-existing/background IP to the counterparty — a "
                            "walk-away (PB-IP-02).",
                            ["PB-IP-02"])
        if s.get("assigns_ip") and s.get("deliverables_only"):
            return _verdict(unit, extraction, "acceptable_fallback", 0.82,
                            "Assignment is limited to deliverables created under the "
                            "agreement — an acceptable fallback with counsel approval "
                            "(PB-IP-02).",
                            ["PB-IP-02"])
        if s.get("retains_ip") or not s.get("assigns_ip"):
            return _verdict(unit, extraction, "preferred", 0.85,
                            "No assignment of our IP: ownership is retained and any "
                            "use is by licence — the preferred position (PB-IP-01).",
                            ["PB-IP-01"])
        return _verdict(unit, extraction, "review_required", 0.6,
                        "The clause assigns intellectual property on terms the rules "
                        "cannot scope; counsel must read it (PB-IP-02).",
                        ["PB-IP-02"])

    if t == "confidentiality":
        if s.get("perpetual") or (s.get("duration_years") is not None and s["duration_years"] > 5):
            return _verdict(unit, extraction, "review_required", 0.87,
                            "Confidentiality lasts longer than the playbook's five "
                            "years (or is perpetual) — counsel review required "
                            "(PB-CON-02).",
                            ["PB-CON-02"])
        if s.get("mutual"):
            return _verdict(unit, extraction, "preferred", 0.9,
                            "Confidentiality is mutual and time-bounded — the "
                            "preferred position (PB-CON-01).",
                            ["PB-CON-01"])
        return _verdict(unit, extraction, "acceptable_fallback", 0.8,
                        "Confidentiality reads one-way; the playbook prefers mutual "
                        "obligations but accepts a narrow one-way clause as a "
                        "fallback (PB-CON-02).",
                        ["PB-CON-02"])

    if t == "term_termination":
        notice = s.get("notice_days")
        if not s.get("termination_for_convenience"):
            return _verdict(unit, extraction, "review_required", 0.85,
                            "No termination for convenience: the playbook requires "
                            "counsel review of any lock-in (PB-TERM-02).",
                            ["PB-TERM-02"])
        if s.get("auto_renewal") and notice is not None and notice > 180:
            return _verdict(unit, extraction, "review_required", 0.85,
                            f"Auto-renewal with a {notice}-day notice window exceeds "
                            "the playbook's 180-day review trigger (PB-TERM-02).",
                            ["PB-TERM-02"])
        if s.get("auto_renewal") and notice is not None and notice > 90:
            return _verdict(unit, extraction, "acceptable_fallback", 0.83,
                            f"Auto-renewal with a {notice}-day notice window is an "
                            "acceptable fallback where termination for convenience "
                            "exists (PB-TERM-02).",
                            ["PB-TERM-02"])
        return _verdict(unit, extraction, "preferred", 0.88,
                        "Termination for convenience on a notice window within the "
                        "playbook's ninety days (PB-TERM-01).",
                        ["PB-TERM-01"])

    if t == "payment_terms":
        net = s.get("net_days")
        late = s.get("late_fee_pct_monthly")
        if s.get("prepay_full"):
            return _verdict(unit, extraction, "review_required", 0.85,
                            "Full payment in advance is a playbook review trigger "
                            "(PB-PAY-02).",
                            ["PB-PAY-02"])
        if late is not None and late > 1.5:
            return _verdict(unit, extraction, "review_required", 0.85,
                            f"Late fees of {late}% per month exceed the playbook's "
                            "1.5% review trigger (PB-PAY-02).",
                            ["PB-PAY-02"])
        if net is not None and net > 60:
            return _verdict(unit, extraction, "review_required", 0.85,
                            f"Payment terms of net {net} exceed the playbook's "
                            "net-60 review trigger (PB-PAY-02).",
                            ["PB-PAY-02"])
        if net is not None and net > 45:
            return _verdict(unit, extraction, "acceptable_fallback", 0.85,
                            f"Net {net} is beyond the preferred net 45 but within the "
                            "acceptable fallback (PB-PAY-02).",
                            ["PB-PAY-02"])
        if net is not None:
            return _verdict(unit, extraction, "preferred", 0.9,
                            f"Payment terms of net {net} meet the playbook's "
                            "preferred position (PB-PAY-01).",
                            ["PB-PAY-01"])
        return _verdict(unit, extraction, "review_required", 0.55,
                        "Payment timing cannot be determined from the clause text; "
                        "counsel must read it (PB-PAY-02).",
                        ["PB-PAY-02"])

    if t == "governing_law":
        jurisdiction = str(s.get("jurisdiction") or "").lower()
        if s.get("foreign_law"):
            return _verdict(unit, extraction, "review_required", 0.9,
                            "Foreign governing law is a playbook review trigger "
                            "(PB-LAW-02).",
                            ["PB-LAW-02"])
        if "delaware" in jurisdiction or "new york" in jurisdiction:
            return _verdict(unit, extraction, "preferred", 0.92,
                            "Governing law is a playbook-preferred jurisdiction "
                            "(Delaware / New York) (PB-LAW-01).",
                            ["PB-LAW-01"])
        if jurisdiction:
            return _verdict(unit, extraction, "acceptable_fallback", 0.82,
                            f"Governing law ({jurisdiction}) is another US "
                            "jurisdiction — an acceptable fallback (PB-LAW-02).",
                            ["PB-LAW-02"])
        return _verdict(unit, extraction, "review_required", 0.55,
                        "The governing jurisdiction cannot be determined from the "
                        "clause text; counsel must read it (PB-LAW-02).",
                        ["PB-LAW-02"])

    if t == "data_protection":
        hours = s.get("breach_notice_hours")
        if s.get("dpa_referenced") and hours is not None and hours <= 72:
            return _verdict(unit, extraction, "preferred", 0.9,
                            "A data processing addendum is incorporated and breach "
                            "notice is within 72 hours — the preferred position "
                            "(PB-DP-01).",
                            ["PB-DP-01"])
        if s.get("dpa_referenced") or s.get("security_measures"):
            return _verdict(unit, extraction, "acceptable_fallback", 0.8,
                            "Data protection terms exist but the breach-notice "
                            "period or addendum is below the preferred standard — "
                            "an acceptable fallback (PB-DP-01).",
                            ["PB-DP-01"])
        return _verdict(unit, extraction, "review_required", 0.65,
                        "The clause touches personal data without a data "
                        "processing addendum the rules can find (PB-DP-02).",
                        ["PB-DP-02"])

    if t == "limitation_of_warranty":
        months = s.get("warranty_months")
        if months is not None and months > 12:
            return _verdict(unit, extraction, "review_required", 0.85,
                            f"A warranty period of {months} months exceeds the "
                            "playbook's twelve months (PB-WAR-01).",
                            ["PB-WAR-01"])
        return _verdict(unit, extraction, "preferred", 0.82,
                        "Warranty scope and period sit within the playbook's "
                        "standard (PB-WAR-01).",
                        ["PB-WAR-01"])

    if t == "assignment":
        if s.get("free_assignment"):
            return _verdict(unit, extraction, "review_required", 0.87,
                            "The counterparty may assign freely"
                            + (" including on a change of control" if s.get("change_of_control") else "")
                            + " — a playbook review trigger (PB-ASG-02).",
                            ["PB-ASG-02"])
        if s.get("consent_required"):
            return _verdict(unit, extraction, "preferred", 0.9,
                            "Assignment requires prior written consent — the "
                            "preferred position (PB-ASG-01).",
                            ["PB-ASG-01"])
        if s.get("affiliates_only"):
            return _verdict(unit, extraction, "acceptable_fallback", 0.8,
                            "Assignment is limited to affiliates — an acceptable "
                            "fallback (PB-ASG-01).",
                            ["PB-ASG-01"])
        return _verdict(unit, extraction, "review_required", 0.55,
                        "The assignment terms cannot be determined from the clause "
                        "text; counsel must read it (PB-ASG-02).",
                        ["PB-ASG-02"])

    # "other" — the rules have no matrix row; a human (or a confident
    # LLM cross-check) must place it. Low rule confidence is deliberate:
    # it is the case the cross-check adoption path exists for. The one
    # exception is the unheaded preamble (parties and recitals): it
    # carries no operative terms for the playbook to place, so it is
    # recorded as needing no position rather than flagged.
    if not unit.heading and not unit.ref:
        return _verdict(unit, extraction, "preferred", 0.6,
                        "Preamble/recitals: no operative terms for the playbook "
                        "to place; counsel sees the full text in the packet "
                        "(PB-GEN-01).",
                        ["PB-GEN-01"])
    return _verdict(unit, extraction, "review_required", 0.5,
                    "The clause does not match a standard playbook category; it is "
                    "flagged for counsel rather than guessed at (PB-GEN-01).",
                    ["PB-GEN-01"])
