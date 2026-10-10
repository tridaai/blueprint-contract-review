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

The delta also narrates the chain's covenant movements (round
19): the digest carries the covenant biography's step readings
(``covenant_biography.py``) for the steps the chain has taken,
and the delta names the movements of the steps that arrived
since the previous export — a covenant introduced, dropped, its
tail changed, its expiry moved — in the biography's own summary
sentences, so the brief and the annex-history view cannot tell
the story two ways.
"""

from __future__ import annotations

import hashlib

from .schemas import AgentResult, WalkAwayOutcome


def _flagged_count(result: AgentResult) -> int:
    return sum(1 for v in result.clause_verdicts if v.position != "preferred")


def _walk_away_count(result: AgentResult) -> int:
    return sum(1 for v in result.clause_verdicts if v.position == "walk_away")


# ---------------------------------------------------------------------------
# Brief digests + deltas: what changed since the last export
# ---------------------------------------------------------------------------


def brief_source_digest(
    chain: list[AgentResult],
    latest: AgentResult,
    covenant_steps: list[dict] | None = None,
) -> dict:
    """The facts a brief is built from, as a comparable digest.

    Stored with every export (in the brief lineage index, see
    ``ContractService.negotiation_brief``) so the NEXT export can
    diff its facts against this one's and lead with the delta —
    the Monday-morning question is never "what does the brief
    say?" but "what changed since the brief I already read?".
    Everything here is copied from stored results; an edit is keyed
    by a hash of its proposed language, so re-proposing the same
    language is not news and new language is.

    ``covenant_steps`` is the covenant biography's step readings
    for this chain (``covenant_biography.py``), carried verbatim —
    the from/to rounds, each step's movements, and the biography's
    own summary sentence — so the delta can narrate a renegotiated
    covenant in the biography's words rather than re-deriving the
    movement here. Without a biography (a chain the service read
    no covenants from) the digest carries no steps and the delta
    says nothing about covenants, as before."""
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
        "certificate_renewal": [
            {"status": c.status, "found": c.found}
            for c in latest.certificate_renewal
        ],
        "prior_invention_drift": [
            {"status": c.status, "found": c.found}
            for c in latest.prior_invention_drift
        ],
        "lapsed_covenant_readings": [
            {"status": c.status, "found": c.found, "detail": c.detail}
            for c in latest.lapsed_covenant_readings
        ],
        "covenant_steps": [
            {
                "from_contract_id": step.get("from_contract_id", ""),
                "to_contract_id": step.get("to_contract_id", ""),
                "movements": [
                    {
                        "kind": m.get("kind", ""),
                        "change": m.get("change", ""),
                        "detail": m.get("detail", ""),
                    }
                    for m in step.get("movements", [])
                ],
                "summary": step.get("summary", ""),
            }
            for step in (covenant_steps or [])
        ],
        "provenance_warnings": line_provenance_warnings(chain, latest),
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
    cur_renewal = current.get("certificate_renewal", [])
    prev_renewal = previous.get("certificate_renewal", [])
    if cur_renewal and cur_renewal != prev_renewal:
        renewal = cur_renewal[0]
        lines.append(
            f"Certificate renewal vs the original terms: "
            f"{renewal['found']} ({renewal['status']}) — see "
            "Exhibit findings."
        )
    cur_pi = current.get("prior_invention_drift", [])
    prev_pi = previous.get("prior_invention_drift", [])
    if cur_pi and cur_pi != prev_pi:
        pi = cur_pi[0]
        lines.append(
            f"Prior-invention schedule drift vs the prior round: "
            f"{pi['found']} ({pi['status']}) — see Exhibit "
            "findings."
        )
    cur_lapsed = current.get("lapsed_covenant_readings", [])
    prev_lapsed = previous.get("lapsed_covenant_readings", [])
    if cur_lapsed and cur_lapsed != prev_lapsed:
        lines.append(
            "Lapsed covenant read against this round: this "
            "letter's fresh covenant follows a lapsed one in the "
            "chain — a new bargain, not a continuation — see "
            "Exhibit findings."
        )
    # Covenant movements (round 19): the steps that ARRIVED since
    # the previous export — a step's later round sits in the chain
    # past the round that export covered — narrated in the
    # covenant biography's own summary sentences, so the delta
    # and the annex-history biography cannot disagree. A step
    # whose covenants were all carried unchanged is not news (the
    # new round itself already leads the delta); a step that
    # introduced, dropped, re-tailed or re-dated a covenant is.
    chain = current.get("chain", [])
    previous_id = previous.get("contract_id", "")
    previous_index = chain.index(previous_id) if previous_id in chain else -1
    for step in current.get("covenant_steps", []):
        to_id = step.get("to_contract_id", "")
        if to_id not in chain or chain.index(to_id) <= previous_index:
            continue
        if not any(
            movement.get("change") not in ("", "carried")
            for movement in step.get("movements", [])
        ):
            continue
        lines.append(
            f"Covenant movement since the last brief "
            f"({step.get('from_contract_id', '')} → {to_id}): "
            f"{step.get('summary', '')}"
        )
    prev_warnings = set(previous.get("provenance_warnings", []))
    for warning in current.get("provenance_warnings", []):
        if warning not in prev_warnings:
            lines.append(
                f"New line-provenance warning since the last brief: "
                f"{warning} — see Line provenance."
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


# ---------------------------------------------------------------------------
# Line provenance: who moved the walk-away lines, named before conceding
# ---------------------------------------------------------------------------

_OUTCOME_PHRASES = {
    "held": "the follow-up round held it at or above the line",
    "softened": "the follow-up round softened it — off the line, short of acceptable",
    "dropped": "the follow-up round still sits beyond it",
    "clause_removed": "the clause left the draft entirely — the line was neither held nor tested",
    "pending": "no follow-up round has answered it yet",
}


def line_provenance_warnings(
    chain: list[AgentResult], latest: AgentResult
) -> list[str]:
    """Warnings from the chain's decided walk-away lines, read by
    ORIGIN (``walkaway.py``'s ``classify_line_origin``): a line can
    be lost at counsel's own desk — modified or rejected before it
    ever went out — or at the counterparty's, and the next
    concession is a different decision depending on which. Counsel
    sees the provenance here, in the brief, before conceding —
    not in a profile after the fact.

    Three kinds of line, all straight off the stored outcomes:

    - a line counsel's desk **modified** (or **rejected**) before
      it went out, with what the follow-up then did with it;
    - a line that went out **intact** and the counterparty still
      moved — provenance clean, movement theirs;
    - the desk's own **pattern**: where most of a clause type's
      decided lines in this chain were modified at counsel's desk
      and the current packet again proposes the playbook's line
      for that type, the brief says so while the choice is still
      counsel's to make differently.
    """
    warnings: list[str] = []
    decided: list[tuple[AgentResult, WalkAwayOutcome]] = [
        (result, outcome)
        for result in chain
        for outcome in result.walk_away_outcomes
    ]
    for result, outcome in decided:
        phrase = _OUTCOME_PHRASES.get(
            outcome.status, f"its outcome is {outcome.status or 'unrecorded'}"
        )
        if outcome.origin == "modified_by_counsel":
            warnings.append(
                f"**{outcome.clause_type}** — decided on "
                f"{result.contract_id}: counsel's own desk modified "
                "this walk-away line before it went out. "
                f"{outcome.origin_detail} Against the follow-up, "
                f"{phrase}. What was conceded was counsel's settled "
                "language, not the packet's line."
            )
        elif outcome.origin == "rejected_by_counsel":
            warnings.append(
                f"**{outcome.clause_type}** — decided on "
                f"{result.contract_id}: counsel rejected the edit "
                "carrying this walk-away line at decision time — "
                "the line as proposed never reached the "
                f"counterparty. Against the follow-up, {phrase}. "
                "Read that outcome with its provenance: this line "
                "was lost at counsel's desk first."
            )
        elif outcome.origin == "sent_intact" and outcome.status in (
            "softened",
            "dropped",
        ):
            warnings.append(
                f"**{outcome.clause_type}** — decided on "
                f"{result.contract_id}: this line went out exactly "
                f"as the packet proposed it, and {phrase}. The "
                "provenance is clean — the movement is the "
                "counterparty's."
            )
    # The desk's own pattern, per clause type: lines whose origin
    # is recorded at all (unmarked lines say nothing about the
    # desk), and how many of them the desk itself modified.
    by_type: dict[str, dict[str, int]] = {}
    for _result, outcome in decided:
        if outcome.origin not in (
            "sent_intact",
            "modified_by_counsel",
            "rejected_by_counsel",
        ):
            continue
        slot = by_type.setdefault(
            outcome.clause_type, {"decided": 0, "modified": 0}
        )
        slot["decided"] += 1
        if outcome.origin == "modified_by_counsel":
            slot["modified"] += 1
    current_lines = {
        d.clause_type for d in latest.clause_diffs if d.proposed_tier == 2
    }
    for clause_type in sorted(by_type):
        slot = by_type[clause_type]
        if (
            slot["modified"] >= 2
            and slot["modified"] * 2 >= slot["decided"]
            and clause_type in current_lines
        ):
            warnings.append(
                f"**Pattern at counsel's own desk — {clause_type}**: "
                f"{slot['modified']} of the {slot['decided']} decided "
                "walk-away lines for this clause type in this chain "
                "were modified by counsel before they went out. The "
                "current packet again proposes the playbook's line "
                "for it, unmodified. That is counsel's choice to "
                "make — the record above is what the modified lines "
                "did."
            )
    return warnings


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

    # -- Line provenance: who moved the lines, before counsel concedes ----
    provenance_warnings = line_provenance_warnings(chain, latest)
    if provenance_warnings:
        lines.append("## Line provenance — read before conceding")
        lines.append("")
        lines.append(
            "A walk-away line can be lost in two places: at "
            "counsel's own desk, before it goes out, or at the "
            "counterparty's, after. The warnings below read this "
            "chain's decided lines by their origin — provenance to "
            "weigh before conceding anything near these clauses "
            "again."
        )
        lines.append("")
        for warning in provenance_warnings:
            lines.append(f"- ⚠ {warning}")
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
    exhibit_rows = (
        list(latest.exhibit_checks)
        + list(latest.subprocessor_drift)
        + list(latest.certificate_renewal)
        + list(latest.prior_invention_drift)
        + list(latest.lapsed_covenant_readings)
    )
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
        if latest.certificate_renewal:
            lines.append("")
            lines.append(
                "The `insurance_certificate_renewal` row is "
                "cross-round: the renewal certificate read against "
                "the ORIGINAL round's insurance requirement set — "
                "the renewal watch closes on this evidence, not "
                "on the certificate's arrival."
            )
        if latest.prior_invention_drift:
            lines.append("")
            lines.append(
                "The `prior_invention_schedule_drift` row is "
                "cross-round: the schedule's movement since the "
                "previous round of this chain — a removal is a "
                "finding, because the assignment excludes what "
                "the schedule lists."
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
