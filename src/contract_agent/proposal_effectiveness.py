"""Proposal effectiveness: what an activated playbook proposal
actually did.

Activation is where round 7 stopped — counsel's decision joined
the proposal's entries to the active playbook, and the record went
quiet. But the question legal ops asks a month later is empirical:
*did the gap close?* Are verdicts for that clause type still
arriving flagged with nothing to offer, or does the new language
now get cited, and at what positions?

This module answers from the stored records, in code. A verdict
counts as issued under a proposal when it was issued at or after
the activation stamp (records carry ``analyzed_at`` — stamped by
the service on every analysis; records saved before the stamp
existed are counted honestly as unstamped and never backdated)
and — for the entry-level view — when its ``playbook_refs`` cite
one of the proposal's entries. The clause-type view sits beside
it: every verdict for the proposal's clause type since activation,
how many cited the new entries, how many still arrived flagged
with no fallback language (the gap recurring — the activation did
not take), and the position spread.

Nothing here judges whether the activation was *wise* — it reports
what happened under it, on the proposal record's surfaces
(``/playbook/proposals`` and ``/metrics``), so counsel can.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from .schemas import PlaybookProposal
from .store import ApprovalRecord


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def proposal_effectiveness(
    proposal: PlaybookProposal, records: list[ApprovalRecord]
) -> dict:
    """The effectiveness view for one proposal over the stored
    records. A proposal that was never activated has no window to
    measure: the view says so, with its status, instead of
    reporting zeros that would look like failure."""
    entry_ids = [entry.id for entry in proposal.entries]
    base = {
        "proposal_id": proposal.proposal_id,
        "clause_type": proposal.clause_type,
        "status": proposal.status,
        "activated_at": proposal.decided_at if proposal.status == "activated" else "",
        "decided_by": proposal.decided_by if proposal.status == "activated" else "",
    }
    empty_entries = [
        {"entry_id": entry_id, "verdicts": 0, "positions": {}}
        for entry_id in entry_ids
    ]
    if proposal.status != "activated":
        return {
            **base,
            "active": False,
            "runs_since_activation": 0,
            "unstamped_runs": 0,
            "entries": empty_entries,
            "verdicts_for_type": 0,
            "verdicts_citing_entries": 0,
            "positions_for_type": {},
            "flagged_for_type": 0,
            "flagged_without_fallback_since": 0,
            "gap_recurring": False,
        }
    since = _parse_ts(proposal.decided_at)
    stamped = [r for r in records if r.analyzed_at]
    unstamped = len(records) - len(stamped)
    in_window = [
        r
        for r in stamped
        if since is None
        or (_parse_ts(r.analyzed_at) is not None and _parse_ts(r.analyzed_at) >= since)
    ]
    per_entry: dict[str, dict] = {
        entry_id: {"entry_id": entry_id, "verdicts": 0, "positions": Counter()}
        for entry_id in entry_ids
    }
    positions: Counter = Counter()
    verdicts_for_type = 0
    citing = 0
    flagged = 0
    without_fallback = 0
    for record in in_window:
        for verdict in record.result.clause_verdicts:
            cited_here = [ref for ref in verdict.playbook_refs if ref in per_entry]
            for ref in cited_here:
                per_entry[ref]["verdicts"] += 1
                per_entry[ref]["positions"][verdict.position] += 1
            if verdict.clause_type != proposal.clause_type:
                continue
            verdicts_for_type += 1
            positions[verdict.position] += 1
            if cited_here:
                citing += 1
            if verdict.position != "preferred":
                flagged += 1
                if not verdict.fallback_language:
                    without_fallback += 1
    return {
        **base,
        "active": True,
        "runs_since_activation": len(in_window),
        "unstamped_runs": unstamped,
        "entries": [
            {
                "entry_id": entry["entry_id"],
                "verdicts": entry["verdicts"],
                "positions": dict(entry["positions"]),
            }
            for entry in per_entry.values()
        ],
        "verdicts_for_type": verdicts_for_type,
        "verdicts_citing_entries": citing,
        "positions_for_type": dict(positions),
        "flagged_for_type": flagged,
        "flagged_without_fallback_since": without_fallback,
        "gap_recurring": without_fallback > 0,
    }
