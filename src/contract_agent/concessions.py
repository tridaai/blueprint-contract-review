"""Counterparty concession profiles: how this counterparty
actually negotiates, computed from the stored record.

Verdicts describe a draft; chains describe a negotiation. Across
every negotiation with one counterparty, patterns counsel feels in
their bones become countable: the clause types they concede once
pressed (improved across rounds, closed better than they opened),
the ones they hold (flagged early, never moved — or won back after
a re-trade), how many rounds it takes them to clear the walk-away
positions, and how often ground won is given back. This module
computes those facts from the store's records — chains threaded by
the ``supersedes`` links, movement from ``round_diff``'s chain
builder — and nothing else. No model judgement: a type is labelled
``concedes`` / ``holds`` / ``mixed`` by a stated counting rule over
its chains, and every number in the profile traces to a stored
result.

The profile surfaces two ways: as a view (``GET
/counterparties/{name}/profile``, the console's profile panel) and
as evidence lines in the next review of the same counterparty, so
the assessment counsel reads is informed by how this counterparty
has actually moved — facts, offered as priors, never as verdicts.
"""

from __future__ import annotations

from collections import Counter

from .round_diff import build_round_chain
from .schemas import POSITION_SEVERITY
from .store import ApprovalRecord


def _chains(records: list[ApprovalRecord]) -> list[list[ApprovalRecord]]:
    """Group one counterparty's records into negotiation chains,
    oldest → newest, following the ``supersedes`` links. A record
    whose predecessor is not in the set starts a chain; cycles in
    bad intake terminate on the seen set, as elsewhere."""
    by_id = {r.result.contract_id: r for r in records}
    children: dict[str, ApprovalRecord] = {}
    for record in records:
        supersedes = (record.contract or {}).get("supersedes") or ""
        if supersedes and supersedes in by_id:
            children[supersedes] = record
    chains: list[list[ApprovalRecord]] = []
    for record in records:
        supersedes = (record.contract or {}).get("supersedes") or ""
        if supersedes and supersedes in by_id:
            continue  # not a root — reached by walking its root
        chain = [record]
        seen = {record.result.contract_id}
        cursor = record
        while cursor.result.contract_id in children:
            nxt = children[cursor.result.contract_id]
            if nxt.result.contract_id in seen:
                break
            seen.add(nxt.result.contract_id)
            chain.append(nxt)
            cursor = nxt
        chains.append(chain)
    return chains


