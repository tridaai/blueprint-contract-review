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

The round that widened the brief assembled it as DATA first
(``build_matter_brief_data``) and renders the markdown from that
data, so the same assembly exports as JSON beside the markdown and
the eval pack can score the assembly itself. The widening added
the views that only exist at matter scope:

- **Lineage on every open walk-away line** — the round of its
  chain that first saw the clause, the round that first proposed
  the walk-away line for it, how many rounds it has sat at the
  line, and whether its position moved along the way (read off the
  chain's stored rounds, aligned by clause type the way the
  pattern machinery aligns).
- **Matter patterns** (``find_matter_patterns``) — a clause type
  whose agreed edits were flagged agreed-but-not-reflected, with
  the miss still open, in TWO OR MORE of the matter's chains. No
  single chain needs to show a pattern for the matter to have one:
  the same ask being missed on two documents of one matter is a
  fact about the counterparty's drafting, visible only when the
  chains are read together.
- **Shared obligations** (``shared_obligations``) — duties the
  register records in more than one chain of the matter, grouped
  by kind and operative text with the figures set aside, their
  stated terms side by side. A group is flagged a conflict when
  the chains state different figures for the same duty — computed
  from the register facts, never from model prose.
- **A matter roll-up** — clauses by position and open lines by
  tier summed across the chains' current rounds, and the chains
  counted by posture.
- **Walk-away line acceptance across the matter** — the chain
  briefs tally what became of each chain's approved walk-away
  lines (``walkaway.acceptance_report``); the matter brief sums
  the same report across all its chains and lists every approved
  line with its chain lineage, so "did our lines hold on this
  matter?" is answered in one place.
"""

from __future__ import annotations

import re

from .concessions import _chains
from .schemas import POSITIONS, POSITION_SEVERITY, AgentResult
from .store import ApprovalRecord
from .walkaway import acceptance_report


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


def _results(chain: list[ApprovalRecord]) -> list[AgentResult]:
    return [record.result for record in chain]


# ---------------------------------------------------------------------------
# Lineage: where an open line came from, read off its chain's rounds
# ---------------------------------------------------------------------------


def _type_trail(results: list[AgentResult], clause_type: str) -> list[dict]:
    """One clause type's trail through a chain's rounds, oldest →
    newest: the rounds where the type appears at all, with its
    worst verdict position and the highest tier the packet
    proposed for it in that round. Alignment is by clause type —
    the key the pattern machinery and the round diff already use,
    because the type is the ask that persists across drafts even
    when clause numbering shifts."""
    trail: list[dict] = []
    for result in results:
        positions = [
            v.position for v in result.clause_verdicts if v.clause_type == clause_type
        ]
        tiers = [
            d.proposed_tier for d in result.clause_diffs if d.clause_type == clause_type
        ]
        if not positions and not tiers:
            continue
        trail.append(
            {
                "contract_id": result.contract_id,
                "position": (
                    max(positions, key=lambda p: POSITION_SEVERITY.get(p, 0))
                    if positions
                    else ""
                ),
                "tier": max(tiers) if tiers else None,
            }
        )
    return trail


def _clause_lineage(results: list[AgentResult], clause_type: str) -> dict | None:
    """The lineage of one clause type in one chain: the round that
    first saw it, the round that first proposed the walk-away line
    for it (``""`` when no round has), how many rounds proposed
    the line, and whether the clause's position moved along the
    trail — first position against current position."""
    trail = _type_trail(results, clause_type)
    if not trail:
        return None
    tier2 = [entry for entry in trail if entry["tier"] == 2]
    seen_positions = [entry["position"] for entry in trail if entry["position"]]
    return {
        "first_seen_in": trail[0]["contract_id"],
        "walk_away_since": tier2[0]["contract_id"] if tier2 else "",
        "rounds_at_walk_away": len(tier2),
        "moved": len(set(seen_positions)) > 1,
        "from_position": seen_positions[0] if seen_positions else "",
        "to_position": seen_positions[-1] if seen_positions else "",
        "trail": trail,
    }


# ---------------------------------------------------------------------------
# Matter patterns: the miss no single chain shows
# ---------------------------------------------------------------------------

_MEASURABLE = ("reflected", "agreed_not_reflected")


def _chain_reflection_checks(
    results: list[AgentResult],
) -> dict[str, list[tuple[str, str]]]:
    """A chain's measurable reflection checks by clause type, in
    chain order, as (follow-up contract id, status) — read off the
    results the way ``reflection_patterns.find_reflection_patterns``
    reads them: each result after the first carries the reflection
    of its predecessor's agreed edits."""
    checks: dict[str, list[tuple[str, str]]] = {}
    for index, result in enumerate(results):
        if index == 0:
            continue  # the first round has no predecessor to reflect
        for entry in result.edit_reflection:
            if entry.status not in _MEASURABLE:
                continue
            checks.setdefault(entry.clause_type, []).append(
                (result.contract_id, entry.status)
            )
    return checks


