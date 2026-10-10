"""Annex history across the whole chain: the annexes' biography
in one view.

The drift checks (``subprocessor_drift.py``,
``prior_invention_drift.py``) are pairwise: each reads the
latest round against the one it supersedes. That is the right
shape for a finding, and the wrong shape for a biography —
across a five-round negotiation, counsel wants to see the
sub-processor list's whole life in one place: what it held each
round, what each step changed, which additions landed inside
the promised notice window, and when an invention left the
schedule (a removal is a transfer, and WHEN it happened is
part of the fact). The renewal composition (round 14) already
walks whole chains for insurance terms; this module gives the
annexes the same walk.

The timelines are computed here, from the stored intake
packages, reusing the drift modules' own readings — the list
entries, the notice promise, the assignment's reliance on the
schedule — so the history can never disagree with the pairwise
checks about what a round's annex said. A family is reported
only when at least one round of the chain carries its annex;
rounds that do not carry it are recorded as absent on their
row, and a schedule that existed and is gone is a step of its
own — the loudest removal of all, named as one.
"""

from __future__ import annotations

from .exhibit_review import (
    _PRIOR_INVENTION_DOC,
    _assignment_relies_on_schedule,
    _find_schedule_document,
    _normalise_name,
)
from .prior_invention_drift import _assignment_text, prior_invention_entries
from .schemas import ContractInput
from .subprocessor_drift import (
    _promised_notice,
    _received_date,
    _subprocessor_clause_text,
    subprocessor_list_entries,
)


def _movement(
    prior_entries: list[dict], current_entries: list[dict]
) -> tuple[list[dict], list[dict], list[dict]]:
    """(added, removed, moved) between two rounds' entry lists,
    matched on the normalised key — the drift modules' own
    diff, shared so the history and the findings agree."""
    prior_by_key = {e["key"]: e for e in prior_entries}
    current_by_key = {e["key"]: e for e in current_entries}
    added = [e for k, e in current_by_key.items() if k not in prior_by_key]
    removed = [e for k, e in prior_by_key.items() if k not in current_by_key]
    moved = [
        {
            "name": current_by_key[k]["name"],
            "from_location": prior_by_key[k].get("location", ""),
            "to_location": current_by_key[k].get("location", ""),
        }
        for k in prior_by_key.keys() & current_by_key.keys()
        if _normalise_name(prior_by_key[k].get("location", ""))
        != _normalise_name(current_by_key[k].get("location", ""))
    ]
    return added, removed, moved


def _names(entries: list[dict]) -> list[str]:
    return [e["name"] for e in entries]


def _subprocessor_history(chain: list[ContractInput]) -> dict | None:
    rounds: list[dict] = []
    for contract in chain:
        entries = subprocessor_list_entries(contract)
        received = _received_date(contract)
        rounds.append(
            {
                "contract_id": contract.contract_id,
                "received_at": contract.received_at or "",
                "present": bool(entries),
                "entries": entries,
                "count": len(entries),
                "_received": received,
            }
        )
    if not any(row["present"] for row in rounds):
        return None
    steps: list[dict] = []
    violations = 0
    gained: list[str] = []
    dropped: list[str] = []
    for (prior, current), current_contract in zip(
        zip(rounds, rounds[1:]), chain[1:]
    ):
        step: dict = {
            "from_contract_id": prior["contract_id"],
            "to_contract_id": current["contract_id"],
        }
        if not prior["present"] or not current["present"]:
            absent = (
                prior["contract_id"]
                if not prior["present"]
                else current["contract_id"]
            )
            step.update(
                {
                    "added": [],
                    "removed": [],
                    "moved": [],
                    "notice": None,
                    "summary": (
                        f"No sub-processor list annex in {absent} — "
                        "no movement can be read across this step."
                    ),
                }
            )
            steps.append(step)
            continue
        added, removed, moved = _movement(prior["entries"], current["entries"])
        gained.extend(_names(added))
        dropped.extend(_names(removed))
        notice = None
        if added:
            _promised, promised_days = _promised_notice(
                _subprocessor_clause_text(current_contract)
            )
            if promised_days:
                gap = None
                if prior["_received"] is not None and current["_received"] is not None:
                    gap = (current["_received"] - prior["_received"]).days
                within = bool(gap is not None and gap < promised_days)
                if within:
                    violations += 1
                notice = {
                    "promised_days": promised_days,
                    "gap_days": gap,
                    "within_window": within,
                    "checkable": gap is not None,
                }
        parts: list[str] = []
        if added:
            parts.append("added: " + ", ".join(_names(added)))
        if removed:
            parts.append("removed: " + ", ".join(_names(removed)))
        if moved:
            parts.append(
                "moved: "
                + ", ".join(
                    f"{m['name']} ({m['from_location'] or 'no location stated'}"
                    f" → {m['to_location'] or 'no location stated'})"
                    for m in moved
                )
            )
        summary = (
            "; ".join(parts)
            if parts
            else "the list is unchanged across this step"
        )
        if notice is not None:
            if not notice["checkable"]:
                summary += (
                    " — the notice clock could not be checked: one "
                    "of the rounds carries no received date."
                )
            elif notice["within_window"]:
                summary += (
                    f" — the addition first appears {notice['gap_days']} "
                    f"day(s) after the previous round, inside the "
                    f"{notice['promised_days']} days' notice the clause "
                    "promises: the promised notice period cannot have run."
                )
            else:
                summary += (
                    " — the additions stand outside the notice window."
                )
        step.update(
            {
                "added": _names(added),
                "removed": _names(removed),
                "moved": moved,
                "notice": notice,
                "summary": summary,
            }
        )
        steps.append(step)
    summary_lines = [
        f"Across {len(rounds)} round(s) the sub-processor list "
        f"gained {len(gained)} processor(s)"
        + (f" ({', '.join(gained)})" if gained else "")
        + f", dropped {len(dropped)}"
        + (f" ({', '.join(dropped)})" if dropped else "")
        + f", and stands at {rounds[-1]['count']} in the latest round.",
    ]
    if violations:
        summary_lines.append(
            f"{violations} step(s) added a processor inside the "
            "promised notice window — the additions the drift "
            "check reads as findings."
        )
    for row in rounds:
        row.pop("_received", None)
    return {"rounds": rounds, "steps": steps, "summary": summary_lines}


