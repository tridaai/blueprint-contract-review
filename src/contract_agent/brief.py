"""The negotiation brief: one document counsel reads before the call.

Everything counsel needs about a negotiation is already in the
store — the chain of rounds and how each clause moved through it,
the re-trades, the positions still open and the tier proposed for
each, the proposed edits, the exhibit findings, the obligations —
but it is spread across per-round results and console panels. This
module assembles it into a single markdown document, in code, from
the stored records: every number in the brief traces to a stored
result, and nothing in it is model-composed.

The brief is labelled what it is — a draft for counsel review. It
states no legal conclusion, recommends no signature, and is
exported through the object-store port (``contracts/<id>/
negotiation-brief.md``) and the download endpoint; like every
output of this blueprint it is never sent anywhere by the agent.

Exports form a lineage (see ``ContractService.negotiation_brief``):
each export's source digest is retained, and the next export
diffs against it — the brief leads with a "Since the last brief"
section (``brief_delta_lines``) naming the new round, the clauses
that moved, the edits proposed anew, the obligations opened or
closed. Both the previous and the new export stay in object
storage under versioned keys; nothing is overwritten but the
canonical latest copy.
"""

from __future__ import annotations

import hashlib

from .schemas import AgentResult


def _flagged_count(result: AgentResult) -> int:
    return sum(1 for v in result.clause_verdicts if v.position != "preferred")


def _walk_away_count(result: AgentResult) -> int:
    return sum(1 for v in result.clause_verdicts if v.position == "walk_away")


# ---------------------------------------------------------------------------
# Brief digests + deltas: what changed since the last export
# ---------------------------------------------------------------------------


def brief_source_digest(chain: list[AgentResult], latest: AgentResult) -> dict:
    """The facts a brief is built from, as a comparable digest.

    Stored with every export (in the brief lineage index, see
    ``ContractService.negotiation_brief``) so the NEXT export can
    diff its facts against this one's and lead with the delta —
    the Monday-morning question is never "what does the brief
    say?" but "what changed since the brief I already read?".
    Everything here is copied from stored results; an edit is keyed
    by a hash of its proposed language, so re-proposing the same
    language is not news and new language is."""
    edits = []
    for edit in latest.proposed_edits:
        digest = hashlib.sha1(edit.replacement_text.encode("utf-8")).hexdigest()[:10]
        edits.append(
            {
                "key": f"{edit.clause_id}|{edit.clause_type}|{edit.kind}|{digest}",
                "clause_id": edit.clause_id,
                "clause_type": edit.clause_type,
                "kind": edit.kind,
            }
        )
    return {
        "contract_id": latest.contract_id,
        "chain": [r.contract_id for r in chain],
        "status": latest.status,
        "decision": latest.decision or "",
        "decided_by": latest.decided_by or "",
        "posture": latest.facts.posture if latest.facts else "",
        "worst_position": latest.facts.worst_position if latest.facts else "",
        "clause_positions": {
            f"{v.clause_type}:{v.clause_id}": v.position for v in latest.clause_verdicts
        },
        "proposed_edits": edits,
        "edit_outcomes": {
            r.contract_id: {str(o.edit_index): o.outcome for o in r.edit_outcomes}
            for r in chain
            if r.edit_outcomes
        },
        "obligations": [
            {"kind": o.kind, "detail": o.detail, "date": o.date or ""}
            for o in latest.obligations
        ],
        "subprocessor_drift": [
            {"status": c.status, "found": c.found}
            for c in latest.subprocessor_drift
        ],
        "re_trades": latest.round_chain.re_trades if latest.round_chain else 0,
        "reflection_flags": sum(
            1 for e in latest.edit_reflection if e.status == "agreed_not_reflected"
        ),
        "reflection_patterns": sorted(
            p.clause_type for p in latest.reflection_patterns
        ),
    }