def find_matter_patterns(chains: list[list[ApprovalRecord]]) -> list[dict]:
    """Clause types whose agreed edits keep not landing ACROSS the
    matter: flagged ``agreed_not_reflected`` in two or more of the
    matter's chains, with the miss still open in each — the
    chain's latest measurable check for the type is a miss, so a
    later landing closes the chain's contribution honestly. A
    chain whose own two-consecutive-miss pattern is active counts
    like any other flagging chain and is named as such; the matter
    pattern needs no active pattern anywhere — two chains missing
    the same ask once each is already the signal."""
    per_type: dict[str, list[dict]] = {}
    for chain in chains:
        results = _results(chain)
        if not results:
            continue
        active = {p.clause_type for p in results[-1].reflection_patterns}
        for clause_type, entries in _chain_reflection_checks(results).items():
            flagged_in = [
                contract_id
                for contract_id, status in entries
                if status == "agreed_not_reflected"
            ]
            if not flagged_in or entries[-1][1] != "agreed_not_reflected":
                continue  # no miss on record, or the latest check landed
            per_type.setdefault(clause_type, []).append(
                {
                    "root": results[0].contract_id,
                    "latest": results[-1].contract_id,
                    "flags": len(flagged_in),
                    "flagged_in": flagged_in,
                    "active_pattern": clause_type in active,
                }
            )
    patterns: list[dict] = []
    for clause_type in sorted(per_type):
        members = per_type[clause_type]
        if len(members) < 2:
            continue  # one chain's miss is that chain's business
        patterns.append(
            {
                "clause_type": clause_type,
                "total_flags": sum(m["flags"] for m in members),
                "chains_with_active_pattern": sum(
                    1 for m in members if m["active_pattern"]
                ),
                "chains": members,
            }
        )
    return patterns


# ---------------------------------------------------------------------------
# Shared obligations: the same duty in more than one chain
# ---------------------------------------------------------------------------

# Number words that carry figures in the register's composed
# details ("successive terms: one (1) year") — set aside with the
# digits when a duty's identity is computed.
_NUMBER_WORDS = {
    "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
    "fifteen", "twenty", "thirty", "forty", "fifty", "sixty",
    "seventy", "eighty", "ninety", "hundred", "thousand",
}


def _duty_key(obligation) -> tuple[str, str]:
    """A duty's identity across documents: its kind and its
    operative text (the register's ``detail``, falling back to the
    composed description) with every figure — digits and number
    words alike — set aside. Two obligations share a duty when
    this key matches; whether they state the same TERMS is a
    separate comparison over their figures, which is what makes a
    conflict detectable instead of definitional."""
    raw = (obligation.detail or obligation.description or "").lower()
    raw = re.sub(r"\d+", " ", raw)
    words = [
        word
        for word in re.split(r"[^a-z]+", raw)
        if word and word not in _NUMBER_WORDS
    ]
    return obligation.kind, " ".join(words)


def _duty_display(obligation) -> str:
    """The duty's operative text with the figures removed, for
    headings: "120 days' written notice" reads "days' written
    notice" — the duty itself, before any chain's figure for it."""
    raw = re.sub(r"\d+\s*", "", obligation.detail or obligation.description or "")
    return " ".join(raw.split()).strip(" —-") or obligation.kind


