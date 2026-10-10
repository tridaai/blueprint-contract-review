"""Opt-in LLM-mode evaluation pack (NOT part of the default gate).

Runs the bundled samples through the pipeline with the configured
provider backend, then judges the drafted packet + counterparty
response against the verified facts with an independent judge prompt
(``JUDGE_*`` in ``prompts.py``): groundedness, invented terms, and
prohibited content — precisely defined in the judge prompt as
commitments and conclusions stated as decided (a legal conclusion,
agreement language, an acceptability pronouncement, a promised
outcome). Proposals and requests in a draft-for-counsel letter do
not count; see the definition note in ``JUDGE_SYSTEM_PROMPT``.

This pack needs a real provider. Without one it exits 2, loudly —
it never silently falls back to the mock and reports that as an LLM
result:

    MODEL_BACKEND=openai OPENAI_API_KEY=... python evals/run_llm_evals.py

Exit codes: 0 = judged, mean groundedness score >= 0.8; 1 = judged,
below threshold; 2 = no provider configured.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from contract_agent.config import env_str, load_dotenv  # noqa: E402
from contract_agent.model_backends import clause_rows_text, get_backend, parse_json_object  # noqa: E402
from contract_agent.prompts import JUDGE_SYSTEM_PROMPT, JUDGE_USER_TEMPLATE  # noqa: E402
from contract_agent.samples import sample_contract_models  # noqa: E402
from contract_agent.service import ContractService  # noqa: E402
from contract_agent.store import format_position_counts  # noqa: E402

THRESHOLD = 0.8


def _provider_configured() -> bool:
    load_dotenv()
    backend = (env_str("MODEL_BACKEND") or "mock").lower()
    if backend == "mock":
        return False
    if backend == "ollama":
        return True
    if backend == "anthropic":
        return bool(env_str("ANTHROPIC_API_KEY"))
    return bool(env_str("OPENAI_API_KEY"))


def main() -> int:
    if not _provider_configured():
        print(
            "LLM eval pack: NO PROVIDER CONFIGURED.\n"
            "Set MODEL_BACKEND (openai | anthropic | ollama) and the matching\n"
            "key (or a local Ollama server) — see .env.example. The pack refuses\n"
            "to judge mock drafts and report that as an LLM result.",
            file=sys.stderr,
        )
        return 2
    backend = get_backend()
    service = ContractService(backend=backend, checkpointer=False)
    rows: list[dict] = []
    for contract in sample_contract_models():
        result = service.analyze(contract)
        recommended = next((o for o in result.options if o.recommended), None)
        verdicts = result.clause_verdicts
        facts_rows = [
            {
                "clause_id": v.clause_id,
                "ref": "",
                "heading": "",
                "clause_type": v.clause_type,
                "position": v.position,
                "refs": v.playbook_refs,
                "rationale": v.rationale,
                "fallback": v.fallback_language,
            }
            for v in verdicts
        ]
        user = JUDGE_USER_TEMPLATE.format(
            posture=result.facts.posture,
            clause_table=clause_rows_text(facts_rows),
            missing_clauses=", ".join(result.facts.missing_standard_clauses) or "none",
            citations=", ".join(
                sorted({ref for v in verdicts for ref in v.playbook_refs})
            ) or "none",
            recommended_option=recommended.title if recommended else "none",
            subject=result.drafts.subject if result.drafts else "",
            review_packet=result.drafts.review_packet if result.drafts else "",
            counterparty_response=result.drafts.counterparty_response if result.drafts else "",
        )
        text, _usage = backend.complete_with_usage(JUDGE_SYSTEM_PROMPT, user, max_tokens=400)
        judged = parse_json_object(text) or {}
        rows.append(
            {
                "contract_id": contract.contract_id,
                "grounded": bool(judged.get("grounded")),
                "invented_terms": bool(judged.get("invented_terms")),
                "prohibited_promise": bool(judged.get("prohibited_promise")),
                "score": float(judged.get("score") or 0.0),
                "rationale": str(judged.get("rationale") or "")[:200],
            }
        )
    scores = [r["score"] for r in rows]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "backend": getattr(backend, "name", "unknown"),
        "judged": len(rows),
        "mean_score": round(sum(scores) / len(scores), 4) if scores else 0.0,
        "grounded_rate": round(sum(1 for r in rows if r["grounded"]) / len(rows), 4) if rows else 0.0,
        "prohibited_rate": round(sum(1 for r in rows if r["prohibited_promise"]) / len(rows), 4) if rows else 0.0,
        "threshold": THRESHOLD,
        "rows": rows,
    }
    out = Path(__file__).resolve().parent / "llm_results.json"
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0 if payload["mean_score"] >= THRESHOLD else 1


if __name__ == "__main__":
    raise SystemExit(main())
