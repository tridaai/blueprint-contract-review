"""Golden-dataset evaluation for the Contract Review Agent.

Runs the deterministic pipeline (playbook rules, offline fallback
backend) over ``golden.jsonl`` and reports clause-level accuracy
(clause type + playbook position), document-level accuracy
(posture, per-type positions, missing-clause detection, injection
flags, information requests, repairs), negotiation-round movement,
round-chain histories (re-trades, cumulative movement), exhibit
consistency checks, matter briefs (cross-chain patterns,
shared-obligation conflicts, walk-away lineage), walk-away line
acceptance (approved lines held / softened / dropped against the
follow-up round, tallied for the briefs), and proposal
effectiveness (verdicts citing an activated proposal's entries,
and the coverage gap before vs after activation — stopped or
still recurring). All cases are synthetic. Run from the repo root:

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


def _eval_exhibit(case: dict, samples: dict[str, ContractInput]) -> tuple[bool, str]:
    """Exhibit consistency: analyse the sample and compare its
    cross-document checks — which checks failed with which status —
    plus the posture those failures produce."""
    from contract_agent.service import ContractService
    from contract_agent.store import InMemoryStore

    contract = samples.get(case["contract_id"])
    if contract is None:
        return False, f"unknown sample {case['contract_id']}"
    service = ContractService(store=InMemoryStore(), checkpointer=False)
    result = service.analyze(contract)
    failed = {
        c.name: c.status for c in result.exhibit_checks if c.status != "consistent"
    }
    problems = []
    expected_failed = case.get("expected_failed") or {}
    if failed != expected_failed:
        problems.append(f"failed checks: expected {expected_failed}, got {failed}")
    if case.get("expected_posture") is not None:
        if result.facts.posture != case["expected_posture"]:
            problems.append(
                f"posture: expected {case['expected_posture']}, "
                f"got {result.facts.posture}"
            )
    if case.get("expected_checks") is not None:
        if len(result.exhibit_checks) != case["expected_checks"]:
            problems.append(
                f"checks: expected {case['expected_checks']}, "
                f"got {len(result.exhibit_checks)}"
            )
    return not problems, "; ".join(problems)


_COVENANT_LABEL_TO_KIND = {
    "non-compete": "non_compete",
    "non-solicitation": "non_solicit",
    "exclusivity": "exclusivity",
    "non-circumvention": "non_circumvention",
}


def _eval_sow_master(case: dict, samples: dict[str, ContractInput]) -> tuple[bool, str]:
    """SOW-vs-MSA covenant conflicts (round 19): seed the master
    agreement the case names, analyse the SOW under it, and
    compare the conflict map (covenant kind → status, for the
    readings that are not consistent), the full check count, and
    the posture — which the conflicts must NOT move: posture is
    the SOW against the playbook, the conflicts are the SOW
    against its own master."""
    from contract_agent.service import ContractService
    from contract_agent.store import InMemoryStore

    master = samples.get(case["master_contract_id"])
    sow = samples.get(case["contract_id"])
    if master is None or sow is None:
        return False, "unknown sample in sow_master case"
    service = ContractService(store=InMemoryStore(), checkpointer=False)
    service.analyze(master)
    result = service.analyze(sow)
    conflicts = {}
    for check in result.sow_covenant_conflicts:
        for label, kind in _COVENANT_LABEL_TO_KIND.items():
            if check.expectation.startswith(f"the {label} covenant"):
                if check.status != "consistent":
                    conflicts[kind] = check.status
    problems = []
    expected_conflicts = case.get("expected_conflicts") or {}
    if conflicts != expected_conflicts:
        problems.append(
            f"conflicts: expected {expected_conflicts}, got {conflicts}"
        )
    if case.get("expected_checks") is not None:
        if len(result.sow_covenant_conflicts) != case["expected_checks"]:
            problems.append(
                f"checks: expected {case['expected_checks']}, "
                f"got {len(result.sow_covenant_conflicts)}"
            )
    if case.get("expected_posture") is not None:
        if result.facts.posture != case["expected_posture"]:
            problems.append(
                f"posture: expected {case['expected_posture']}, "
                f"got {result.facts.posture}"
            )
    return not problems, "; ".join(problems)


def _eval_matter(case: dict, samples: dict[str, ContractInput]) -> tuple[bool, str]:
    """Matter briefs: run the case's steps — analyse sample or
    inline contracts under one counterparty + matter tag, with
    counsel edit agreements recorded between rounds where the case
    scripts them — then assemble the matter brief AS DATA and
    compare the matter-scope views: the cross-chain patterns, the
    shared obligations and their conflicts, the open walk-away
    lines' lineage, and the roll-up. The brief is a read model, so
    the case scores the assembly itself, not a pipeline verdict."""
    from contract_agent.matter_brief import build_matter_brief_data, matter_records
    from contract_agent.schemas import EditOutcomeInput
    from contract_agent.service import ContractService
    from contract_agent.store import InMemoryStore

    store = InMemoryStore()
    service = ContractService(store=store, checkpointer=False)
    results: dict[str, object] = {}
    for step in case.get("steps") or []:
        if "analyze" in step:
            contract = samples.get(step["analyze"])
            if contract is None:
                return False, f"unknown sample {step['analyze']}"
            if case.get("matter") and not contract.matter:
                contract = contract.model_copy(update={"matter": case["matter"]})
            results[contract.contract_id] = service.analyze(contract)
        elif "analyze_inline" in step:
            contract = ContractInput(**step["analyze_inline"])
            results[contract.contract_id] = service.analyze(contract)
        elif "agree" in step:
            target = results.get(step["agree"]["contract_id"])
            if target is None:
                return False, f"agree step for unanalysed {step['agree']['contract_id']}"
            clause_type = step["agree"]["clause_type"]
            index = next(
                (
                    i
                    for i, e in enumerate(target.proposed_edits)
                    if e.clause_type == clause_type
                ),
                None,
            )
            if index is None:
                return False, f"no proposed {clause_type} edit on {target.contract_id}"
            service.record_edit_outcomes(
                target.contract_id,
                [
                    EditOutcomeInput(
                        edit_index=index,
                        outcome="accepted",
                        note="agreed on the call",
                    )
                ],
                actor="Eval",
            )
        else:
            return False, f"unknown matter step {step}"
    chains = matter_records(store.all_records(), case["counterparty"], case["matter"])
    data = build_matter_brief_data(case["counterparty"], case["matter"], chains)
    problems = []
    if case.get("expected_chains") is not None and len(chains) != case["expected_chains"]:
        problems.append(f"chains: expected {case['expected_chains']}, got {len(chains)}")
    if case.get("expected_open_walk_aways") is not None:
        if data["rollup"]["open_walk_aways"] != case["expected_open_walk_aways"]:
            problems.append(
                f"open walk-aways: expected {case['expected_open_walk_aways']}, "
                f"got {data['rollup']['open_walk_aways']}"
            )
    if case.get("expected_matter_patterns") is not None:
        got = sorted(p["clause_type"] for p in data["matter_patterns"])
        if got != sorted(case["expected_matter_patterns"]):
            problems.append(
                f"matter patterns: expected {case['expected_matter_patterns']}, got {got}"
            )
    for clause_type, expected in (case.get("expected_pattern_active_chains") or {}).items():
        pattern = next(
            (p for p in data["matter_patterns"] if p["clause_type"] == clause_type), None
        )
        if pattern is None:
            problems.append(f"{clause_type}: no matter pattern")
        elif pattern["chains_with_active_pattern"] != expected:
            problems.append(
                f"{clause_type} active-pattern chains: expected {expected}, "
                f"got {pattern['chains_with_active_pattern']}"
            )
    for clause_type, expected in (case.get("expected_pattern_flags") or {}).items():
        pattern = next(
            (p for p in data["matter_patterns"] if p["clause_type"] == clause_type), None
        )
        if pattern is None:
            problems.append(f"{clause_type}: no matter pattern")
        elif pattern["total_flags"] != expected:
            problems.append(
                f"{clause_type} total flags: expected {expected}, got {pattern['total_flags']}"
            )
    groups = {g["kind"]: g for g in data["shared_obligations"]}
    for kind, status in (case.get("expected_shared_obligations") or {}).items():
        group = groups.get(kind)
        if group is None:
            problems.append(f"shared obligations: no {kind} group across chains")
        elif group["status"] != status:
            problems.append(
                f"shared {kind}: expected {status}, got {group['status']}"
            )
    for kind, days in (case.get("expected_shared_days") or {}).items():
        group = groups.get(kind)
        if group is None:
            problems.append(f"shared obligations: no {kind} group across chains")
        elif sorted(m["days"] for m in group["members"]) != sorted(days):
            problems.append(
                f"shared {kind} days: expected {days}, "
                f"got {[m['days'] for m in group['members']]}"
            )
    tier2 = {p["clause_type"]: p for p in data["open_positions"] if p["tier"] == 2}
    for clause_type, expected in (case.get("expected_lineage") or {}).items():
        entry = tier2.get(clause_type)
        if entry is None or entry["lineage"] is None:
            problems.append(f"{clause_type}: no open walk-away lineage")
            continue
        lineage = entry["lineage"]
        for field in ("first_seen_in", "walk_away_since", "moved", "from_position", "to_position"):
            if field in expected and lineage[field] != expected[field]:
                problems.append(
                    f"{clause_type} lineage {field}: expected {expected[field]}, "
                    f"got {lineage[field]}"
                )
    return not problems, "; ".join(problems)


def _eval_walkaway_acceptance(case: dict, samples: dict[str, ContractInput]) -> tuple[bool, str]:
    """Walk-away line acceptance: run the case's steps — analyse
    rounds, approve the packets the case approves — then tally
    the chain's approved walk-away lines with
    ``walkaway.acceptance_report`` (the same assembly both briefs
    render) and compare the counts and each line's lineage:
    status, the round that decided it, the follow-up round that
    resolved it, and the position it landed at."""
    from contract_agent.brief import build_negotiation_brief
    from contract_agent.service import ContractService
    from contract_agent.store import InMemoryStore
    from contract_agent.walkaway import acceptance_report, chain_for

    service = ContractService(store=InMemoryStore(), checkpointer=False)
    latest_id = ""
    for step in case.get("steps") or []:
        if "analyze" in step:
            contract = samples.get(step["analyze"])
            if contract is None:
                return False, f"unknown sample {step['analyze']}"
            latest_id = service.analyze(contract).contract_id
        elif "analyze_inline" in step:
            contract = ContractInput(**step["analyze_inline"])
            latest_id = service.analyze(contract).contract_id
        elif "approve" in step:
            service.approve(step["approve"], approver="Eval")
        else:
            return False, f"unknown acceptance step {step}"
    if not latest_id:
        return False, "no round analysed"
    chain = chain_for(service._get_store().all_records(), latest_id)
    results = [record.result for record in chain]
    report = acceptance_report(results)
    problems = []
    for key, expected in (case.get("expected_acceptance") or {}).items():
        if report.get(key) != expected:
            problems.append(
                f"acceptance {key}: expected {expected}, got {report.get(key)}"
            )
    def _line_key(entry: dict) -> tuple:
        return (
            entry["clause_type"],
            entry["decided_in"],
            entry["status"],
            entry["resolved_in"],
            entry["final_position"],
        )

    expected_lines = sorted(_line_key(e) for e in case.get("expected_lines") or [])
    got_lines = sorted(_line_key(e) for e in report["lines"])
    if expected_lines and got_lines != expected_lines:
        problems.append(f"lines: expected {expected_lines}, got {got_lines}")
    markdown = build_negotiation_brief(results, service.get(latest_id))
    if "## Walk-away line acceptance" not in markdown:
        problems.append("chain brief carries no acceptance section")
    headline = case.get("expected_headline")
    if headline and headline not in markdown:
        problems.append(f"headline missing from the brief: {headline!r}")
    return not problems, "; ".join(problems)


_GAP_TEXT = """1. Limitation of Liability
Each party's aggregate liability shall not exceed the fees paid in the
twelve (12) months preceding the claim.

