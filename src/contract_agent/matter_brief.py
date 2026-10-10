"""The matter brief: one document across a whole matter.

The negotiation brief (``brief.py``) serves one chain — one
contract's rounds. A matter is wider: several chains with one
counterparty under one matter tag (the platform MSA and its SOW,
the renewal and the amendment riding on it). Counsel walking into
a matter-level call needs the combined picture: every chain's open
positions in one place, the exhibit findings across all the
documents, and every dated obligation the matter carries, sorted
by when it falls due — not four briefs to cross-reference by hand.

This module assembles that document in code from the stored
records, with the chain brief's discipline: every figure traces to
a stored result, nothing is model-composed, and the document is
labelled what it is — a draft for counsel review that states no
legal conclusion and is sent nowhere by the agent. Exports form a
lineage exactly like the chain brief's (see
``ContractService.matter_brief``): each export's source digest is
retained, and the next export leads with a "Since the last matter
brief" delta — the chains that advanced, the decisions recorded,
the obligations opened or closed, the exhibit failures that
appeared or cleared.

A chain belongs to the matter when its contracts carry the matter
tag and the matter's counterparty: grouping is read off the stored
intake payloads, and a contract with no tag is in no matter.
"""

from __future__ import annotations

import re

from .concessions import _chains
from .schemas import AgentResult
from .store import ApprovalRecord


def matter_records(
    records: list[ApprovalRecord], counterparty: str, matter: str
) -> list[list[ApprovalRecord]]:
    """The matter's chains, oldest → newest within each: the
    stored records whose intake carries this counterparty and
    matter tag, grouped by their ``supersedes`` links."""
    mine = [
        record
        for record in records
        if (record.contract or {}).get("matter", "") == matter
        and (
            record.result.counterparty_name
            or (record.contract or {}).get("counterparty_name", "")
        )
        == counterparty
    ]
    return _chains(mine)


