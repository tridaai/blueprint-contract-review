"""CLI — batch-style runner over sample contracts (for the traced
single-contract walkthrough, use the demo: ``python -m contract_agent demo``).

Usage:
    contract-agent                       # first bundled sample contract
    contract-agent --all                 # every bundled sample contract
    contract-agent --index 2             # a specific bundled sample
    contract-agent --file data/contracts.json --all
    contract-agent --all --concurrency 4 # the batch, four at a time

The default data source is the sample set bundled inside the package, so
this works identically from a source checkout and a pip install.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import silence_langchain_deprecation_warnings

# Must precede the graph import: the warning fires while langgraph loads.
silence_langchain_deprecation_warnings()

from .events import PrintingSink
from .graph import run_contract
from .ports import ModelBackend, Retriever
from .samples import load_sample_contracts
from .schemas import AgentResult, ContractInput
from .service import BatchItem
from .store import InMemoryStore
from .wiring import build_backend, build_retriever, build_service_from_env


def _load_contracts(path: Path | None) -> list[ContractInput]:
    if path is None:
        return [ContractInput.model_validate(item) for item in load_sample_contracts()]
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [ContractInput.model_validate(item) for item in raw]


def _render_result(contract: ContractInput, result: AgentResult) -> None:
    f = result.facts
    print("=" * 72)
    print(f"Contract {result.contract_id}: {contract.counterparty_name} ({contract.doc_type} — {contract.title or 'untitled'})")
    print(f"Posture   : {f.posture} (worst position {f.worst_position})")
    print("Clauses   :")
    for v in result.clause_verdicts:
        escalated = " [escalated by cross-check disagreement]" if v.escalated else ""
        refs = " ".join(f"[{r}]" for r in v.playbook_refs)
        print(f"  {v.clause_id} [{v.clause_type}] {v.position} {refs}{escalated}")
    if f.missing_standard_clauses:
        print(f"Missing   : standard clauses absent: {', '.join(f.missing_standard_clauses)}"
              + (" — CRITICAL" if f.critical_absences else ""))
    if result.injection_flags:
        print(f"Screening : {len(result.injection_flags)} injection flag(s) — flagged text kept out of prompts and drafts")
    print(f"Playbook  : {[g.playbook_id for g in result.playbook]}")
    print(f"Assessment: {result.assessment.summary} (composed by: {result.assessment.source})")
    recommended = next((o for o in result.options if o.recommended), None)
    if recommended is not None:
        print(f"Next step : recommended [{recommended.kind}] for clause {recommended.clause_id} — {recommended.title} (score {recommended.score}) of {len(result.options)} scored option(s)")
    if result.verification is not None:
        verdict = "grounded" if result.verification.grounded else "NOT GROUNDED"
        issues = f" issues={result.verification.issues}" if result.verification.issues else ""
        print(f"Self-check: {verdict} ({result.verification.source}){issues}")
    if result.review is not None:
        findings = f" findings={result.review.findings}" if result.review.findings else ""
        blocked = " — REVIEWER BLOCKED (flagged for counsel)" if result.reviewer_blocked else ""
        print(f"Review    : {result.review.verdict.upper()} ({result.review.source}){findings}{blocked}")
    if result.validation is not None:
        failed = [c.name for c in result.validation.checks if not c.passed]
        print(f"Guardrails: passed={result.validation.passed}" + (f" failed={failed}" if failed else ""))
    if result.repair_attempted:
        print(f"Repair    : attempted ({result.repair_attempts}) — {'repaired' if result.repaired else 'still failing'}")
    print(f"Gate      : {result.status} | external action taken: {result.external_action_taken}")
    label = "eligible for auto-triage" if result.autonomy.eligible_for_auto_triage else "counsel decision required"
    print(f"Autonomy  : {label} (recommendation only) — {'; '.join(result.autonomy.reasons)}")
    if result.needs_information and result.information_request is not None:
        print(f"Info needed: information request composed (not sent) — missing: {'; '.join(result.information_request.missing_items)}")
    if result.telemetry is not None:
        tel = result.telemetry
        budget_text = ""
        if tel.budget is not None:
            budget_text = f" · token budget {tel.budget['used']}/{tel.budget['limit']}" + (
                " EXCEEDED (later steps degraded)" if tel.budget["exceeded"] else ""
            )
        if tel.input_tokens is not None:
            cost = f" · est. ${tel.estimated_cost_usd:.4f}" if tel.estimated_cost_usd is not None else " · cost n/a"
            print(f"Telemetry : {tel.backend} · {tel.model} · {tel.model_calls} model call(s) · {tel.input_tokens} in / {tel.output_tokens} out tokens{cost} · {tel.latency_seconds}s{budget_text}")
        else:
            print(f"Telemetry : {tel.backend} (offline fallback — no model ran) · {tel.model_calls} call(s) · tokens n/a · {tel.latency_seconds}s{budget_text}")
    print("-" * 72)
    if result.drafts is not None:
        print(f"Subject: {result.drafts.subject}")
        print(result.drafts.counterparty_response)
    print()


def _run_batch(contracts: list[ContractInput], concurrency: int) -> list[BatchItem]:
    """The concurrent batch path: the service layer with an in-memory
    store (the CLI persists nothing), each item rendered as it lands.
    Configuration was already resolved by main() before this runs, so a
    misconfigured backend fails loudly there, not per item here."""
    service = build_service_from_env(store=InMemoryStore(), checkpointer=False)
    items = service.analyze_batch(contracts, concurrency=concurrency)
    for contract, item in zip(contracts, items):
        if item.error:
            print("=" * 72)
            print(f"Contract {item.contract_id}: ERROR — {item.error}")
            print()
        elif item.result is not None:
            _render_result(contract, item.result)
    return items


def _print_batch_summary(items: list[BatchItem], concurrency: int, wall_clock: float) -> None:
    print("=" * 72)
    print(
        f"Batch summary: {len(items)} contract(s) · concurrency {concurrency} "
        f"· wall-clock {wall_clock:.2f}s"
    )
    for item in items:
        if item.error:
            print(f"  {item.contract_id}: ERROR — {item.error}")
        elif item.result is not None:
            result = item.result
            print(
                f"  {item.contract_id}: posture {result.facts.posture} "
                f"(worst {result.facts.worst_position}) — {result.status}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Contract Review Agent CLI (offline, synthetic data)")
    parser.add_argument("--file", type=Path, default=None, help="Path to a contracts JSON file (default: bundled samples)")
    parser.add_argument("--index", type=int, default=0, help="Which sample contract to run")
    parser.add_argument("--all", action="store_true", help="Run every sample contract")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="With --all: analyse N contracts concurrently (default 1 = sequential, unchanged)",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Print the run's structured events live as it executes (sequential path; the rendered result follows as usual)",
    )
    args = parser.parse_args()

    # Backend + retriever come from the environment (.env loaded by the
    # factories). A misconfiguration (e.g. MODEL_BACKEND=openai with no
    # key) fails loudly with the actionable message, not a traceback —
    # and so does a provider failure mid-run (e.g. Ollama not running):
    # provider errors arrive pre-translated via errors.ProviderError.
    try:
        backend = build_backend()
        retriever = build_retriever()
        contracts = _load_contracts(args.file)
        selected = contracts if args.all else [contracts[args.index]]
        started = time.perf_counter()
        if args.all and args.concurrency > 1:
            items = _run_batch(selected, args.concurrency)
        else:
            items = []
            sink = PrintingSink() if args.stream else None
            for contract in selected:
                result = run_contract(
                    contract, backend=backend, retriever=retriever, event_sink=sink
                )
                _render_result(contract, result)
                items.append(
                    BatchItem(contract_id=contract.contract_id, result=result)
                )
        if args.all:
            _print_batch_summary(items, args.concurrency, time.perf_counter() - started)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
