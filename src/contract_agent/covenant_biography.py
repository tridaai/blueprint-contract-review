"""Covenant biography: the chain's restrictive covenants in
one timeline.

The annexes got their whole-chain biography in round 15 and
the certificate chain in round 16; the restrictive covenants
were the register's remaining pairwise story. A covenant's
life is spread across exactly the surfaces that each show one
slice of it: the register entry on each round's result (its
duration, its computed expiry, its anchor), the lapse a later
round's reading names when the restriction ran its course and
a fresh one is asked for (``covenant_history.py``), and the
follow-through acts that close the lapse's loop on the record
(round 16). Across a long chain nobody could see the whole
life in one place — which round imposed the covenant, how its
tail changed, when it lapsed, whether anyone closed the loop,
and whether the next round's covenant was read as the new
bargain it is.

This module is that view, in the annex history's shape: one
row per round (the covenants its register carries, each with
its clock, its lapse state as of today, and its follow-through
state read with the register's own ``follow_through_state``),
one step per round pair (what each step did to each covenant —
introduced, dropped, its duration changed, its expiry moved —
and the covenants that had lapsed by the later round and were
carried afresh), the later rounds' stored new-bargain readings
riding on their rows, and a summary. Employment and commercial
covenants alike: the register holds both since round 18, and
the biography reads the register, so it cannot disagree with
the findings it narrates.
"""

from __future__ import annotations

from datetime import date

from .covenant_history import _LABELS
from .obligations import ALL_COVENANT_KINDS, follow_through_state
from .schemas import AgentResult, ContractInput


def _round_covenants(
    result: AgentResult | None, acts: list[dict], today: date
) -> list[dict]:
    """One round's covenant rows: each covenant-kind entry on
    its stored register, with its lapse state as of ``today``
    and — for a dated covenant — its follow-through state, read
    with the register's own reading over the round record's
    acts. An undated covenant's clock never started: it carries
    no lapse and no follow-through state, the register's own
    position."""
    if result is None:
        return []
    rows: list[dict] = []
    for obligation in result.obligations:
        if obligation.kind not in ALL_COVENANT_KINDS:
            continue
        row = {
            "kind": obligation.kind,
            "label": _LABELS[obligation.kind],
            "detail": obligation.detail,
            "description": obligation.description,
            "date": obligation.date or "",
            "computed": obligation.computed,
            "source_clause_id": obligation.source_clause_id,
            "lapsed": False,
            "follow_through": [],
            "follow_through_state": "",
        }
        if obligation.date:
            try:
                expiry = date.fromisoformat(obligation.date)
            except ValueError:
                expiry = None
            if expiry is not None:
                lapsed = expiry <= today
                row["lapsed"] = lapsed
                row["follow_through"] = [
                    act for act in acts if act.get("kind") == obligation.kind
                ]
                row["follow_through_state"] = follow_through_state(
                    acts, obligation.kind, lapsed=lapsed
                )
        rows.append(row)
    rows.sort(key=lambda r: (r["kind"], r["date"]))
    return rows


def _by_kind(rows: list[dict]) -> dict[str, dict]:
    """A round's covenant rows by kind — the first row of each
    kind (the register can carry two entries of a kind from two
    clauses; the biography's steps read the kind's headline
    row, and the round rows themselves keep both)."""
    by_kind: dict[str, dict] = {}
    for row in rows:
        by_kind.setdefault(row["kind"], row)
    return by_kind