def _prior_invention_history(chain: list[ContractInput]) -> dict | None:
    rounds: list[dict] = []
    for contract in chain:
        doc = _find_schedule_document(contract, _PRIOR_INVENTION_DOC)
        entries = prior_invention_entries(contract)
        rounds.append(
            {
                "contract_id": contract.contract_id,
                "received_at": contract.received_at or "",
                "schedule_present": doc is not None,
                "entries": entries,
                "count": len(entries),
            }
        )
    if not any(row["schedule_present"] for row in rounds):
        return None
    by_id = {c.contract_id: c for c in chain}
    steps: list[dict] = []
    transfers: list[str] = []
    for prior, current in zip(rounds, rounds[1:]):
        step: dict = {
            "from_contract_id": prior["contract_id"],
            "to_contract_id": current["contract_id"],
        }
        vanished = prior["schedule_present"] and not current["schedule_present"]
        if not prior["entries"] and not vanished:
            step.update(
                {
                    "added": [],
                    "removed": [],
                    "removal_is_transfer": False,
                    "schedule_vanished": False,
                    "summary": (
                        f"The schedule in {prior['contract_id']} "
                        "listed nothing — no baseline to read "
                        "movement from across this step."
                    ),
                }
            )
            steps.append(step)
            continue
        added, removed, _moved = _movement(prior["entries"], current["entries"])
        relies = _assignment_relies_on_schedule(
            _assignment_text(by_id[current["contract_id"]])
        )
        is_transfer = bool(removed) and relies
        if is_transfer:
            transfers.extend(_names(removed))
        parts: list[str] = []
        if added:
            parts.append("added: " + ", ".join(_names(added)))
        if removed:
            parts.append("removed: " + ", ".join(_names(removed)))
        summary = (
            "; ".join(parts)
            if parts
            else "the schedule is unchanged across this step"
        )
        if vanished:
            summary += (
                " — and the schedule document itself is gone in "
                f"{current['contract_id']}: every invention it "
                "listed leaves the record at this step."
            )
        elif is_transfer:
            summary += (
                " — the assignment excludes what the schedule "
                "lists, so the removal is a transfer: what left "
                "the schedule falls into the assignment."
            )
        elif removed:
            summary += (
                " — the current assignment does not tie its "
                "exclusion to the schedule, so the removal "
                "changes no exclusion the assignment grants."
            )
        step.update(
            {
                "added": _names(added),
                "removed": _names(removed),
                "removal_is_transfer": is_transfer,
                "schedule_vanished": vanished,
                "summary": summary,
            }
        )
        steps.append(step)
    summary_lines = [
        f"Across {len(rounds)} round(s) the schedule of prior "
        f"inventions stands at {rounds[-1]['count']} recorded "
        "invention(s) in the latest round.",
    ]
    if transfers:
        summary_lines.append(
            "Removals the assignment reads as transfers: "
            + ", ".join(transfers)
            + " — inventions that left the schedule and, as the "
            "chain stands, fall into the assignment."
        )
    return {"rounds": rounds, "steps": steps, "summary": summary_lines}


def build_annex_history(chain: list[ContractInput]) -> dict:
    """The annex timelines for one negotiation chain, oldest
    round first and the current round last: each annex family's
    per-round state and per-step movement, with the notice
    clock read on every sub-processor addition and the
    transfer reading on every schedule removal. A family with
    no annex anywhere in the chain is None — the view does not
    invent a biography for an annex the negotiation never had."""
    return {
        "chain": [c.contract_id for c in chain],
        "rounds": len(chain),
        "subprocessor": _subprocessor_history(chain),
        "prior_inventions": _prior_invention_history(chain),
        "note": (
            "The annexes' biography across the whole chain, "
            "computed from the stored rounds with the drift "
            "checks' own readings — the pairwise findings ride "
            "on each round's result; this view is the timeline "
            "they are steps of."
        ),
    }