def brief_delta_lines(previous: dict, current: dict) -> list[str]:
    """What changed between the previous export's digest and this
    one's, as plain lines for the brief's "Since the last brief"
    section: a new round, a decision recorded, clauses moved or
    appearing/disappearing, edits proposed anew, outcomes recorded,
    obligations opened or closed, reflection flags raised. An empty
    diff says so — the re-export itself is the only news."""
    lines: list[str] = []
    if previous["contract_id"] != current["contract_id"]:
        lines.append(
            f"New round since the last brief: {current['contract_id']} — "
            f"the last brief covered {previous['contract_id']}; the chain "
            f"now runs {' → '.join(current['chain'])}."
        )
    if current["decision"] and current["decision"] != previous["decision"]:
        lines.append(
            f"Decision recorded since the last brief: {current['decision']} "
            f"by {current['decided_by'] or 'counsel'} on {current['contract_id']}."
        )
    elif (
        current["status"] != previous["status"]
        and current["contract_id"] == previous["contract_id"]
    ):
        lines.append(
            f"Status changed since the last brief: "
            f"{previous['status']} → {current['status']}."
        )
    prev_pos, cur_pos = previous["clause_positions"], current["clause_positions"]
    for key in sorted(set(prev_pos) & set(cur_pos)):
        if prev_pos[key] != cur_pos[key]:
            clause_type, clause_id = key.split(":", 1)
            lines.append(
                f"Clause moved: {clause_type} ({clause_id}): "
                f"{prev_pos[key]} → {cur_pos[key]}."
            )
    for key in sorted(set(cur_pos) - set(prev_pos)):
        clause_type, clause_id = key.split(":", 1)
        lines.append(
            f"New clause in the current round: {clause_type} "
            f"({clause_id}) at {cur_pos[key]}."
        )
    for key in sorted(set(prev_pos) - set(cur_pos)):
        clause_type, clause_id = key.split(":", 1)
        lines.append(
            f"Clause no longer in the draft: {clause_type} "
            f"({clause_id}, was {prev_pos[key]})."
        )
    prev_edits = {e["key"]: e for e in previous["proposed_edits"]}
    cur_edits = {e["key"]: e for e in current["proposed_edits"]}
    for key in sorted(set(cur_edits) - set(prev_edits)):
        edit = cur_edits[key]
        lines.append(
            f"New proposed edit: {edit['clause_id']} "
            f"{edit['clause_type']} ({edit['kind']})."
        )
    for contract_id in sorted(
        set(previous["edit_outcomes"]) | set(current["edit_outcomes"])
    ):
        prev_outcomes = previous["edit_outcomes"].get(contract_id, {})
        cur_outcomes = current["edit_outcomes"].get(contract_id, {})
        changed = {
            idx
            for idx in cur_outcomes
            if prev_outcomes.get(idx) != cur_outcomes[idx]
        }
        for idx in sorted(changed, key=int):
            lines.append(
                f"Edit outcome recorded: {contract_id} edit {idx} "
                f"— {cur_outcomes[idx]}."
            )

    def obligation_key(obligation: dict) -> tuple[str, str, str]:
        return (obligation["kind"], obligation["detail"], obligation["date"])

    prev_obligations = {obligation_key(o): o for o in previous["obligations"]}
    cur_obligations = {obligation_key(o): o for o in current["obligations"]}
    for key in sorted(set(cur_obligations) - set(prev_obligations)):
        obligation = cur_obligations[key]
        lines.append(
            f"New obligation on the register: {obligation['kind']} — "
            f"{obligation['detail']}"
            + (f" ({obligation['date']})" if obligation["date"] else "")
            + "."
        )
    for key in sorted(set(prev_obligations) - set(cur_obligations)):
        obligation = prev_obligations[key]
        lines.append(
            f"Obligation closed or no longer on the register: "
            f"{obligation['kind']} — {obligation['detail']}."
        )
    if current["reflection_flags"] and (
        current["reflection_flags"] != previous["reflection_flags"]
    ):
        lines.append(
            f"{current['reflection_flags']} agreed edit(s) from the "
            "previous round are flagged agreed-but-not-reflected in "
            "the current draft (see Edit outcomes and reflection)."
        )
    for clause_type in sorted(
        set(current.get("reflection_patterns", []))
        - set(previous.get("reflection_patterns", []))
    ):
        lines.append(
            f"New reflection pattern: {clause_type} — agreed edits on "
            "this clause were flagged not reflected in two consecutive "
            "follow-ups; the packet now proposes the walk-away line "
            "for it (see Reflection patterns)."
        )
    cur_drift = current.get("subprocessor_drift", [])
    prev_drift = previous.get("subprocessor_drift", [])
    if cur_drift and cur_drift != prev_drift:
        drift = cur_drift[0]
        lines.append(
            f"Sub-processor list drift vs the prior round: "
            f"{drift['found']} ({drift['status']}) — see Exhibit "
            "findings."
        )
    if current["re_trades"] != previous["re_trades"]:
        lines.append(
            f"Re-trades across the chain: {previous['re_trades']} "
            f"→ {current['re_trades']}."
        )
    if not lines:
        lines.append(
            "Nothing material changed since the last brief — the "
            "facts below are the same facts, re-exported."
        )
    return lines


