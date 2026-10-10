"""Assess node logic: a document-level risk summary over the evidence.

The assessment reasons over computed evidence only — the per-clause
playbook verdicts, the position counts and posture, the missing
standard clauses, the extraction cross-check discrepancies, the
retrieved playbook entries, and (when the store has history) the
counterparty memory and counsel-feedback lines. The evidence list is
assembled deterministically in BOTH modes, so the citations counsel
sees are always the real inputs. In provider mode the LLM composes
the risk prose from those facts — agenticly, through a bounded tool
loop when the graph supplies a toolbox (``tools_agent.py``); in the
default mode a deterministic template composes it from the same
evidence structure. An unusable LLM reply falls back to the template,
with the reason recorded in ``note``.

The assessment never states a legal conclusion: it summarises
positions against the playbook. Counsel concludes.
"""

from __future__ import annotations

from .extractor import extraction_discrepancies
from .model_backends import DraftContext, clause_rows_text
from .schemas import Assessment, ClauseExtraction, ContractFacts, ContractInput
from .screening import sanitized_cover
from .store import format_position_counts
from .tools_agent import TOOL_SPECS, AssessmentToolBox, assessment_max_tool_calls


def memory_evidence_lines(history: dict | None) -> list[str]:
    """History as evidence lines, in the assessment's citation style.

    Only real history produces lines — absence is not evidence.
    Feedback lines carry counsel's decisions (with their reasons) on
    earlier contracts with this counterparty, so the pattern of
    oversight itself becomes evidence.
    """
    if not history:
        return []
    lines: list[str] = []
    if history.get("counterparty_count"):
        recent = ", ".join(history.get("counterparty_recent_postures", [])) or "none recorded"
        lines.append(
            f"memory: {history['counterparty_count']} prior contract(s) for this "
            f"counterparty in the stored history (counterparty: "
            f"{history.get('counterparty', '')}; most recent postures: {recent})"
        )
    for index, feedback in enumerate(history.get("feedback", [])):
        when = "last" if index == 0 else "an earlier"
        lines.append(
            f"counsel feedback: {when} decision for this counterparty was "
            f"{feedback.get('decision', '')} — reason: '{feedback.get('reason', '')}'"
        )
    return lines


def build_evidence(
    *,
    facts: ContractFacts,
    verdicts: list,
    extractions: list[ClauseExtraction],
    playbook: list[dict],
    history: dict | None = None,
    discrepancies: list[str] | None = None,
    history_lines: list[str] | None = None,
) -> list[str]:
    """The deterministic evidence list every assessment cites."""
    evidence: list[str] = []
    evidence.append(
        f"computed posture={facts.posture} worst={facts.worst_position} "
        f"positions={format_position_counts(facts.positions_count)} "
        f"(tool: compute_facts)"
    )
    for verdict in verdicts:
        if verdict.position != "preferred":
            evidence.append(
                f"clause {verdict.clause_id} [{verdict.clause_type}]: "
                f"{verdict.position} — {verdict.rationale}"
            )
    if facts.missing_standard_clauses:
        evidence.append(
            "missing standard clauses: " + ", ".join(facts.missing_standard_clauses)
        )
    if facts.critical_absences:
        evidence.append(
            "critical absences: " + ", ".join(facts.critical_absences)
        )
    if discrepancies is None:
        discrepancies = extraction_discrepancies(extractions)
    for line in discrepancies:
        evidence.append(f"extraction cross-check: {line}")
    for entry in playbook:
        evidence.append(f"playbook {entry.get('playbook_id') or entry.get('policy_id')}: {entry['title']}")
    evidence.extend(
        history_lines if history_lines is not None else memory_evidence_lines(history)
    )
    return evidence


def _template_risk_factors(
    *, facts: ContractFacts, verdicts: list, discrepancies: list[str]
) -> list[str]:
    factors: list[str] = []
    for verdict in verdicts:
        if verdict.position in {"walk_away", "review_required"}:
            factors.append(
                f"clause {verdict.clause_id} [{verdict.clause_type}]: {verdict.position}"
            )
    for clause_type in facts.critical_absences:
        factors.append(f"missing critical clause: {clause_type}")
    for clause_type in facts.missing_standard_clauses:
        if clause_type not in facts.critical_absences:
            factors.append(f"missing standard clause: {clause_type}")
    factors.extend(f"extraction discrepancy: {d}" for d in discrepancies)
    if not facts.package_complete:
        factors.append(
            "package incomplete: referenced exhibits missing ("
            + ", ".join(facts.exhibits_missing) + ")"
        )
    return factors


