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

The baseline sits on the other side of the stamp: the same
clause-type counts over the records analysed BEFORE activation
(``before_activation``). "Since" alone can only say whether the
gap appeared again; before-vs-since is what says it STOPPED —
``gap_stopped_recurring`` is set only when the gap was real
before the stamp (flagged verdicts with no fallback language)
and has not appeared since. A clause type with no gap before
activation has nothing to stop, and the view says so by leaving
the flag unset rather than crediting the activation.

Nothing here judges whether the activation was *wise* — it reports
what happened under it, on the proposal record's surfaces
(``/playbook/proposals`` and ``/metrics``), so counsel can.

Retirement (round 10) closes the window rather than erasing it: a
retired proposal's view measures the activation → retirement
window exactly as above, and adds a post-retirement window —
verdicts for the clause type since the entries left the playbook,
and whether the coverage gap the activation had closed has
re-opened (flagged verdicts arriving with no fallback language
again). The gap re-opening is the honest price of retiring, stated
on the record rather than discovered at the next flagged contract.
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
    live = proposal.status in ("activated", "retired")
    base = {
        "proposal_id": proposal.proposal_id,
        "clause_type": proposal.clause_type,
        "status": proposal.status,
        "activated_at": proposal.decided_at if live else "",
        "decided_by": proposal.decided_by if live else "",
        "retired_at": proposal.retired_at if proposal.status == "retired" else "",
        "retired_by": proposal.retired_by if proposal.status == "retired" else "",
        "retire_reason": (
            proposal.retire_reason if proposal.status == "retired" else ""
        ),
    }
    empty_entries = [
        {"entry_id": entry_id, "verdicts": 0, "positions": {}}
        for entry_id in entry_ids
    ]
    if not live:
        return {
            **base,
            "active": False,
            "retired": False,
            "runs_since_activation": 0,
            "unstamped_runs": 0,
            "entries": empty_entries,
            "verdicts_for_type": 0,
            "verdicts_citing_entries": 0,
            "positions_for_type": {},
            "flagged_for_type": 0,
            "flagged_without_fallback_since": 0,
            "gap_recurring": False,
            "before_activation": {
                "runs": 0,
                "verdicts_for_type": 0,
                "flagged_for_type": 0,
                "flagged_without_fallback": 0,
            },
            "gap_stopped_recurring": False,
            "post_retirement": None,
        }
    since = _parse_ts(proposal.decided_at)
    until = _parse_ts(proposal.retired_at) if proposal.status == "retired" else None
    stamped = [r for r in records if r.analyzed_at]
    unstamped = len(records) - len(stamped)

    def _in_window(record) -> bool:
        ts = _parse_ts(record.analyzed_at)
        if ts is None:
            return False
        if since is not None and ts < since:
            return False
        if until is not None and ts >= until:
            return False
        return True

    in_window = [r for r in stamped if _in_window(r)]
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
    # The baseline: the same clause-type counts over the stamped
    # records analysed before the activation stamp. Unstamped
    # records belong to neither window — they are counted once,
    # as unstamped, above.
    before_records = [
        r
        for r in stamped
        if since is not None
        and _parse_ts(r.analyzed_at) is not None
        and _parse_ts(r.analyzed_at) < since
    ]
    before_verdicts = 0
    before_flagged = 0
    before_without_fallback = 0
    for record in before_records:
        for verdict in record.result.clause_verdicts:
            if verdict.clause_type != proposal.clause_type:
                continue
            before_verdicts += 1
            if verdict.position != "preferred":
                before_flagged += 1
                if not verdict.fallback_language:
                    before_without_fallback += 1
    before_activation = {
        "runs": len(before_records),
        "verdicts_for_type": before_verdicts,
        "flagged_for_type": before_flagged,
        "flagged_without_fallback": before_without_fallback,
    }
    post_retirement = None
    if proposal.status == "retired":
        after = [
            r
            for r in stamped
            if until is not None
            and _parse_ts(r.analyzed_at) is not None
            and _parse_ts(r.analyzed_at) >= until
        ]
        pr_verdicts = 0
        pr_flagged = 0
        pr_without_fallback = 0
        for record in after:
            for verdict in record.result.clause_verdicts:
                if verdict.clause_type != proposal.clause_type:
                    continue
                pr_verdicts += 1
                if verdict.position != "preferred":
                    pr_flagged += 1
                    if not verdict.fallback_language:
                        pr_without_fallback += 1
        post_retirement = {
            "runs": len(after),
            "verdicts_for_type": pr_verdicts,
            "flagged_for_type": pr_flagged,
            "flagged_without_fallback": pr_without_fallback,
            "gap_reopened": pr_without_fallback > 0,
        }
    return {
        **base,
        "active": proposal.status == "activated",
        "retired": proposal.status == "retired",
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
        "before_activation": before_activation,
        # The gap stopped only if it was real before the stamp
        # and absent since: a type that never gapped has nothing
        # for the activation to have stopped.
        "gap_stopped_recurring": before_without_fallback > 0 and without_fallback == 0,
        "post_retirement": post_retirement,
    }