def build_negotiation_brief(
    chain: list[AgentResult],
    latest: AgentResult,
    delta_lines: list[str] | None = None,
    first_export: bool = False,
) -> str:
    """Assemble the brief markdown for the chain ending at ``latest``.

    ``chain`` is the negotiation's stored rounds, oldest → newest
    (``ContractService._chain_rounds``); ``latest`` is its final
    element — the current draft under review. ``delta_lines`` /
    ``first_export`` carry the since-the-last-brief comparison the
    service computed from the export lineage; direct callers who
    pass neither get the brief without that section."""
    lines: list[str] = []
    lines.append(f"# Negotiation brief — {latest.contract_id}")
    lines.append("")
    lines.append(
        "**DRAFT — FOR COUNSEL REVIEW.** Assembled in code from the "
        "stored review records; every figure below traces to one. "
        "This brief states no legal conclusion, approves nothing, and "
        "is never sent by the agent — counsel takes it into the "
        "negotiation."
    )
    lines.append("")
    lines.append(f"- Counterparty: {latest.counterparty_name or '—'}")
    lines.append(f"- Document type: {latest.doc_type or '—'}")
    lines.append(f"- Status: {latest.status}")
    if latest.facts:
        lines.append(
            f"- Current posture: {latest.facts.posture} "
            f"(worst clause position: {latest.facts.worst_position})"
        )
    lines.append("")

    # -- Since the last brief: the delta against the previous export -------
    if first_export or delta_lines is not None:
        lines.append("## Since the last brief")
        lines.append("")
        if first_export:
            lines.append(
                "This is the first brief exported for this negotiation "
                "— there is no earlier export to compare against. "
                "Every later export leads with what changed since "
                "the one before it."
            )
        else:
            for line in delta_lines or []:
                lines.append(f"- {line}")
        lines.append("")

    # -- The chain: how the negotiation got here -------------------------
    lines.append("## Negotiation chain")
    lines.append("")
    if len(chain) <= 1:
        lines.append(
            "A single round is on record — no earlier draft of this "
            "contract has been reviewed here, so there is no movement "
            "to report."
        )
    else:
        ids = " → ".join(r.contract_id for r in chain)
        lines.append(f"{len(chain)} rounds on record: {ids}")
        lines.append("")
        lines.append("| Round | Posture | Worst position | Walk-away clauses | Flagged clauses |")
        lines.append("|---|---|---|---|---|")
        for result in chain:
            facts = result.facts
            lines.append(
                f"| {result.contract_id} | {facts.posture if facts else '—'} "
                f"| {facts.worst_position if facts else '—'} "
                f"| {_walk_away_count(result)} | {_flagged_count(result)} |"
            )
        lines.append("")
        chain_report = latest.round_chain
        if chain_report is not None:
            lines.append(
                f"Cumulative movement across the chain: "
                f"{chain_report.cumulative_improved} improved, "
                f"{chain_report.cumulative_worsened} worsened, "
                f"{chain_report.cumulative_unchanged} unchanged. "
                + (
                    f"No walk-away position remained by round "
                    f"{chain_report.rounds_to_no_walk_away}."
                    if chain_report.rounds_to_no_walk_away
                    else "A walk-away position is still open in the "
                    "current round."
                )
            )
            lines.append("")
            lines.append("| Clause | History (oldest → current) | Cumulative | Re-traded |")
            lines.append("|---|---|---|---|")
            for history in chain_report.histories:
                track = " → ".join(
                    p.position or "absent" for p in history.track
                )
                lines.append(
                    f"| {history.heading or history.clause_type} "
                    f"({history.clause_type}) | {track} "
                    f"| {history.cumulative_movement} "
                    f"| {'YES — ' + history.re_trade_detail if history.re_traded else 'no'} |"
                )
            lines.append("")
            if chain_report.re_trades:
                lines.append(
                    f"**Re-trades: {chain_report.re_trades}.** A "
                    "re-traded clause improved in an earlier round and "
                    "was given back later — settled ground the "
                    "counterparty reopened. Counsel will want the "
                    "reason before conceding anything near it."
                )
                lines.append("")
        elif latest.round_diff is not None:
            diff = latest.round_diff
            lines.append(
                f"Movement in the last round (vs "
                f"{diff.previous_contract_id}): {diff.improved} improved, "
                f"{diff.worsened} worsened, {diff.unchanged} unchanged, "
                f"{diff.new_clauses} new, {diff.removed_clauses} removed."
            )
            lines.append("")
            for line in diff.summary:
                lines.append(f"- {line}")
            lines.append("")

    # -- Open positions, by the tier the packet proposes ------------------
    lines.append("## Open positions by tier")
    lines.append("")
    if not latest.clause_diffs:
        lines.append(
            "No flagged clauses in the current round — every clause "
            "sits at the playbook's preferred position."
        )
    else:
        for tier, heading in (
            (2, "Tier 2 — the walk-away line (final positions)"),
            (1, "Tier 1 — first fallbacks"),
            (0, "No tier proposed — counsel drafts"),
        ):
            group = [d for d in latest.clause_diffs if d.proposed_tier == tier]
            if not group:
                continue
            lines.append(f"### {heading}")
            lines.append("")
            for diff in group:
                ref = f" §{diff.ref}" if diff.ref else ""
                lines.append(
                    f"- **{diff.clause_id}{ref} — {diff.clause_type}** "
                    f"({diff.position}): {diff.rationale}"
                )
                language = diff.tier_language or diff.playbook_fallback
                if language:
                    lines.append(f"  - Proposed language: {language}")
                if diff.tier_reason:
                    lines.append(f"  - Why this tier: {diff.tier_reason}")
            lines.append("")

    # -- Proposed edits ----------------------------------------------------
    lines.append("## Proposed edits (draft for counsel review)")
    lines.append("")
    if not latest.proposed_edits:
        lines.append("No edits proposed — there are no flagged clauses to map edits onto.")
    else:
        outcomes_by_index = {o.edit_index: o for o in latest.edit_outcomes}
        for index, edit in enumerate(latest.proposed_edits):
            lines.append(f"### {edit.clause_id} — {edit.clause_type} ({edit.kind})")
            lines.append("")
            if edit.original_sentence:
                lines.append(f"- Out: {edit.original_sentence}")
            if edit.anchor_sentence:
                lines.append(f"- Insert after: {edit.anchor_sentence}")
            if edit.replacement_text:
                lines.append(f"- In: {edit.replacement_text}")
            lines.append(f"- Note: {edit.note}")
            outcome = outcomes_by_index.get(index)
            if outcome is not None:
                lines.append(
                    f"- Counsel outcome: **{outcome.outcome}**"
                    + (f" — {outcome.note}" if outcome.note else "")
                    + (f" (marked by {outcome.marked_by})" if outcome.marked_by else "")
                )
                if outcome.modified_text:
                    lines.append(f"- Settled language: {outcome.modified_text}")
            lines.append("")

    # -- Edit outcomes across the chain + reflection ------------------------
    lines.append("## Edit outcomes and reflection")
    lines.append("")
    rounds_with_outcomes = [r for r in chain if r.edit_outcomes]
    if not rounds_with_outcomes and not latest.edit_reflection:
        lines.append(
            "No per-edit outcomes are on record yet. Counsel marks "
            "each proposed edit accepted, modified, or rejected as the "
            "negotiation settles it; the next round's brief then checks "
            "the agreed edits against the new draft's text."
        )
        lines.append("")
    else:
        for result in rounds_with_outcomes:
            lines.append(f"### Outcomes recorded on {result.contract_id}")
            lines.append("")
            for outcome in result.edit_outcomes:
                lines.append(
                    f"- Edit {outcome.edit_index} — {outcome.clause_id} "
                    f"{outcome.clause_type} ({outcome.kind}): "
                    f"**{outcome.outcome}**"
                    + (f" — {outcome.note}" if outcome.note else "")
                )
            lines.append("")
        if latest.edit_reflection:
            lines.append(
                f"### Reflection — agreed edits checked against {latest.contract_id}"
            )
            lines.append("")
            lines.append(
                "The check is textual: how much of each agreed edit's "
                "new language appears in the new draft's aligned "
                "clause. Counsel judges substance; this flags where "
                "to look."
            )
            lines.append("")
            for entry in latest.edit_reflection:
                flag = (
                    "**AGREED BUT NOT REFLECTED** — "
                    if entry.status == "agreed_not_reflected"
                    else ""
                )
                lines.append(
                    f"- {entry.clause_type} ({entry.kind}, "
                    f"{entry.outcome}): {flag}{entry.status} — {entry.detail}"
                )
            lines.append("")
        if latest.reflection_patterns:
            lines.append(
                "### Reflection patterns — agreed twice, reflected neither time"
            )
            lines.append("")
            lines.append(
                "One missed agreement is a drafting oversight. The "
                "clause types below were agreed, missed, agreed again, "
                "and missed again — flagged in two consecutive "
                "follow-ups. That is a pattern in how this negotiation "
                "is going, and the current packet answers it: each of "
                "these clauses is proposed at the walk-away line."
            )
            lines.append("")
            for pattern in latest.reflection_patterns:
                lines.append(
                    f"- **{pattern.clause_type}** — flagged "
                    "agreed-but-not-reflected in "
                    f"{' → '.join(pattern.flagged_in)}, checking the "
                    "edits agreed in "
                    f"{' and '.join(pattern.agreed_in)} "
                    f"({pattern.flags} missed agreement(s) on record "
                    "for this clause type across the chain)."
                )
            lines.append("")

    # -- Exhibit findings ---------------------------------------------------
    lines.append("## Exhibit findings")
    lines.append("")
    exhibit_rows = list(latest.exhibit_checks) + list(latest.subprocessor_drift)
    if not exhibit_rows:
        lines.append(
            "No cross-document checks were generated for this package "
            "— the agreement ties nothing checkable to an exhibit."
        )
    else:
        lines.append("| Check | Promised | Exhibit says | Status | Detail |")
        lines.append("|---|---|---|---|---|")
        for check in exhibit_rows:
            lines.append(
                f"| {check.name} | {check.promised or '—'} "
                f"| {check.found or '—'} | {check.status} | {check.detail} |"
            )
        if latest.subprocessor_drift:
            lines.append("")
            lines.append(
                "The `subprocessor_list_drift` row is cross-round: "
                "the annex's movement since the previous round of "
                "this chain, with any addition read against the "
                "notice period the clause promises."
            )
    lines.append("")

    # -- Obligations ----------------------------------------------------------
    lines.append("## Obligations register (current round)")
    lines.append("")
    if not latest.obligations:
        lines.append("No dated or recurring obligations were extracted from the current round.")
    else:
        lines.append("| Kind | Obligation | Detail | When | Source |")
        lines.append("|---|---|---|---|---|")
        for obligation in latest.obligations:
            when = (
                obligation.date
                or (f"{obligation.days} days" if obligation.days is not None else "")
                or (f"{obligation.hours} hours" if obligation.hours is not None else "—")
            )
            if obligation.date and obligation.computed:
                when += " (computed)"
            lines.append(
                f"| {obligation.kind} | {obligation.description} "
                f"| {obligation.detail or '—'} | {when} "
                f"| {obligation.source_clause_id} |"
            )
    lines.append("")
    lines.append(
        "---\nGenerated by the Contract Review blueprint from stored "
        "review records. Draft for counsel review — not a legal "
        "conclusion, not an approval, not a redline."
    )
    return "\n".join(lines) + "\n"
