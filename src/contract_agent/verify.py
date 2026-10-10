"""Self-verification node logic: the agent critiques its own drafts.

Two paths, one contract (``VerifyResult``):

- **LLM critique** (provider mode): the drafting model is asked to
  check its own drafts against the verified facts — playbook citations
  it was not given, clause verdicts that disagree with the computed
  verdicts, legal conclusions or agreement language. Self-critique
  catches composition errors the deterministic checklist cannot see.
- **Deterministic checklist** (default mode, and the fallback when an
  LLM critique is unusable): citation IDs in the drafts must be among
  the retrieved playbook entries; no dollar figure may appear in the
  counterparty response prose (figures are clause facts for the
  packet, not things the response asserts); agreement/conclusion
  language is flagged; a foreign contract id is flagged.

Verification never blocks a run by itself — it is evidence for
counsel and an input to the autonomy recommendation. The hard stop
is the guardrail layer.
"""

from __future__ import annotations

import re

from .guardrails import _AGREEMENT_PATTERNS, _CONCLUSION_PATTERNS, _affirmative
from .model_backends import DraftContext, clause_rows_text
from .schemas import ContractFacts, ContractInput, Drafts, VerifyResult

_CITATION = re.compile(r"\[(PB-[A-Z]+-\d+)\]")
_MONEY = re.compile(r"\$\s?[\d,]+")
_ID_TOKEN = re.compile(r"\bCTR-\d{3,}\b")


def checklist_grounding(
    *, drafts: Drafts, retrieved_ids: list[str], contract_id: str
) -> list[str]:
    """Deterministic grounding issues for a draft (empty = grounded)."""
    combined = f"{drafts.subject}\n{drafts.counterparty_response}\n{drafts.review_packet}"
    issues: list[str] = []
    for citation in sorted(set(_CITATION.findall(combined))):
        if citation not in retrieved_ids:
            issues.append(
                f"draft cites playbook entry {citation}, which was not retrieved for this contract"
            )
    for amount in sorted(set(_MONEY.findall(drafts.counterparty_response or ""))):
        issues.append(
            f"counterparty draft states a monetary figure ({amount}) — figures are "
            "clause facts for the packet, not terms the response asserts"
        )
    hit = next(
        (h for h in (_affirmative(p, combined) for p in _CONCLUSION_PATTERNS) if h), None
    )
    if hit:
        issues.append(f"draft contains a legal conclusion: '{hit}'")
    hit = next(
        (h for h in (_affirmative(p, drafts.counterparty_response or "") for p in _AGREEMENT_PATTERNS) if h),
        None,
    )
    if hit:
        issues.append(f"counterparty draft contains agreement language: '{hit}'")
    for token in sorted(set(_ID_TOKEN.findall(combined))):
        if token != contract_id:
            issues.append(f"draft references a different contract id ({token})")
    return issues


def verify_drafts(
    *,
    contract: ContractInput,
    facts: ContractFacts,
    clause_rows: list[dict],
    drafts: Drafts,
    retrieved_ids: list[str],
    backend,
) -> VerifyResult:
    """Verify a draft pair: LLM critique in provider mode, checklist otherwise."""
    issues = checklist_grounding(
        drafts=drafts, retrieved_ids=retrieved_ids, contract_id=contract.contract_id
    )
    verify_fn = getattr(backend, "verify_draft", None)
    if verify_fn is None:
        return VerifyResult(grounded=not issues, issues=issues, source="checklist")
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
        subject=drafts.subject,
        review_packet=drafts.review_packet,
        counterparty_response=drafts.counterparty_response,
        playbook_details=[],
    )
    try:
        critique = verify_fn(context)
    except Exception as exc:  # verification never fails the run
        return VerifyResult(
            grounded=not issues,
            issues=issues,
            source="checklist",
            note=f"LLM self-verification failed ({exc}) — deterministic checklist used",
        )
    if critique is None:
        return VerifyResult(
            grounded=not issues,
            issues=issues,
            source="checklist",
            note="LLM self-verification reply was unusable — deterministic checklist used",
        )
    merged = list(dict.fromkeys([*issues, *critique["issues"]]))
    return VerifyResult(
        grounded=critique["grounded"] and not issues,
        issues=merged,
        source="llm",
        note=critique.get("summary", ""),
    )
