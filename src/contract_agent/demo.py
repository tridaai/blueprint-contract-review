"""One-command demo: ONE contract, end to end, with a readable trace.

    contract-agent-demo            (console script)
    python -m contract_agent demo  (module form)
    make demo

Default: offline, deterministic mock backend, synthetic sample data. Set
MODEL_BACKEND=openai|anthropic (plus the API key, e.g. in .env) and the
same trace runs against the real provider — the header line says which
backend produced the drafts. The trace shows every stage a reviewer needs
to judge the blueprint: input → segmentation → extraction → injection
screening → per-clause playbook verdicts with cross-check → retrieved
playbook → assessment → scored options → drafts → self-verification →
independent review → guardrail checks → the counsel gate it stops at.
"""

from __future__ import annotations

import argparse
import sys

from .config import silence_langchain_deprecation_warnings

# Must precede the graph import: the warning fires while langgraph loads.
silence_langchain_deprecation_warnings()

from .graph import run_contract
from .ports import ModelBackend, Retriever
from .samples import sample_contract_models
from .wiring import build_backend, build_retriever

_LINE = "=" * 74


def print_trace(
    index: int = 0,
    backend: ModelBackend | None = None,
    retriever: Retriever | None = None,
) -> None:
    backend = backend or build_backend()
    retriever = retriever or build_retriever()
    contracts = sample_contract_models()
    contract = contracts[index]
    result = run_contract(contract, backend=backend, retriever=retriever)
    backend_desc = (
        "offline mock backend"
        if backend.name == "mock"
        else f"{backend.name} backend via MODEL_BACKEND (real provider calls)"
    )

    print(_LINE)
    print("TRIDA AI BLUEPRINT DEMO — Contract Review Agent")
    print(f"Reference prototype · synthetic data · {backend_desc} · no external action")
    print(_LINE)

    print("\n[1] INPUT CONTRACT")
    print(f"    ID          : {contract.contract_id}")
    print(f"    Counterparty: {contract.counterparty_name} · Our role: {contract.our_role}")
    print(f"    Type / title: {contract.doc_type} / {contract.title or 'untitled'}")
    print(f"    Documents   : {[d.document_id or d.doc_type for d in contract.documents] or 'none'}")
    for flag in result.injection_flags:
        print(f"    ⚠ Injection screen: FLAG — {flag}")
    if result.injection_flags:
        print("      (flagged sentences are kept out of the drafts and the prompts; the clause is still reviewed on substance)")
    f = result.facts
    print(f"    Computed    : {f.clause_count} clause(s) · posture {f.posture} · worst position {f.worst_position}")
    if f.missing_standard_clauses:
        print(f"    Missing     : standard clauses absent: {', '.join(f.missing_standard_clauses)}"
              + (f" (CRITICAL: {', '.join(f.critical_absences)})" if f.critical_absences else ""))

    print("\n[2] CLAUSE-BY-CLAUSE PLAYBOOK REVIEW (rules + LLM cross-check)")
    units = {u.clause_id: u for u in result.clauses}
    for v in result.clause_verdicts:
        unit = units.get(v.clause_id)
        label = f"§{unit.ref} {unit.heading}" if unit and (unit.ref or unit.heading) else v.clause_id
        cross = f"rules={v.rule_position} · resolution={v.resolution}"
        if v.llm_position:
            cross += f" · llm={v.llm_position}"
        if v.escalated:
            cross += " · ESCALATED"
        print(f"    {v.clause_id} [{label}] {v.clause_type}: {v.position.upper()} ({cross})")
        print(f"        {v.rationale}")

    print("\n[3] RETRIEVED PLAYBOOK CONTEXT")
    for g in result.playbook:
        snippet = g.text if len(g.text) <= 160 else g.text[:157] + "..."
        print(f"    [{g.playbook_id}] {g.title} (score {g.score}, via {'+'.join(g.sources) or 'keyword'})")
        print(f"        {snippet}")
    if not result.playbook:
        print("    (no playbook entries retrieved)")

    print("\n[4] DOCUMENT ASSESSMENT")
    print(f"    Summary   : {result.assessment.summary} (composed by: {result.assessment.source})")
    for factor in result.assessment.risk_factors:
        print(f"      - risk factor: {factor}")
    for call in result.assessment.tool_calls:
        print(f"      - tool call: {call.get('name')} — {call.get('summary')}")
    if result.assessment.note:
        print(f"    Note      : {result.assessment.note}")
    for line in result.assessment.evidence:
        print(f"      - evidence: {line}")

    print("\n[5] NEXT-STEP OPTIONS (scored by code — the model never does this arithmetic)")
    for option in result.options:
        mark = " <- recommended" if option.recommended else ""
        print(f"    [clause {option.clause_id}] [{option.kind}] {option.title} — score {option.score}{mark}")
    if not result.options:
        print("    (no flagged clauses — no options needed)")

    print("\n[6] DRAFT — REVIEW PACKET (for counsel)")
    if result.drafts is not None:
        print(f"    Subject: {result.drafts.subject}")
        for line in result.drafts.review_packet.splitlines():
            print(f"    {line}")

    print("\n[7] DRAFT — COUNTERPARTY RESPONSE (proposes changes; asserts nothing as agreed)")
    if result.drafts is not None:
        for line in result.drafts.counterparty_response.splitlines():
            print(f"    {line}")

    print("\n[8] SELF-VERIFICATION — the agent critiques its own drafts")
    if result.verification is not None:
        verdict = "GROUNDED" if result.verification.grounded else "NOT GROUNDED"
        source = "LLM critique" if result.verification.source == "llm" else "deterministic checklist"
        print(f"    verdict: {verdict} ({source})")
        for issue in result.verification.issues:
            print(f"    issue: {issue}")
        if result.verification.note:
            print(f"    note: {result.verification.note}")

    print("\n[9] INDEPENDENT REVIEW — a second pair of eyes (not the drafter)")
    if result.review is not None:
        r = result.review
        source = "LLM reviewer" if r.source == "llm" else "deterministic checklist"
        print(f"    verdict: {r.verdict.upper()} ({source})")
        for finding in r.findings:
            print(f"    finding: {finding}")
        if result.reviewer_blocked:
            print("    ⛔ REVIEWER BLOCKED this draft — flagged for counsel; the human still decides.")
    else:
        print("    (disabled — REVIEWER=off)")

    print("\n[10] GUARDRAIL CHECKS")
    if result.validation is not None:
        for check in result.validation.checks:
            mark = "PASS" if check.passed else "FAIL"
            print(f"    [{mark}] {check.name} — {check.detail}")
        print(f"    Overall: {'PASSED' if result.validation.passed else 'FAILED'}")
    if result.repair_attempted:
        outcome = "redraft passed" if result.repaired else "redraft still failed"
        print(f"    Repair: bounded repair attempted ({result.repair_attempts} attempt(s)) — {outcome}")
        if result.original_validation is not None:
            for failed in [c for c in result.original_validation.checks if not c.passed]:
                print(f"    Original failure: {failed.name} — {failed.detail}")

    print("\n[11] FINAL STATE")
    print("    AWAITING_COUNSEL" if result.status == "awaiting_counsel" else f"    {result.status}")
    print(f"    status = {result.status} · external_action_taken = {result.external_action_taken}")
    label = "eligible for auto-triage" if result.autonomy.eligible_for_auto_triage else "counsel decision required"
    print(f"    Autonomy recommendation: {label} (recommendation only — never acted on)")
    for reason in result.autonomy.reasons:
        print(f"      - {reason}")
    for blocker in result.autonomy.blockers:
        print(f"      - blocker: {blocker}")
    if result.needs_information and result.information_request is not None:
        print("    Information : NEEDED — a request to the counterparty was composed (not sent):")
        for item in result.information_request.missing_items:
            print(f"      - missing: {item}")
    if result.telemetry is not None:
        tel = result.telemetry
        budget_text = ""
        if tel.budget is not None:
            budget_text = f" · token budget {tel.budget['used']}/{tel.budget['limit']}" + (
                " — EXCEEDED (later provider steps degraded to their template paths)"
                if tel.budget["exceeded"]
                else ""
            )
        if tel.input_tokens is not None:
            cost = (
                f" · est. ${tel.estimated_cost_usd:.4f}"
                if tel.estimated_cost_usd is not None
                else " · cost n/a (model not in the price table)"
            )
            print(
                f"    Telemetry : {tel.backend} · {tel.model} · {tel.model_calls} model call(s) · "
                f"{tel.input_tokens} in / {tel.output_tokens} out tokens{cost} · {tel.latency_seconds}s{budget_text}"
            )
        else:
            print(
                f"    Telemetry : {tel.backend} (offline fallback — no model ran) · "
                f"{tel.model_calls} call(s) · tokens n/a · {tel.latency_seconds}s{budget_text}"
            )
    print("    Nothing was sent, signed, agreed, or filed. Counsel decides via the API/UI.")
    print(_LINE)


def main() -> None:
    parser = argparse.ArgumentParser(description="Contract Review Agent — traced demo (offline)")
    parser.add_argument("--index", type=int, default=0, help="Which bundled sample contract to run (0-based)")
    args = parser.parse_args()
    try:
        print_trace(args.index)
    except (RuntimeError, ValueError) as exc:
        # e.g. MODEL_BACKEND=openai with no key — fail loudly and cleanly.
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