2. Indemnification
Each party shall indemnify the other against third-party claims arising
from its breach of this Agreement.

3. Confidentiality
Each party shall hold the other's Confidential Information in confidence
for three (3) years.

4. Term and Termination
Either party may terminate this Agreement for convenience on thirty
(30) days' written notice.

5. Data Protection
Each party shall protect personal data in accordance with the Data
Processing Addendum. A party shall notify the other within 48 hours of
a personal data breach.

6. Governing Law
This Agreement is governed by the laws of the State of Delaware.

7. Force Majeure
Neither party shall be liable for delay or failure to perform caused
by events beyond its reasonable control, including acts of God, war,
or embargo.
"""


def _dig(view: dict, path: str):
    current = view
    for part in path.split("."):
        current = current[part]
    return current


def _check_effectiveness_view(view: dict, expected: dict, problems: list[str]) -> None:
    for path, want in expected.items():
        if path == "entries":
            got = {e["entry_id"]: e["verdicts"] for e in view["entries"]}
            if got != want:
                problems.append(f"entries: expected {want}, got {got}")
            continue
        got = _dig(view, path)
        if got != want:
            problems.append(f"{path}: expected {want}, got {got}")


def _eval_effectiveness(case: dict, samples: dict[str, ContractInput]) -> tuple[bool, str]:
    """Proposal effectiveness, two seedings. ``seeded``: an
    activated proposal and fabricated stamped records saved
    straight into the service's stores — the report's numbers are
    then exact arithmetic over known verdicts. ``pipeline``: the
    real gap flow (analyse gap contracts, generate + activate the
    proposal, backdate the gap runs, analyse one more) — the
    baseline must count the runs the activation was drafted from,
    and the structure-only activation must show the gap still
    recurring rather than stopped."""
    from contract_agent.proposal_store import InMemoryProposalStore
    from contract_agent.schemas import (
        AgentResult,
        ClauseVerdict,
        PlaybookProposal,
        PlaybookProposalEntry,
    )
    from contract_agent.service import ContractService
    from contract_agent.store import ApprovalRecord, InMemoryStore

    service = ContractService(
        store=InMemoryStore(),
        checkpointer=False,
        proposal_store=InMemoryProposalStore(),
    )
    problems: list[str] = []
    if case.get("mode") == "pipeline":
        ids = case["contract_ids"]
        for contract_id in ids[:2]:
            service.analyze(
                ContractInput(
                    contract_id=contract_id,
                    doc_type="msa",
                    title="Master Services Agreement",
                    counterparty_name="Effectiveness Eval Co",
                    our_role="vendor",
                    documents=[
                        DocumentInput(
                            doc_type="agreement",
                            document_id=f"AGR-{contract_id}",
                            raw_text=_GAP_TEXT,
                        )
                    ],
                )
            )
        proposals = service.generate_playbook_proposals()
        if len(proposals) != 1:
            return False, f"expected exactly 1 generated proposal, got {len(proposals)}"
        proposal = proposals[0]
        service.activate_playbook_proposal(proposal.proposal_id, actor="Eval")
        store = service._get_store()
        for contract_id in ids[:2]:
            record = store.get(contract_id)
            record.analyzed_at = case["restamp"]
            store.save(record)
        service.analyze(
            ContractInput(
                contract_id=ids[2],
                doc_type="msa",
                title="Master Services Agreement",
                counterparty_name="Effectiveness Eval Co",
                our_role="vendor",
                documents=[
                    DocumentInput(
                        doc_type="agreement",
                        document_id=f"AGR-{ids[2]}",
                        raw_text=_GAP_TEXT,
                    )
                ],
            )
        )
        view = service.proposal_effectiveness(proposal.proposal_id)
        _check_effectiveness_view(view, case.get("expected") or {}, problems)
        rows = service.metrics()["proposal_effectiveness"]
        if len(rows) != 1:
            problems.append(f"metrics rows: expected 1, got {len(rows)}")
        else:
            _check_effectiveness_view(rows[0], case.get("expected") or {}, problems)
        return not problems, "; ".join(problems)
    proposal = PlaybookProposal(
        **{
            **case["proposal"],
            "entries": [
                PlaybookProposalEntry(**entry) for entry in case["proposal"]["entries"]
            ],
        }
    )
    service._get_proposal_store().save(proposal)
    store = service._get_store()
    for record in case.get("records") or []:
        store.save(
            ApprovalRecord(
                result=AgentResult(
                    contract_id=record["contract_id"],
                    clause_verdicts=[
                        ClauseVerdict(**verdict) for verdict in record["verdicts"]
                    ],
                ),
                analyzed_at=record["analyzed_at"],
            )
        )
    view = service.proposal_effectiveness(proposal.proposal_id)
    _check_effectiveness_view(view, case.get("expected") or {}, problems)
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
        elif kind == "exhibit":
            ok, detail = _eval_exhibit(case, samples)
        elif kind == "sow_master":
            ok, detail = _eval_sow_master(case, samples)
        elif kind == "matter":
            ok, detail = _eval_matter(case, samples)
        elif kind == "walkaway_acceptance":
            ok, detail = _eval_walkaway_acceptance(case, samples)
        elif kind == "effectiveness":
            ok, detail = _eval_effectiveness(case, samples)
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