def build_covenant_biography(
    chain: list[ContractInput],
    results: dict[str, AgentResult] | None = None,
    follow_throughs: dict[str, list[dict]] | None = None,
    today: date | None = None,
) -> dict | None:
    """The covenant timeline for one negotiation chain, oldest
    round first: each round's covenants with their clocks,
    lapse and follow-through states, and the new-bargain
    readings stored on the round; each step's movement per
    covenant kind. ``results`` maps contract_id → the stored
    analysis result (the register's source) and
    ``follow_throughs`` contract_id → the record's
    follow-through acts; without a result a round shows no
    covenants — the biography is read off the stored registers,
    never reconstructed from the paper. None when no round of
    the chain carries a covenant — the view does not invent a
    biography for restrictions the negotiation never had."""
    results = results or {}
    follow_throughs = follow_throughs or {}
    today = today or date.today()
    rounds: list[dict] = []
    for contract in chain:
        result = results.get(contract.contract_id)
        readings = []
        if result is not None:
            readings = [
                {"found": r.found, "detail": r.detail}
                for r in result.lapsed_covenant_readings
            ]
        rounds.append(
            {
                "contract_id": contract.contract_id,
                "received_at": contract.received_at or "",
                "covenants": _round_covenants(
                    result,
                    follow_throughs.get(contract.contract_id, []),
                    today,
                ),
                "lapsed_readings": readings,
            }
        )
    if not any(row["covenants"] for row in rounds):
        return None

    steps: list[dict] = []
    for prior_row, current_row in zip(rounds, rounds[1:]):
        prior = _by_kind(prior_row["covenants"])
        current = _by_kind(current_row["covenants"])
        movements: list[dict] = []
        parts: list[str] = []
        for kind in ALL_COVENANT_KINDS:
            label = _LABELS[kind]
            before, after = prior.get(kind), current.get(kind)
            if before is None and after is None:
                continue
            if before is None:
                movements.append(
                    {
                        "kind": kind, "label": label,
                        "change": "introduced",
                        "detail": after["detail"],
                    }
                )
                parts.append(
                    f"the {label} covenant is introduced "
                    f"({after['detail']})"
                )
                continue
            if after is None:
                movements.append(
                    {
                        "kind": kind, "label": label,
                        "change": "dropped",
                        "detail": before["detail"],
                    }
                )
                parts.append(
                    f"the {label} covenant is dropped "
                    f"(it ran {before['detail']})"
                )
                continue
            if before["detail"] != after["detail"]:
                change, wording = "duration_changed", (
                    f"the {label} covenant's tail changes from "
                    f"{before['detail']} to {after['detail']}"
                )
            elif before["date"] != after["date"]:
                change, wording = "expiry_moved", (
                    f"the {label} covenant is carried at the same "
                    f"length, its expiry moving from "
                    f"{before['date'] or 'undated'} to "
                    f"{after['date'] or 'undated'}"
                )
            else:
                change, wording = "carried", (
                    f"the {label} covenant is carried unchanged "
                    f"({after['detail']})"
                )
            movements.append(
                {
                    "kind": kind, "label": label, "change": change,
                    "detail": after["detail"],
                }
            )
            parts.append(wording)
            if before["lapsed"]:
                parts.append(
                    f"the {label} covenant it succeeds had lapsed "
                    f"on {before['date']} (follow-through: "
                    f"{before['follow_through_state']}) — carried "
                    "afresh here, it is a new bargain, not a "
                    "continuation"
                )
        steps.append(
            {
                "from_contract_id": prior_row["contract_id"],
                "to_contract_id": current_row["contract_id"],
                "movements": movements,
                "summary": (
                    "; ".join(parts) + "."
                    if parts
                    else "Neither round carries a covenant."
                ),
            }
        )

    carried = [row for row in rounds if row["covenants"]]
    kinds_seen = sorted(
        {c["kind"] for row in rounds for c in row["covenants"]}
    )
    summary_lines = [
        f"Across {len(rounds)} round(s), {len(carried)} carried "
        f"restrictive covenants — {len(kinds_seen)} kind(s) in "
        f"all ({', '.join(_LABELS[k] for k in kinds_seen)})."
    ]
    lapsed = [
        (row["contract_id"], c)
        for row in rounds
        for c in row["covenants"]
        if c["lapsed"]
    ]
    if lapsed:
        summary_lines.append(
            f"{len(lapsed)} covenant(s) have lapsed by today: "
            + "; ".join(
                f"{cid}'s {c['label']} on {c['date']}"
                for cid, c in lapsed
            )
            + "."
        )
        open_lapses = [
            (cid, c) for cid, c in lapsed
            if c["follow_through_state"] == "open"
        ]
        if open_lapses:
            summary_lines.append(
                "Lapse(s) still standing open — announced, never "
                "followed through: "
                + "; ".join(
                    f"{cid}'s {c['label']}" for cid, c in open_lapses
                )
                + "."
            )
        confirmed = [
            (cid, c) for cid, c in lapsed
            if c["follow_through_state"] == "restriction_confirmed_lifted"
        ]
        if confirmed:
            summary_lines.append(
                "Lapse(s) closed on the record — the restriction "
                "confirmed lifted: "
                + "; ".join(
                    f"{cid}'s {c['label']}" for cid, c in confirmed
                )
                + "."
            )
    readings = sum(len(row["lapsed_readings"]) for row in rounds)
    if readings:
        summary_lines.append(
            f"{readings} new-bargain reading(s) ride on the later "
            "rounds' records — a fresh covenant read against the "
            "lapsed one it succeeds, and priced as its own promise."
        )
    return {"rounds": rounds, "steps": steps, "summary": summary_lines}