def matter_slug(counterparty: str, matter: str) -> str:
    """The object-store slug for a matter: counterparty and tag,
    reduced to key-safe characters."""
    def clean(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "matter"

    return f"{clean(counterparty)}--{clean(matter)}"


def _open_walk_aways(result: AgentResult) -> int:
    return sum(1 for d in result.clause_diffs if d.proposed_tier == 2)


def _failed_checks(result: AgentResult) -> list[str]:
    return sorted(
        check.name for check in result.exhibit_checks if check.status != "consistent"
    )


# ---------------------------------------------------------------------------
# Digest + delta: what changed since the last export
# ---------------------------------------------------------------------------


def matter_source_digest(
    counterparty: str, matter: str, chains: list[list[ApprovalRecord]]
) -> dict:
    """The facts a matter brief is built from, as a comparable
    digest — stored with every export so the next one can lead
    with the delta. Keyed by chain root: a chain is its root's id,
    and its state is its latest round."""
    latest = [chain[-1].result for chain in chains]
    obligation_entries: dict[tuple[str, str, str, str], dict] = {}
    for result in latest:
        for obligation in result.obligations:
            entry = {
                "contract_id": result.contract_id,
                "kind": obligation.kind,
                "detail": obligation.detail,
                "date": obligation.date or "",
            }
            obligation_entries[
                (entry["contract_id"], entry["kind"], entry["detail"], entry["date"])
            ] = entry
    return {
        "counterparty": counterparty,
        "matter": matter,
        "chains": {
            chain[0].result.contract_id: chain[-1].result.contract_id
            for chain in chains
        },
        "rounds": {
            chain[0].result.contract_id: len(chain) for chain in chains
        },
        "statuses": {r.contract_id: r.status for r in latest},
        "decisions": {r.contract_id: r.decision or "" for r in latest},
        "open_walk_aways": {r.contract_id: _open_walk_aways(r) for r in latest},
        "exhibit_failures": {r.contract_id: _failed_checks(r) for r in latest},
        "obligations": [
            obligation_entries[key] for key in sorted(obligation_entries)
        ],
    }


def matter_delta_lines(previous: dict, current: dict) -> list[str]:
    """What changed between the previous export's digest and this
    one's: chains added, chains advanced, decisions recorded,
    walk-away lines opened or closed, obligations opened or
    closed, exhibit failures appearing or clearing. An empty diff
    says so."""
    lines: list[str] = []
    prev_chains, cur_chains = previous["chains"], current["chains"]
    for root in sorted(set(cur_chains) - set(prev_chains)):
        lines.append(
            f"New chain in the matter since the last brief: "
            f"{cur_chains[root]} (root {root})."
        )
    for root in sorted(set(prev_chains) - set(cur_chains)):
        lines.append(
            f"Chain no longer in the matter: {prev_chains[root]} "
            f"(root {root})."
        )
    for root in sorted(set(prev_chains) & set(cur_chains)):
        if prev_chains[root] != cur_chains[root]:
            lines.append(
                f"Chain advanced: {prev_chains[root]} → "
                f"{cur_chains[root]} (now {current['rounds'][root]} "
                "round(s))."
            )
    for contract_id in sorted(set(current["decisions"]) & set(previous["decisions"])):
        if current["decisions"][contract_id] and (
            current["decisions"][contract_id] != previous["decisions"][contract_id]
        ):
            lines.append(
                f"Decision recorded since the last brief: "
                f"{current['decisions'][contract_id]} on {contract_id}."
            )
    for contract_id in sorted(
        set(current["open_walk_aways"]) & set(previous["open_walk_aways"])
    ):
        before, now = (
            previous["open_walk_aways"][contract_id],
            current["open_walk_aways"][contract_id],
        )
        if before != now:
            lines.append(
                f"Open walk-away lines on {contract_id}: {before} → {now}."
            )
    for contract_id in sorted(
        set(current["exhibit_failures"]) & set(previous["exhibit_failures"])
    ):
        appeared = sorted(
            set(current["exhibit_failures"][contract_id])
            - set(previous["exhibit_failures"][contract_id])
        )
        cleared = sorted(
            set(previous["exhibit_failures"][contract_id])
            - set(current["exhibit_failures"][contract_id])
        )
        for name in appeared:
            lines.append(f"New exhibit failure on {contract_id}: {name}.")
        for name in cleared:
            lines.append(f"Exhibit failure cleared on {contract_id}: {name}.")

    def obligation_key(obligation: dict) -> tuple[str, str, str, str]:
        return (
            obligation["contract_id"],
            obligation["kind"],
            obligation["detail"],
            obligation["date"],
        )

    prev_obligations = {obligation_key(o): o for o in previous["obligations"]}
    cur_obligations = {obligation_key(o): o for o in current["obligations"]}
    for key in sorted(set(cur_obligations) - set(prev_obligations)):
        obligation = cur_obligations[key]
        lines.append(
            f"New obligation in the matter: {obligation['contract_id']} — "
            f"{obligation['kind']}: {obligation['detail']}"
            + (f" ({obligation['date']})" if obligation["date"] else "")
            + "."
        )
    for key in sorted(set(prev_obligations) - set(cur_obligations)):
        obligation = prev_obligations[key]
        lines.append(
            f"Obligation closed or no longer in the matter: "
            f"{obligation['contract_id']} — {obligation['kind']}: "
            f"{obligation['detail']}."
        )
    if not lines:
        lines.append(
            "Nothing material changed since the last matter brief — "
            "the facts below are the same facts, re-exported."
        )
    return lines


# ---------------------------------------------------------------------------
# The brief itself
# ---------------------------------------------------------------------------


def build_matter_brief(
    counterparty: str,
    matter: str,
    chains: list[list[ApprovalRecord]],
    delta_lines: list[str] | None = None,
    first_export: bool = False,
) -> str:
    """Assemble the matter brief markdown across ``chains`` (each
    oldest → newest). Cross-contract views read each chain's LATEST
    round — the current state of that negotiation — while the
    chain table keeps the whole history visible."""
    latest = [chain[-1].result for chain in chains]
    lines: list[str] = []
    lines.append(f"# Matter brief — {matter}")
    lines.append("")
    lines.append(
        "**DRAFT — FOR COUNSEL REVIEW.** Assembled in code from the "
        "stored review records across this matter's chains; every "
        "figure below traces to one. This brief states no legal "
        "conclusion, approves nothing, and is never sent by the "
        "agent — counsel takes it into the matter-level call."
    )
    lines.append("")
    lines.append(f"- Counterparty: {counterparty}")
    lines.append(f"- Matter: {matter}")
    lines.append(
        f"- Chains: {len(chains)} · contracts on record: "
        f"{sum(len(chain) for chain in chains)}"
    )
    total_walk_aways = sum(_open_walk_aways(result) for result in latest)
    lines.append(
        f"- Open walk-away lines across the matter: {total_walk_aways}"
    )
    lines.append("")

    if first_export or delta_lines is not None:
        lines.append("## Since the last matter brief")
        lines.append("")
        if first_export:
            lines.append(
                "This is the first brief exported for this matter — "
                "there is no earlier export to compare against. "
                "Every later export leads with what changed since "
                "the one before it."
            )
        else:
            for line in delta_lines or []:
                lines.append(f"- {line}")
        lines.append("")

    # -- The chains ------------------------------------------------------
    lines.append("## Chains in this matter")
    lines.append("")
    lines.append(
        "| Chain | Rounds | Latest | Status | Decision | Worst position | Open walk-away lines |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for chain in chains:
        result = chain[-1].result
        lines.append(
            f"| {' → '.join(r.result.contract_id for r in chain)} "
            f"| {len(chain)} | {result.contract_id} | {result.status} "
            f"| {result.decision or '—'} "
            f"| {result.facts.worst_position if result.facts else '—'} "
            f"| {_open_walk_aways(result)} |"
        )
    lines.append("")

    # -- Combined open positions ------------------------------------------
    lines.append("## Open positions across the matter")
    lines.append("")
    flagged = [
        (result, diff)
        for result in latest
        for diff in result.clause_diffs
    ]
    if not flagged:
        lines.append(
            "No flagged clauses in any chain's current round — every "
            "clause across the matter sits at the playbook's "
            "preferred position."
        )
    else:
        for tier, heading in (
            (2, "Tier 2 — walk-away lines open anywhere in the matter"),
            (1, "Tier 1 — first fallbacks"),
            (0, "No tier proposed — counsel drafts"),
        ):
            group = [(r, d) for r, d in flagged if d.proposed_tier == tier]
            if not group:
                continue
            lines.append(f"### {heading}")
            lines.append("")
            for result, diff in group:
                ref = f" §{diff.ref}" if diff.ref else ""
                lines.append(
                    f"- **{result.contract_id} · {diff.clause_id}{ref} — "
                    f"{diff.clause_type}** ({diff.position}): {diff.rationale}"
                )
                language = diff.tier_language or diff.playbook_fallback
                if language:
                    lines.append(f"  - Proposed language: {language}")
            lines.append("")

    # -- Reflection patterns across the matter -----------------------------
    patterns = [
        (result, pattern)
        for result in latest
        for pattern in result.reflection_patterns
    ]
    if patterns:
        lines.append("## Reflection patterns across the matter")
        lines.append("")
        lines.append(
            "Clause types in this matter whose agreed edits were "
            "flagged agreed-but-not-reflected in two consecutive "
            "follow-ups (see each chain's brief for the round "
            "history) — their packets propose the walk-away line."
        )
        lines.append("")
        for result, pattern in patterns:
            lines.append(
                f"- **{pattern.clause_type}** on {result.contract_id} — "
                f"flagged in {' → '.join(pattern.flagged_in)} "
                f"({pattern.flags} missed agreement(s) on record)."
            )
        lines.append("")

    # -- Exhibit findings across the matter --------------------------------
    lines.append("## Exhibit findings across the matter")
    lines.append("")
    checks = [
        (result, check) for result in latest for check in result.exhibit_checks
    ]
    if not checks:
        lines.append(
            "No cross-document checks were generated anywhere in "
            "this matter — no agreement in it ties anything checkable "
            "to an exhibit."
        )
    else:
        lines.append("| Contract | Check | Promised | Exhibit says | Status | Detail |")
        lines.append("|---|---|---|---|---|---|")
        for result, check in checks:
            lines.append(
                f"| {result.contract_id} | {check.name} "
                f"| {check.promised or '—'} | {check.found or '—'} "
                f"| {check.status} | {check.detail} |"
            )
    lines.append("")

    # -- Obligations across the matter, by deadline -------------------------
    lines.append("## Obligations across the matter, by deadline")
    lines.append("")
    obligations = [
        (result, obligation)
        for result in latest
        for obligation in result.obligations
    ]
    if not obligations:
        lines.append(
            "No dated or recurring obligations were extracted from "
            "any chain's current round."
        )
    else:
        dated = sorted(
            (pair for pair in obligations if pair[1].date),
            key=lambda pair: (pair[1].date, pair[0].contract_id),
        )
        undated = [pair for pair in obligations if not pair[1].date]
        lines.append("| Contract | Kind | Obligation | Detail | When | Source |")
        lines.append("|---|---|---|---|---|---|")
        for result, obligation in dated + undated:
            when = (
                obligation.date
                or (f"{obligation.days} days" if obligation.days is not None else "")
                or (f"{obligation.hours} hours" if obligation.hours is not None else "—")
            )
            if obligation.date and obligation.computed:
                when += " (computed)"
            lines.append(
                f"| {result.contract_id} | {obligation.kind} "
                f"| {obligation.description} | {obligation.detail or '—'} "
                f"| {when} | {obligation.source_clause_id} |"
            )
    lines.append("")
    lines.append(
        "---\nGenerated by the Contract Review blueprint from stored "
        "review records. Draft for counsel review — not a legal "
        "conclusion, not an approval, not a redline."
    )
    return "\n".join(lines) + "\n"