def shared_obligations(chains: list[list[ApprovalRecord]]) -> list[dict]:
    """The duties the register records in more than one chain of
    the matter, read from each chain's LATEST round like every
    other cross-contract view in the brief. Members carry their
    stated terms side by side; a group is a ``conflict`` when the
    chains state different figures for the duty — different
    day/hour counts, different operative text, or different
    explicit (non-computed) dates — and ``consistent`` when every
    chain states the same terms. Computed dates follow from a
    document's own figures, so they are shown but never compared:
    the figures are the terms."""
    groups: dict[tuple[str, str], dict] = {}
    for chain in chains:
        results = _results(chain)
        if not results:
            continue
        latest = results[-1]
        for obligation in latest.obligations:
            key = _duty_key(obligation)
            group = groups.setdefault(
                key,
                {
                    "kind": key[0],
                    "duty": _duty_display(obligation),
                    "members": [],
                },
            )
            group["members"].append(
                {
                    "chain_root": results[0].contract_id,
                    "contract_id": latest.contract_id,
                    "detail": obligation.detail,
                    "days": obligation.days,
                    "hours": obligation.hours,
                    "date": obligation.date,
                    "computed": obligation.computed,
                    "source_clause_id": obligation.source_clause_id,
                }
            )
    shared: list[dict] = []
    for key in sorted(groups):
        group = groups[key]
        roots = {member["chain_root"] for member in group["members"]}
        if len(roots) < 2:
            continue  # one chain's duty, however often it recurs there
        values = {
            (m["days"], m["hours"], m["detail"]) for m in group["members"]
        }
        explicit_dates = {
            m["date"] for m in group["members"] if m["date"] and not m["computed"]
        }
        group["status"] = (
            "conflict" if len(values) > 1 or len(explicit_dates) > 1 else "consistent"
        )
        shared.append(group)
    return shared


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
        "matter_patterns": [
            pattern["clause_type"] for pattern in find_matter_patterns(chains)
        ],
        "shared_obligation_conflicts": sorted(
            f"{group['kind']} — {group['duty']}"
            for group in shared_obligations(chains)
            if group["status"] == "conflict"
        ),
    }


def matter_delta_lines(previous: dict, current: dict) -> list[str]:
    """What changed between the previous export's digest and this
    one's: chains added, chains advanced, decisions recorded,
    walk-away lines opened or closed, obligations opened or
    closed, exhibit failures appearing or clearing, matter-level
    patterns forming or clearing, shared-obligation conflicts
    appearing or resolving. An empty diff says so. Digests written
    before a fact existed read as empty for that fact — a new
    reading never invents a change against an old export that
    could not record it."""
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
    prev_patterns = set(previous.get("matter_patterns", []))
    cur_patterns = set(current.get("matter_patterns", []))
    for clause_type in sorted(cur_patterns - prev_patterns):
        lines.append(
            f"Matter-level pattern since the last brief: {clause_type} — "
            "agreed edits for this clause type are now flagged "
            "not-reflected in two or more of the matter's chains."
        )
    for clause_type in sorted(prev_patterns - cur_patterns):
        lines.append(
            f"Matter-level pattern cleared since the last brief: "
            f"{clause_type}."
        )
    prev_conflicts = set(previous.get("shared_obligation_conflicts", []))
    cur_conflicts = set(current.get("shared_obligation_conflicts", []))
    for conflict in sorted(cur_conflicts - prev_conflicts):
        lines.append(
            f"Shared-obligation conflict in the matter: {conflict} — "
            "the matter's chains state different terms for the same duty."
        )
    for conflict in sorted(prev_conflicts - cur_conflicts):
        lines.append(
            f"Shared-obligation conflict resolved in the matter: {conflict}."
        )
    if not lines:
        lines.append(
            "Nothing material changed since the last matter brief — "
            "the facts below are the same facts, re-exported."
        )
    return lines


# ---------------------------------------------------------------------------
# The brief as data: one assembly, two renderings (markdown + JSON)
# ---------------------------------------------------------------------------


def _rollup(chains: list[list[ApprovalRecord]], latest: list[AgentResult]) -> dict:
    """The matter counted in code: clauses by playbook position
    and open lines by proposed tier, summed over the chains'
    current rounds, and the chains counted by document posture."""
    positions: dict[str, int] = {}
    tiers: dict[str, int] = {}
    postures: dict[str, int] = {}
    for result in latest:
        if result.facts:
            for position, count in result.facts.positions_count.items():
                positions[position] = positions.get(position, 0) + count
            postures[result.facts.posture] = postures.get(result.facts.posture, 0) + 1
        for diff in result.clause_diffs:
            key = str(diff.proposed_tier)
            tiers[key] = tiers.get(key, 0) + 1
    return {
        "chains": len(chains),
        "contracts": sum(len(chain) for chain in chains),
        "open_walk_aways": sum(_open_walk_aways(result) for result in latest),
        "positions": positions,
        "tiers": tiers,
        "chains_by_posture": postures,
    }