def build_concession_profile(
    counterparty: str, records: list[ApprovalRecord]
) -> dict:
    """The concession profile for one counterparty over its stored
    records. ``records`` is the counterparty's full record set (the
    caller filters); an empty set yields a zero profile."""
    mine = [
        r
        for r in records
        if (r.result.counterparty_name or (r.contract or {}).get("counterparty_name", ""))
        == counterparty
    ]
    chains = _chains(mine)
    multi = [chain for chain in chains if len(chain) >= 2]

    per_type: dict[str, dict] = {}

    def slot(clause_type: str) -> dict:
        return per_type.setdefault(
            clause_type,
            {
                "clause_type": clause_type,
                "chains": 0,
                "improved_steps": 0,
                "worsened_steps": 0,
                "re_trades": 0,
                "conceded_chains": 0,
                "held_chains": 0,
                "closes_preferred": 0,
                "first_positions": Counter(),
                "final_positions": Counter(),
            },
        )

    rounds_to_clean: list[int] = []
    total_re_trades = 0
    histories_with_improvement = 0
    for chain in multi:
        report = build_round_chain([r.result for r in chain])
        if report.rounds_to_no_walk_away is not None:
            rounds_to_clean.append(report.rounds_to_no_walk_away)
        total_re_trades += report.re_trades
        for history in report.histories:
            entry = slot(history.clause_type)
            entry["chains"] += 1
            entry["improved_steps"] += sum(
                1 for m in history.movements if m == "improved"
            )
            entry["worsened_steps"] += sum(
                1 for m in history.movements if m == "worsened"
            )
            if history.re_traded:
                entry["re_trades"] += 1
            if "improved" in history.movements:
                histories_with_improvement += 1
            present = [p.position for p in history.track if p.position]
            if not present:
                continue
            first, last = present[0], present[-1]
            entry["first_positions"][first] += 1
            entry["final_positions"][last] += 1
            if POSITION_SEVERITY[first] == 0:
                continue  # opened preferred: nothing to concede or hold
            if POSITION_SEVERITY[last] < POSITION_SEVERITY[first]:
                entry["conceded_chains"] += 1
            else:
                entry["held_chains"] += 1
            if last == "preferred":
                entry["closes_preferred"] += 1

    clause_types: list[dict] = []
    for entry in per_type.values():
        conceded, held = entry["conceded_chains"], entry["held_chains"]
        if conceded == 0 and held == 0:
            tendency = "uncontested"  # never opened flagged in a chain
        elif conceded > held:
            tendency = "concedes"
        elif held > conceded:
            tendency = "holds"
        else:
            tendency = "mixed"
        clause_types.append(
            {
                "clause_type": entry["clause_type"],
                "chains": entry["chains"],
                "improved_steps": entry["improved_steps"],
                "worsened_steps": entry["worsened_steps"],
                "re_trades": entry["re_trades"],
                "conceded_chains": conceded,
                "held_chains": held,
                "closes_preferred": entry["closes_preferred"],
                "first_positions": dict(entry["first_positions"]),
                "final_positions": dict(entry["final_positions"]),
                "tendency": tendency,
            }
        )
    # Most contested first: chains seen, then movement volume.
    clause_types.sort(
        key=lambda row: (
            -(row["conceded_chains"] + row["held_chains"]),
            -row["chains"],
            row["clause_type"],
        )
    )

    decided = [r.result for r in mine if r.result.decision in ("approved", "rejected")]
    return {
        "counterparty": counterparty,
        "contracts": len(mine),
        "chains": len(chains),
        "multi_round_chains": len(multi),
        "decisions": {
            "approved": sum(1 for r in decided if r.decision == "approved"),
            "rejected": sum(1 for r in decided if r.decision == "rejected"),
            "awaiting": len(mine) - len(decided),
        },
        "rounds_to_no_walk_away": rounds_to_clean,
        "best_rounds_to_no_walk_away": min(rounds_to_clean) if rounds_to_clean else None,
        "re_trades": total_re_trades,
        "re_trade_rate": (
            round(total_re_trades / histories_with_improvement, 3)
            if histories_with_improvement
            else None
        ),
        "clause_types": clause_types,
        "concedes": [r["clause_type"] for r in clause_types if r["tendency"] == "concedes"],
        "holds": [r["clause_type"] for r in clause_types if r["tendency"] == "holds"],
        "mixed": [r["clause_type"] for r in clause_types if r["tendency"] == "mixed"],
    }


def concession_evidence_lines(profile: dict) -> list[str]:
    """The profile as assessment evidence lines: the headline counts
    plus the strongest concede/hold facts. Lines appear only when a
    multi-round chain exists — a single decided contract is a
    decision, not a negotiating pattern."""
    if not profile or profile.get("multi_round_chains", 0) == 0:
        return []
    lines = [
        f"concession profile: {profile['counterparty']} — "
        f"{profile['contracts']} contract(s) in {profile['chains']} chain(s), "
        f"{profile['multi_round_chains']} multi-round; re-trades: "
        f"{profile['re_trades']}"
        + (
            f"; fastest clearance of walk-away positions: round "
            f"{profile['best_rounds_to_no_walk_away']}"
            if profile["best_rounds_to_no_walk_away"]
            else "; no chain has cleared its walk-away positions yet"
        )
    ]
    for row in profile["clause_types"]:
        if row["tendency"] == "concedes":
            finals = ", ".join(
                f"{n}× {name}" for name, n in sorted(row["final_positions"].items())
            )
            lines.append(
                f"concession profile: {profile['counterparty']} concedes "
                f"{row['clause_type']} — moved in {row['conceded_chains']} of "
                f"{row['conceded_chains'] + row['held_chains']} contested "
                f"chain(s); closes at: {finals}"
            )
    # Holds are ordered by signal strength: a re-traded clause (won
    # once, taken back) tells counsel more than a clause that simply
    # never moved, so re-trades surface first.
    holds = [r for r in profile["clause_types"] if r["tendency"] == "holds"]
    holds.sort(key=lambda r: (-r["re_trades"], -r["worsened_steps"], r["clause_type"]))
    for row in holds:
        finals = ", ".join(
            f"{n}× {name}" for name, n in sorted(row["final_positions"].items())
        )
        lines.append(
            f"concession profile: {profile['counterparty']} holds "
            f"{row['clause_type']} — no net movement in "
            f"{row['held_chains']} contested chain(s); latest positions: "
            f"{finals}"
            + (f"; re-traded {row['re_trades']}×" if row["re_trades"] else "")
        )
    return lines[:6]  # priors inform; they do not crowd out the contract's own facts
