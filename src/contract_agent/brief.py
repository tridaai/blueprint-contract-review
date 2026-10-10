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
"""

from __future__ import annotations

from .schemas import AgentResult


def _flagged_count(result: AgentResult) -> int:
    return sum(1 for v in result.clause_verdicts if v.position != "preferred")


def _walk_away_count(result: AgentResult) -> int:
    return sum(1 for v in result.clause_verdicts if v.position == "walk_away")


def build_negotiation_brief(chain: list[AgentResult], latest: AgentResult) -> str:
    """Assemble the brief markdown for the chain ending at ``latest``.

    ``chain`` is the negotiation's stored rounds, oldest → newest
    (``ContractService._chain_rounds``); ``latest`` is its final
    element — the current draft under review."""
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

    # -- Exhibit findings ---------------------------------------------------
    lines.append("## Exhibit findings")
    lines.append("")
    if not latest.exhibit_checks:
        lines.append(
            "No cross-document checks were generated for this package "
            "— the agreement ties nothing checkable to an exhibit."
        )
    else:
        lines.append("| Check | Promised | Exhibit says | Status | Detail |")
        lines.append("|---|---|---|---|---|")
        for check in latest.exhibit_checks:
            lines.append(
                f"| {check.name} | {check.promised or '—'} "
                f"| {check.found or '—'} | {check.status} | {check.detail} |"
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