def build_matter_brief_data(
    counterparty: str,
    matter: str,
    chains: list[list[ApprovalRecord]],
    delta_lines: list[str] | None = None,
    first_export: bool = False,
) -> dict:
    """Assemble the matter brief as structured data across
    ``chains`` (each oldest → newest). Cross-contract views read
    each chain's LATEST round — the current state of that
    negotiation — while lineage, patterns and the chain table keep
    the whole history visible. Every entry traces to its chain
    (root + current round), its document and, where the view is
    clause-level, its clause. The markdown rendering
    (``render_matter_brief_markdown``) and the JSON export are two
    renderings of this one assembly, so they cannot drift."""
    latest = [chain[-1].result for chain in chains]

    chain_rows: list[dict] = []
    for chain in chains:
        results = _results(chain)
        result = results[-1]
        chain_rows.append(
            {
                "root": results[0].contract_id,
                "rounds": [r.contract_id for r in results],
                "rounds_count": len(results),
                "latest": result.contract_id,
                "status": result.status,
                "decision": result.decision or "",
                "worst_position": result.facts.worst_position if result.facts else "",
                "posture": result.facts.posture if result.facts else "",
                "open_walk_aways": _open_walk_aways(result),
            }
        )

    lineage_cache: dict[tuple[str, str], dict | None] = {}

    def lineage_for(chain: list[ApprovalRecord], clause_type: str) -> dict | None:
        results = _results(chain)
        key = (results[0].contract_id, clause_type)
        if key not in lineage_cache:
            lineage_cache[key] = _clause_lineage(results, clause_type)
        return lineage_cache[key]

    chain_by_latest = {chain[-1].result.contract_id: chain for chain in chains}
    open_positions: list[dict] = []
    flagged = [
        (result, diff)
        for result in latest
        for diff in result.clause_diffs
    ]
    for tier in (2, 1, 0):
        for result, diff in flagged:
            if diff.proposed_tier != tier:
                continue
            chain = chain_by_latest[result.contract_id]
            open_positions.append(
                {
                    "chain_root": chain[0].result.contract_id,
                    "contract_id": result.contract_id,
                    "clause_id": diff.clause_id,
                    "ref": diff.ref,
                    "heading": diff.heading,
                    "clause_type": diff.clause_type,
                    "position": diff.position,
                    "rationale": diff.rationale,
                    "tier": diff.proposed_tier,
                    "tier_name": diff.tier_name,
                    "pattern_escalated": diff.pattern_escalated,
                    "proposed_language": diff.tier_language or diff.playbook_fallback,
                    "lineage": lineage_for(chain, diff.clause_type),
                }
            )

    patterns: list[dict] = []
    for chain in chains:
        results = _results(chain)
        for pattern in results[-1].reflection_patterns:
            patterns.append(
                {
                    "chain_root": results[0].contract_id,
                    "contract_id": results[-1].contract_id,
                    "clause_type": pattern.clause_type,
                    "flagged_in": list(pattern.flagged_in),
                    "flags": pattern.flags,
                }
            )

    exhibit_findings: list[dict] = []
    for chain in chains:
        results = _results(chain)
        result = results[-1]
        for check in result.exhibit_checks:
            exhibit_findings.append(
                {
                    "chain_root": results[0].contract_id,
                    "contract_id": result.contract_id,
                    "name": check.name,
                    "promised": check.promised,
                    "found": check.found,
                    "status": check.status,
                    "detail": check.detail,
                }
            )

    obligation_rows: list[dict] = []
    for chain in chains:
        results = _results(chain)
        result = results[-1]
        for obligation in result.obligations:
            when = (
                obligation.date
                or (f"{obligation.days} days" if obligation.days is not None else "")
                or (f"{obligation.hours} hours" if obligation.hours is not None else "—")
            )
            if obligation.date and obligation.computed:
                when += " (computed)"
            obligation_rows.append(
                {
                    "chain_root": results[0].contract_id,
                    "contract_id": result.contract_id,
                    "kind": obligation.kind,
                    "description": obligation.description,
                    "detail": obligation.detail,
                    "date": obligation.date,
                    "days": obligation.days,
                    "hours": obligation.hours,
                    "computed": obligation.computed,
                    "source_clause_id": obligation.source_clause_id,
                    "when": when,
                    "_dated": bool(obligation.date),
                }
            )
    dated = sorted(
        (row for row in obligation_rows if row["_dated"]),
        key=lambda row: (row["date"], row["contract_id"]),
    )
    undated = [row for row in obligation_rows if not row["_dated"]]
    obligations = [
        {key: value for key, value in row.items() if not key.startswith("_")}
        for row in dated + undated
    ]

    return {
        "kind": "matter_brief",
        "counterparty": counterparty,
        "matter": matter,
        "first_export": first_export,
        "delta_lines": list(delta_lines or []),
        "rollup": _rollup(chains, latest),
        "chains": chain_rows,
        "open_positions": open_positions,
        "acceptance": acceptance_report(
            [result for chain in chains for result in _results(chain)]
        ),
        "patterns": patterns,
        "matter_patterns": find_matter_patterns(chains),
        "exhibit_findings": exhibit_findings,
        "obligations": obligations,
        "shared_obligations": shared_obligations(chains),
    }


