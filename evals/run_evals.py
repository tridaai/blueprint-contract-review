"""Golden-dataset evaluation for the Contract Review Agent.

Runs the deterministic pipeline (playbook rules, offline fallback
backend) over ``golden.jsonl`` and reports clause-level accuracy
(clause type + playbook position) and document-level accuracy
(posture, per-type positions, missing-clause detection, injection
flags, information requests, repairs). All cases are synthetic. Run
from the repo root:

    python evals/run_evals.py

Exit code is 0 when overall accuracy >= 95%, 1 otherwise (the gate).
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from contract_agent.clause_review import review_clause  # noqa: E402
from contract_agent.extractor import extract_clauses  # noqa: E402
from contract_agent.graph import run_contract  # noqa: E402
from contract_agent.samples import sample_contract_models  # noqa: E402
from contract_agent.schemas import ContractInput, DocumentInput  # noqa: E402
from contract_agent.segmentation import segment_text  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden.jsonl"
THRESHOLD = 0.95


def _write_results(payload: dict) -> None:
    """Emit results.json next to this script (and into the package, when
    the source tree is present) so the demo console can display it."""
    targets = [Path(__file__).resolve().parent / "results.json"]
    packaged = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "contract_agent"
        / "data"
        / "eval_results.json"
    )
    if packaged.parent.is_dir():
        targets.append(packaged)
    for target in targets:
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _eval_clause(case: dict) -> tuple[bool, str]:
    units = segment_text(case["text"])
    if not units:
        return False, "no clause units segmented"
    extractions = extract_clauses(units, backend=object())
    verdict = review_clause(units[0], extractions[0])
    problems = []
    if extractions[0].clause_type != case["expected_type"]:
        problems.append(
            f"type: expected {case['expected_type']}, got {extractions[0].clause_type}"
        )
    if verdict.position != case["expected_position"]:
        problems.append(
            f"position: expected {case['expected_position']}, got {verdict.position}"
        )
    return not problems, "; ".join(problems)


def _eval_document(case: dict, samples: dict[str, ContractInput]) -> tuple[bool, str]:
    contract = samples.get(case["sample_id"])
    if contract is None:
        return False, f"unknown sample {case['sample_id']}"
    result = run_contract(contract)
    problems = []
    if result.facts.posture != case["expected_posture"]:
        problems.append(
            f"posture: expected {case['expected_posture']}, got {result.facts.posture}"
        )
    by_type = {v.clause_type: v.position for v in result.clause_verdicts}
    for clause_type, position in (case.get("expected_positions") or {}).items():
        if by_type.get(clause_type) != position:
            problems.append(
                f"{clause_type}: expected {position}, got {by_type.get(clause_type)}"
            )
    if case.get("expected_missing") is not None:
        if sorted(result.facts.missing_standard_clauses) != sorted(case["expected_missing"]):
            problems.append(
                f"missing: expected {case['expected_missing']}, "
                f"got {result.facts.missing_standard_clauses}"
            )
    if case.get("expected_flags") is not None:
        if bool(result.injection_flags) != case["expected_flags"]:
            problems.append(
                f"flags: expected {case['expected_flags']}, got {bool(result.injection_flags)}"
            )
    if case.get("expected_needs_information") is not None:
        if result.needs_information != case["expected_needs_information"]:
            problems.append(
                f"needs_information: expected {case['expected_needs_information']}, "
                f"got {result.needs_information}"
            )
    if case.get("expected_repaired") is not None:
        if result.repaired != case["expected_repaired"]:
            problems.append(
                f"repaired: expected {case['expected_repaired']}, got {result.repaired}"
            )
    return not problems, "; ".join(problems)


def evaluate(cases: list[dict]) -> dict:
    samples = {c.contract_id: c for c in sample_contract_models()}
    correct = 0
    per_kind_total: dict[str, int] = defaultdict(int)
    per_kind_correct: dict[str, int] = defaultdict(int)
    failures: list[str] = []
    for case in cases:
        kind = case.get("kind", "clause")
        per_kind_total[kind] += 1
        if kind == "clause":
            ok, detail = _eval_clause(case)
        else:
            ok, detail = _eval_document(case, samples)
        if ok:
            correct += 1
            per_kind_correct[kind] += 1
        else:
            failures.append(f"{case['case_id']}: {detail}")
    total = len(cases)
    return {
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total, 4) if total else 0.0,
        "per_kind": {
            kind: {
                "total": per_kind_total[kind],
                "correct": per_kind_correct[kind],
                "accuracy": round(per_kind_correct[kind] / per_kind_total[kind], 4),
            }
            for kind in sorted(per_kind_total)
        },
        "failures": failures,
    }


def main() -> int:
    cases = [
        json.loads(line)
        for line in GOLDEN.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    scored = evaluate(cases)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "backend": "mock (offline fallback)",
        "total": scored["total"],
        "correct": scored["correct"],
        "accuracy": scored["accuracy"],
        "per_kind": scored["per_kind"],
        "threshold": THRESHOLD,
        "passed": scored["accuracy"] >= THRESHOLD,
        "failures": scored["failures"],
    }
    _write_results(payload)
    print(json.dumps(payload, indent=2))
    if scored["failures"]:
        print("\nFailures:", file=sys.stderr)
        for failure in scored["failures"]:
            print(f"  - {failure}", file=sys.stderr)
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
