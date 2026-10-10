"""Independent review node logic: a second pair of eyes on the drafts.

The reviewer is deliberately NOT the drafter: in provider mode it runs
the reviewer persona (optionally a different model, ``REVIEWER_MODEL``)
over checks the drafter never sees — review-packet completeness above
all — and in every mode a disjoint deterministic checklist guards
packet structure:

- the packet records the assessment summary;
- the packet carries the clause-by-clause table;
- the packet cites at least one retrieved playbook entry;
- fallback language is present for the flagged clauses that have it;
- the packet names a recommended option;
- the counterparty draft states the next step;
- the packet cites exactly the retrieved entries (no strays).

A ``block`` verdict flags the run (``reviewer_blocked``): the draft
still reaches counsel, but the flag is prominent and the case is never
eligible for an auto-triage recommendation. Set ``REVIEWER=off`` to
disable the node.
"""

from __future__ import annotations

import re

from .config import env_str, load_dotenv
from .model_backends import DraftContext
from .schemas import (
    Assessment,
    ClauseOption,
    ClauseVerdict,
    ContractFacts,
    ContractInput,
    Drafts,
    ReviewResult,
)

_CITATION = re.compile(r"\[(PB-[A-Z]+-\d+)\]")


def reviewer_enabled() -> bool:
    load_dotenv()
    return env_str("REVIEWER", "on").strip().lower() not in {"off", "0", "false", "no"}


def packet_contents(
    *,
    drafts: Drafts,
    assessment: Assessment | None,
    verdicts: list[ClauseVerdict],
    options: list[ClauseOption],
    retrieved_ids: list[str],
) -> dict:
    """The packet facts the reviewer judges — computed in code, from
    the structured assessment/options, not re-parsed from prose."""
    packet = drafts.review_packet or ""
    recommended = next((o for o in options if o.recommended), None)
    flagged = [v for v in verdicts if v.position != "preferred"]
    table_present = all(v.clause_id in packet for v in verdicts) if verdicts else False
    fallbacks_present = all(
        (not v.fallback_language) or (v.fallback_language in packet)
        for v in flagged
    )
    return {
        "packet_summary_present": bool(assessment and assessment.summary),
        "packet_table_present": table_present,
        "packet_fallbacks_present": fallbacks_present,
        "packet_citations": sorted(set(_CITATION.findall(packet))),
        "packet_options": [o.kind for o in options],
        "packet_recommended": recommended.title if recommended else None,
        # Options exist only for flagged clauses; an aligned contract
        # legitimately recommends nothing.
        "options_expected": bool(flagged),
        "assessment_citations": list(retrieved_ids),
    }


def checklist_review(
    *, drafts: Drafts, contents: dict, retrieved_ids: list[str]
) -> tuple[str, list[str]]:
    """Deterministic packet-structure review. Returns (verdict, findings)."""
    findings: list[str] = []
    blockers: list[str] = []
    if not contents["packet_summary_present"]:
        blockers.append("the review packet records no assessment summary")
    if not contents["packet_table_present"]:
        blockers.append("the review packet does not carry the full clause table")
    if not contents["packet_citations"]:
        blockers.append("the review packet cites no playbook entry")
    if contents.get("options_expected") and not contents["packet_recommended"]:
        blockers.append("the packet names no recommended option")
    if not contents["packet_fallbacks_present"]:
        findings.append("fallback language is missing for a flagged clause that has it")
    if "next step" not in (drafts.counterparty_response or "").lower():
        findings.append("the counterparty draft does not state the next step")
    for citation in contents["packet_citations"]:
        if citation not in retrieved_ids:
            findings.append(f"packet cites playbook entry {citation}, which was not retrieved")
    for citation in contents["assessment_citations"]:
        if citation not in contents["packet_citations"]:
            findings.append(f"assessment cites {citation} but the packet does not carry it")
    if blockers:
        return "block", blockers + findings
    if findings:
        return "concerns", findings
    return "pass", ["packet structure complete: clause table, citations, fallbacks, and a recommendation are all present"]


def review_drafts(
    *,
    contract: ContractInput,
    facts: ContractFacts,
    clause_rows: list[dict],
    assessment: Assessment | None,
    verdicts: list[ClauseVerdict],
    options: list[ClauseOption],
    drafts: Drafts,
    retrieved_ids: list[str],
    backend,
) -> ReviewResult:
    """Review a draft pair: LLM reviewer in provider mode, checklist otherwise."""
    if not reviewer_enabled():
        return ReviewResult(verdict="skipped", source="checklist", enabled=False)
    contents = packet_contents(
        drafts=drafts, assessment=assessment, verdicts=verdicts,
        options=options, retrieved_ids=retrieved_ids,
    )
    verdict, findings = checklist_review(
        drafts=drafts, contents=contents, retrieved_ids=retrieved_ids
    )
    review_fn = getattr(backend, "review_draft", None)
    if review_fn is None:
        return ReviewResult(verdict=verdict, findings=findings, source="checklist")
    context = DraftContext(
        contract_id=contract.contract_id,
        counterparty_name=contract.counterparty_name,
        our_role=contract.our_role,
        doc_type=contract.doc_type,
        title=contract.title,
        posture=facts.posture,
        positions_summary=", ".join(
            f"{k}: {v}" for k, v in sorted(facts.positions_count.items())
        ) or "none",
        clause_rows=clause_rows,
        missing_clauses=facts.missing_standard_clauses,
        discrepancies=facts.discrepancies,
        cover_text="",
        playbook_ids=retrieved_ids,
        assessment_summary=assessment.summary if assessment else "",
        assessment_citations=contents["assessment_citations"],
        packet_table_present=contents["packet_table_present"],
        packet_citations=contents["packet_citations"],
        packet_fallbacks_present=contents["packet_fallbacks_present"],
        packet_options=contents["packet_options"],
        packet_recommended=contents["packet_recommended"],
        subject=drafts.subject,
        review_packet=drafts.review_packet,
        counterparty_response=drafts.counterparty_response,
        playbook_details=[],
    )
    note = ""
    try:
        critique = review_fn(context)
    except Exception as exc:  # review never fails the run
        critique = None
        note = f"LLM review failed ({exc}) — deterministic checklist review used"
    if critique is None:
        return ReviewResult(
            verdict=verdict,
            findings=findings,
            source="checklist",
            note=note or "LLM review reply was unusable — deterministic checklist review used",
        )
    merged = list(dict.fromkeys([*findings, *critique["findings"]]))
    severity = {"pass": 0, "concerns": 1, "block": 2}
    final = max(verdict, critique["verdict"], key=lambda v: severity[v])
    if final == "block" and verdict != "block":
        merged.append("the independent reviewer blocked this draft (see findings)")
    if critique.get("model"):
        merged.append(f"reviewer model: {critique['model']}")
    return ReviewResult(verdict=final, findings=merged, source="llm")