def _lineage_line(lineage: dict) -> str:
    """One open line's lineage as a brief sentence: where the
    clause entered the chain, when the walk-away line was first
    proposed for it, how long it has sat there, and whether its
    position moved on the way."""
    moved = (
        f"position moved {lineage['from_position']} → {lineage['to_position']}"
        if lineage["moved"]
        else f"position unmoved at {lineage['to_position']}"
    )
    rounds = lineage["rounds_at_walk_away"]
    return (
        f"Lineage: clause first seen in {lineage['first_seen_in']} "
        f"at {lineage['from_position']}; walk-away line first "
        f"proposed in {lineage['walk_away_since']}; open at the "
        f"line for {rounds} round{'s' if rounds != 1 else ''} of "
        f"the chain; {moved}."
    )


def render_matter_brief_markdown(data: dict) -> str:
    """The matter brief as a markdown document, rendered from the
    structured assembly (``build_matter_brief_data``) — the JSON
    export carries the same facts in the same order."""
    counterparty, matter = data["counterparty"], data["matter"]
    rollup = data["rollup"]
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
        f"- Chains: {rollup['chains']} · contracts on record: "
        f"{rollup['contracts']}"
    )
    lines.append(
        f"- Open walk-away lines across the matter: {rollup['open_walk_aways']}"
    )
    lines.append("")

    if data["first_export"] or data["delta_lines"]:
        lines.append("## Since the last matter brief")
        lines.append("")
        if data["first_export"]:
            lines.append(
                "This is the first brief exported for this matter — "
                "there is no earlier export to compare against. "
                "Every later export leads with what changed since "
                "the one before it."
            )
        else:
            for line in data["delta_lines"]:
                lines.append(f"- {line}")
        lines.append("")

    # -- The roll-up -------------------------------------------------------
    lines.append("## Matter roll-up")
    lines.append("")
    positions = rollup["positions"]
    lines.append(
        "- Clauses by position across the matter's current rounds: "
        + " · ".join(
            f"{position} {positions.get(position, 0)}" for position in POSITIONS
        )
    )
    tiers = rollup["tiers"]
    lines.append(
        "- Open positions by proposed tier: "
        f"tier 2 (walk-away lines) {tiers.get('2', 0)} · "
        f"tier 1 (first fallbacks) {tiers.get('1', 0)} · "
        f"no tier proposed {tiers.get('0', 0)}"
    )
    postures = rollup["chains_by_posture"]
    lines.append(
        "- Chains by posture: "
        + " · ".join(
            f"{posture} {postures.get(posture, 0)}"
            for posture in ("aligned", "negotiable", "escalation_required")
        )
    )
    lines.append("")

    # -- The chains ------------------------------------------------------
    lines.append("## Chains in this matter")
    lines.append("")
    lines.append(
        "| Chain | Rounds | Latest | Status | Decision | Worst position | Posture | Open walk-away lines |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for row in data["chains"]:
        lines.append(
            f"| {' → '.join(row['rounds'])} "
            f"| {row['rounds_count']} | {row['latest']} | {row['status']} "
            f"| {row['decision'] or '—'} "
            f"| {row['worst_position'] or '—'} "
            f"| {row['posture'] or '—'} "
            f"| {row['open_walk_aways']} |"
        )
    lines.append("")

    # -- Combined open positions ------------------------------------------
    lines.append("## Open positions across the matter")
    lines.append("")
    if not data["open_positions"]:
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
            group = [p for p in data["open_positions"] if p["tier"] == tier]
            if not group:
                continue
            lines.append(f"### {heading}")
            lines.append("")
            for entry in group:
                ref = f" §{entry['ref']}" if entry["ref"] else ""
                lines.append(
                    f"- **{entry['contract_id']} · {entry['clause_id']}{ref} — "
                    f"{entry['clause_type']}** ({entry['position']}): "
                    f"{entry['rationale']}"
                )
                if entry["proposed_language"]:
                    lines.append(
                        f"  - Proposed language: {entry['proposed_language']}"
                    )
                if tier == 2 and entry["lineage"]:
                    lines.append(f"  - {_lineage_line(entry['lineage'])}")
            lines.append("")

    # -- Walk-away line acceptance across the matter -----------------------
    acceptance = data["acceptance"]
    lines.append("## Walk-away line acceptance across the matter")
    lines.append("")
    if not acceptance["approved_lines"]:
        lines.append(
            "No walk-away lines have been approved anywhere in "
            "this matter yet — acceptance is tracked from the "
            "moment counsel approves a packet that proposed one, "
            "and read against the follow-up round's draft."
        )
    else:
        total = acceptance["approved_lines"]
        headline = (
            f"Of the {total} walk-away line{'s' if total != 1 else ''} "
            f"counsel approved across this matter, {acceptance['held']} "
            f"held, {acceptance['softened']} softened, "
            f"{acceptance['dropped']} dropped"
        )
        extras = []
        if acceptance["clause_removed"]:
            extras.append(
                f"{acceptance['clause_removed']} clause(s) left the "
                "draft entirely — neither held nor tested"
            )
        if acceptance["still_open"]:
            extras.append(
                f"{acceptance['still_open']} still open — no "
                "follow-up round has answered them yet"
            )
        if extras:
            headline += "; " + "; ".join(extras)
        lines.append(headline + ".")
        lines.append("")
        status_words = {
            "held": "held",
            "softened": "softened",
            "dropped": "dropped",
            "clause_removed": "clause removed",
            "pending": "still open",
        }
        lines.append(
            "| Decided in | Clause | Position at decision | Follow-up round | Final position | Outcome |"
        )
        lines.append("|---|---|---|---|---|---|")
        for entry in acceptance["lines"]:
            lines.append(
                f"| {entry['decided_in']} "
                f"| {entry['clause_type']} ({entry['clause_id']}) "
                f"| {entry['position_at_decision'] or '—'} "
                f"| {entry['resolved_in'] or '—'} "
                f"| {entry['final_position'] or '—'} "
                f"| {status_words.get(entry['status'], entry['status'])} |"
            )
    lines.append("")

    # -- Reflection patterns across the matter -----------------------------
    if data["patterns"]:
        lines.append("## Reflection patterns across the matter")
        lines.append("")
        lines.append(
            "Clause types in this matter whose agreed edits were "
            "flagged agreed-but-not-reflected in two consecutive "
            "follow-ups (see each chain's brief for the round "
            "history) — their packets propose the walk-away line."
        )
        lines.append("")
        for pattern in data["patterns"]:
            lines.append(
                f"- **{pattern['clause_type']}** on {pattern['contract_id']} — "
                f"flagged in {' → '.join(pattern['flagged_in'])} "
                f"({pattern['flags']} missed agreement(s) on record)."
            )
        lines.append("")

    # -- Patterns visible only across the matter ---------------------------
    if data["matter_patterns"]:
        lines.append("## Patterns visible only across the matter")
        lines.append("")
        lines.append(
            "Clause types whose agreed edits were flagged "
            "agreed-but-not-reflected, with the miss still open, in "
            "two or more of this matter's chains. No single chain "
            "has to show the pattern for the matter to have one: "
            "the same ask is being missed on more than one document "
            "of the matter, and that only shows when the chains are "
            "read together."
        )
        lines.append("")
        for pattern in data["matter_patterns"]:
            chain_bits = []
            for member in pattern["chains"]:
                bit = (
                    f"{member['latest']} (chain root {member['root']}): "
                    f"{member['flags']} missed agreement(s), flagged in "
                    f"{' → '.join(member['flagged_in'])}"
                )
                if member["active_pattern"]:
                    bit += "; the chain's own pattern is active"
                chain_bits.append(bit)
            lines.append(
                f"- **{pattern['clause_type']}** — missed in "
                f"{len(pattern['chains'])} chains: "
                + "; ".join(chain_bits)
                + f". {pattern['total_flags']} missed agreement(s) "
                "across the matter."
            )
        lines.append("")

    # -- Exhibit findings across the matter --------------------------------
    lines.append("## Exhibit findings across the matter")
    lines.append("")
    if not data["exhibit_findings"]:
        lines.append(
            "No cross-document checks were generated anywhere in "
            "this matter — no agreement in it ties anything checkable "
            "to an exhibit."
        )
    else:
        lines.append("| Contract | Check | Promised | Exhibit says | Status | Detail |")
        lines.append("|---|---|---|---|---|---|")
        for check in data["exhibit_findings"]:
            lines.append(
                f"| {check['contract_id']} | {check['name']} "
                f"| {check['promised'] or '—'} | {check['found'] or '—'} "
                f"| {check['status']} | {check['detail']} |"
            )
    lines.append("")

    # -- Obligations across the matter, by deadline -------------------------
    lines.append("## Obligations across the matter, by deadline")
    lines.append("")
    if not data["obligations"]:
        lines.append(
            "No dated or recurring obligations were extracted from "
            "any chain's current round."
        )
    else:
        lines.append("| Contract | Kind | Obligation | Detail | When | Source |")
        lines.append("|---|---|---|---|---|---|")
        for obligation in data["obligations"]:
            lines.append(
                f"| {obligation['contract_id']} | {obligation['kind']} "
                f"| {obligation['description']} | {obligation['detail'] or '—'} "
                f"| {obligation['when']} | {obligation['source_clause_id']} |"
            )
    lines.append("")

    # -- Shared obligations: the same duty, read across chains --------------
    if data["shared_obligations"]:
        lines.append("## Shared obligations across the matter")
        lines.append("")
        lines.append(
            "Duties the register records in more than one chain of "
            "this matter — grouped by kind and operative text with "
            "the figures set aside, then read side by side with "
            "each chain's stated terms. A group is marked a "
            "conflict when the chains state different figures for "
            "the same duty; the comparison is over the register's "
            "figures, computed in code."
        )
        lines.append("")
        for group in data["shared_obligations"]:
            marker = "CONFLICT" if group["status"] == "conflict" else "consistent"
            lines.append(f"### {group['kind']} — {group['duty']} — {marker}")
            lines.append("")
            lines.append("| Chain (current round) | Detail | Days | When | Source |")
            lines.append("|---|---|---|---|---|")
            for member in group["members"]:
                when = member["date"] or "—"
                if member["date"] and member["computed"]:
                    when += " (computed)"
                days = member["days"] if member["days"] is not None else "—"
                lines.append(
                    f"| {member['contract_id']} | {member['detail'] or '—'} "
                    f"| {days} | {when} | {member['source_clause_id']} |"
                )
            lines.append("")
            if group["status"] == "conflict":
                stated = "; ".join(
                    f"{member['contract_id']} states "
                    f"{member['detail'] or 'no operative text'}"
                    for member in group["members"]
                )
                lines.append(
                    f"The chains do not state the same terms for "
                    f"this duty: {stated}."
                )
            else:
                lines.append(
                    "Every chain in the group states the same terms "
                    "for this duty."
                )
            lines.append("")

    lines.append(
        "---\nGenerated by the Contract Review blueprint from stored "
        "review records. Draft for counsel review — not a legal "
        "conclusion, not an approval, not a redline."
    )
    return "\n".join(lines) + "\n"


def build_matter_brief(
    counterparty: str,
    matter: str,
    chains: list[list[ApprovalRecord]],
    delta_lines: list[str] | None = None,
    first_export: bool = False,
) -> str:
    """Assemble the matter brief markdown across ``chains`` (each
    oldest → newest) — the markdown rendering of
    ``build_matter_brief_data``; the two renderings share one
    assembly so they cannot tell the story two ways."""
    return render_matter_brief_markdown(
        build_matter_brief_data(
            counterparty,
            matter,
            chains,
            delta_lines=delta_lines,
            first_export=first_export,
        )
    )