def citations_for(verdicts: list, playbook: list[dict]) -> list[str]:
    cited: list[str] = []
    for verdict in verdicts:
        for ref in verdict.playbook_refs:
            if ref not in cited:
                cited.append(ref)
    for entry in playbook:
        pid = entry.get("playbook_id") or entry.get("policy_id")
        if pid and pid not in cited:
            cited.append(pid)
    return cited


def build_assessment(
    *,
    contract: ContractInput,
    facts: ContractFacts,
    verdicts: list,
    clause_rows: list[dict],
    extractions: list[ClauseExtraction],
    playbook: list[dict],
    backend,
    history: dict | None = None,
    toolbox: AssessmentToolBox | None = None,
    discrepancies: list[str] | None = None,
    history_lines: list[str] | None = None,
) -> Assessment:
    """Compose the assessment: LLM prose in provider mode, template otherwise."""
    # The assessment feeds the counterparty's cover note to the model
    # (provider path); it sees the screened version — sentences the
    # injection screen flagged never reach the prompt.
    cover_text = sanitized_cover(contract)
    if discrepancies is None:
        discrepancies = extraction_discrepancies(extractions)
    evidence = build_evidence(
        facts=facts,
        verdicts=verdicts,
        extractions=extractions,
        playbook=playbook,
        history=history,
        discrepancies=discrepancies,
        history_lines=history_lines,
    )
    risk_factors = _template_risk_factors(
        facts=facts, verdicts=verdicts, discrepancies=discrepancies
    )
    worst_lines = [
        f"clause {v.clause_id} [{v.clause_type}] is {v.position}"
        for v in verdicts
        if v.position in {"walk_away", "review_required"}
    ]
    template = Assessment(
        summary=(
            f"Posture {facts.posture} for {contract.counterparty_name} — "
            f"{contract.doc_type} '{contract.title or 'untitled'}'; "
            f"{facts.clause_count} clause(s) reviewed: "
            f"{format_position_counts(facts.positions_count)}; "
            + ("; ".join(worst_lines) if worst_lines else "no walk-away or review-required clauses")
            + (
                f"; missing standard clauses: {', '.join(facts.missing_standard_clauses)}."
                if facts.missing_standard_clauses
                else "."
            )
        ),
        risk_factors=risk_factors,
        evidence=evidence,
        source="template",
    )
    assess_fn = getattr(backend, "assess", None)
    if assess_fn is None:
        return template
    positions_summary = format_position_counts(facts.positions_count)
    context = DraftContext(
        contract_id=contract.contract_id,
        counterparty_name=contract.counterparty_name,
        our_role=contract.our_role,
        doc_type=contract.doc_type,
        title=contract.title,
        posture=facts.posture,
        positions_summary=positions_summary,
        clause_rows=clause_rows,
        missing_clauses=facts.missing_standard_clauses,
        discrepancies=discrepancies,
        cover_text=cover_text or "none recorded",
        playbook_details=playbook,
    )
    tools_fn = getattr(backend, "assess_with_tools", None)
    if tools_fn is not None and toolbox is not None:
        try:
            llm = tools_fn(
                context, TOOL_SPECS, toolbox.dispatch, assessment_max_tool_calls()
            )
        except Exception as exc:  # the assessment never fails the run
            llm = None
            template.note = (
                f"LLM agentic assessment failed ({exc}) — template assessment used"
            )
        if llm:
            return Assessment(
                summary=llm["summary"],
                risk_factors=llm["risk_factors"],
                evidence=evidence,
                source="llm",
                tool_calls=llm.get("tool_calls", []),
            )
        if not template.note:
            template.note = (
                "LLM agentic assessment reply was unusable — template assessment used"
            )
        return template
    try:
        llm = assess_fn(context)
    except Exception as exc:  # the assessment never fails the run
        llm = None
        template.note = f"LLM assessment failed ({exc}) — template assessment used"
    if not llm:
        if not template.note:
            template.note = "LLM assessment reply was unusable — template assessment used"
        return template
    return Assessment(
        summary=llm["summary"],
        risk_factors=llm["risk_factors"],
        evidence=evidence,
        source="llm",
    )
