"""Golden-dataset evaluation for the Contract Review Agent.

Runs the deterministic pipeline (playbook rules, offline fallback
backend) over ``golden.jsonl`` and reports clause-level accuracy
(clause type + playbook position), document-level accuracy
(posture, per-type positions, missing-clause detection, injection
flags, information requests, repairs), negotiation-round movement,
round-chain histories (re-trades, cumulative movement), and exhibit
consistency checks. All cases are synthetic. Run from the repo root:

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
from contract_agent.round_diff import build_round_diff  # noqa: E402
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


def _eval_round_diff(case: dict, samples: dict[str, ContractInput]) -> tuple[bool, str]:
    """Negotiation-round diffing: run both rounds, align, and compare
    the verdict movement per clause type + the summary counts."""
    prior = samples.get(case["prior_id"])
    current = samples.get(case["sample_id"])
    if prior is None or current is None:
        return False, f"unknown sample {case.get('prior_id')} / {case.get('sample_id')}"
    diff = build_round_diff(run_contract(prior), run_contract(current))
    problems = []
    by_type = {m.clause_type: m for m in diff.movements}
    for clause_type, expected in (case.get("expected_movements") or {}).items():
        entry = by_type.get(clause_type)
        if entry is None:
            problems.append(f"{clause_type}: no movement recorded")
            continue
        for field, key in (("from", "prior_position"), ("to", "position"), ("movement", "movement")):
            if field in expected and getattr(entry, key) != expected[field]:
                problems.append(
                    f"{clause_type} {field}: expected {expected[field]}, "
                    f"got {getattr(entry, key)}"
                )
    for field, key in (
        ("improved", "improved"), ("worsened", "worsened"),
        ("unchanged", "unchanged"), ("new", "new_clauses"), ("removed", "removed_clauses"),
    ):
        expected = (case.get("expected_counts") or {}).get(field)
        if expected is not None and getattr(diff, key) != expected:
            problems.append(f"count {field}: expected {expected}, got {getattr(diff, key)}")
    return not problems, "; ".join(problems)


def _eval_round_chain(case: dict, samples: dict[str, ContractInput]) -> tuple[bool, str]:
    """Negotiation chains: analyse the chain in order through the
    service (the chain lives in the store), then compare the final
    round's chain report — re-trades, cumulative movement, and the
    rounds-to-no-walk-away signal."""
    from contract_agent.service import ContractService
    from contract_agent.store import InMemoryStore

    service = ContractService(store=InMemoryStore(), checkpointer=False)
    result = None
    for contract_id in case["chain_ids"]:
        contract = samples.get(contract_id)
        if contract is None:
            return False, f"unknown sample {contract_id}"
        result = service.analyze(contract)
    chain = result.round_chain if result is not None else None
    if chain is None:
        return False, "no round chain attached to the final round"
    problems = []
    if case.get("expected_rounds") is not None and chain.rounds != case["expected_rounds"]:
        problems.append(f"rounds: expected {case['expected_rounds']}, got {chain.rounds}")
    if case.get("expected_re_trades") is not None and chain.re_trades != case["expected_re_trades"]:
        problems.append(
            f"re_trades: expected {case['expected_re_trades']}, got {chain.re_trades}"
        )
    if case.get("expected_re_traded_types") is not None:
        if sorted(chain.re_traded_clause_types) != sorted(case["expected_re_traded_types"]):
            problems.append(
                f"re_traded types: expected {case['expected_re_traded_types']}, "
                f"got {chain.re_traded_clause_types}"
            )
    cumulative = case.get("expected_cumulative") or {}
    for field, attr in (
        ("improved", "cumulative_improved"),
        ("worsened", "cumulative_worsened"),
        ("unchanged", "cumulative_unchanged"),
    ):
        if field in cumulative and getattr(chain, attr) != cumulative[field]:
            problems.append(
                f"cumulative {field}: expected {cumulative[field]}, got {getattr(chain, attr)}"
            )
    if case.get("expected_rounds_to_no_walk_away") is not None:
        if chain.rounds_to_no_walk_away != case["expected_rounds_to_no_walk_away"]:
            problems.append(
                f"rounds_to_no_walk_away: expected {case['expected_rounds_to_no_walk_away']}, "
                f"got {chain.rounds_to_no_walk_away}"
            )
    by_type = {h.clause_type: h for h in chain.histories}
    for clause_type, expected in (case.get("expected_histories") or {}).items():
        history = by_type.get(clause_type)
        if history is None:
            problems.append(f"{clause_type}: no history tracked")
            continue
        if "cumulative" in expected and history.cumulative_movement != expected["cumulative"]:
            problems.append(
                f"{clause_type} cumulative: expected {expected['cumulative']}, "
                f"got {history.cumulative_movement}"
            )
        if "re_traded" in expected and history.re_traded != expected["re_traded"]:
            problems.append(
                f"{clause_type} re_traded: expected {expected['re_traded']}, "
                f"got {history.re_traded}"
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
        elif kind == "round_diff":
            ok, detail = _eval_round_diff(case, samples)
        elif kind == "round_chain":
            ok, detail = _eval_round_chain(case, samples)
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
