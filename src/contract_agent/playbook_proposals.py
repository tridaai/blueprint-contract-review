"""Coverage-driven playbook proposals: the agent drafts the
paperwork, counsel owns the playbook.

``playbook_coverage`` (in the metrics) has long named an awkward
fact: some clause types keep arriving FLAGGED with no fallback
language to offer — a gap in the playbook, not in the contracts.
The types with no matrix row at all (everything the extractor can
only call ``other``) live there permanently, and any type a
deployment's playbook trims joins them. This module turns the
coverage report into a work queue: for each gap type it drafts a
candidate playbook family — a position entry, a fallback entry,
and (where the donor family carries one) a walk-away line — shaped
from the NEAREST COVERED clause type, stored as a PROPOSED
``PlaybookProposal``.

"Nearest" is a stated rule, not a model's taste: the covered type
sharing the most name tokens with the gap type (``data_transfer``
sits next to ``data_protection``), ties broken by family size, then
alphabetically. Where the donor is genuinely related (a shared
token), its fallback language and walk-away line are carried into
the draft AS the starting wording — labelled as adapted, in the
open, for counsel to confirm or rewrite. Where no covered family
shares a token, the draft says so and carries structure only:
inventing legal language for an unrelated clause type would be
worse than admitting the scaffold is empty.

The proposal changes nothing by itself. It is never active until
counsel activates it in the console (``ContractService`` stamps the
actor and registers the entries in ``playbook_registry``), and a
dismissed proposal is never re-drafted — counsel's "no" is part of
the record. The playbook stays human-owned; this just stops the
gaps from being invisible.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .playbook_registry import active_playbook
from .schemas import PlaybookProposal, PlaybookProposalEntry

# A type is proposed on when it has been flagged without a fallback
# at least this many times across the stored runs — "keeps
# arriving", not "arrived once".
MIN_GAP_FLAGS = 2


def _tokens(clause_type: str) -> set[str]:
    return {token for token in clause_type.split("_") if token}


def _title(clause_type: str) -> str:
    return clause_type.replace("_", " ").title()


def covered_clause_types(entries: list[dict] | None = None) -> set[str]:
    """Clause types the active playbook covers with fallback
    language — the donor pool, and the definition of "covered" the
    coverage report's gap list is measured against."""
    entries = entries if entries is not None else active_playbook()
    return {e["clause_type"] for e in entries if e.get("fallback_language")}


def nearest_covered_type(
    clause_type: str, entries: list[dict] | None = None
) -> tuple[str, str]:
    """The covered clause type a gap type's draft is shaped from,
    and the basis for the choice (recorded on the proposal).

    Rule: most shared name tokens wins; ties break to the family
    with more entries (the fuller shape), then alphabetically —
    deterministic, so the same gap always drafts from the same
    donor."""
    entries = entries if entries is not None else active_playbook()
    by_type: dict[str, list[dict]] = {}
    for entry in entries:
        if entry.get("fallback_language"):
            by_type.setdefault(entry["clause_type"], []).append(entry)
    candidates = [t for t in by_type if t != clause_type]
    if not candidates:
        return "", "no covered clause type exists to shape a draft from"
    mine = _tokens(clause_type)

    def rank(candidate: str) -> tuple[int, int, str]:
        shared = len(mine & _tokens(candidate))
        return (-shared, -len(by_type[candidate]), candidate)

    donor = min(candidates, key=rank)
    shared = sorted(mine & _tokens(donor))
    if shared:
        basis = (
            f"nearest covered family by name: '{donor}' shares the "
            f"token(s) {', '.join(shared)} with '{clause_type}'"
        )
    else:
        basis = (
            f"no covered family shares a name token with '{clause_type}' — "
            f"the most complete covered family ('{donor}', "
            f"{len(by_type[donor])} entries) donates its shape only; "
            "counsel drafts the substance"
        )
    return donor, basis


def draft_playbook_proposal(
    clause_type: str,
    *,
    flagged: int,
    without_fallback: int,
    example_contract_ids: list[str] | None = None,
    entries: list[dict] | None = None,
    created_at: str = "",
) -> PlaybookProposal:
    """Draft the candidate family for one gap clause type.

    The entries are scaffolds with provenance: entry text states
    what was adapted from the donor and what counsel must still
    write. Donor fallback / walk-away language is carried only when
    the donor is genuinely related (a shared name token); otherwise
    the fallback entry is an explicit drafting task with no
    language, rather than another type's words wearing this type's
    name."""
    entries = entries if entries is not None else active_playbook()
    donor, basis = nearest_covered_type(clause_type, entries)
    donor_entries = [e for e in entries if e["clause_type"] == donor]
    preferred_donor = donor_entries[0] if donor_entries else {}
    fallback_donor = next(
        (e for e in donor_entries if e.get("fallback_language")), {}
    )
    walk_donor = next((e for e in donor_entries if e.get("walk_away_line")), {})
    related = bool(_tokens(clause_type) & _tokens(donor)) if donor else False
    stem = f"PROP-{clause_type.upper().replace('_', '-')}"
    title = _title(clause_type)

    drafted: list[PlaybookProposalEntry] = [
        PlaybookProposalEntry(
            id=f"{stem}-01",
            title=f"{title} — preferred position (PROPOSED)",
            clause_type=clause_type,
            text=(
                f"PROPOSED — drafted from the playbook coverage report, "
                f"not yet counsel's position. '{clause_type}' has arrived "
                f"flagged {flagged} time(s) across the stored reviews, "
                f"{without_fallback} of them with no fallback language "
                f"the playbook could offer. This candidate takes its "
                f"shape from the {donor or '—'} family"
                + (
                    f" ({preferred_donor.get('id', '')} — "
                    f"“{preferred_donor.get('title', '')}”)"
                    if preferred_donor
                    else ""
                )
                + f": counsel states the preferred position for "
                f"{clause_type} here, in their own words, before "
                "activation."
            ),
        )
    ]
    if related and fallback_donor:
        drafted.append(
            PlaybookProposalEntry(
                id=f"{stem}-02",
                title=f"{title} — fallback language (PROPOSED)",
                clause_type=clause_type,
                text=(
                    f"PROPOSED fallback, adapted from "
                    f"{fallback_donor.get('id', '')}'s language for "
                    f"{donor}: counsel confirms the wording fits "
                    f"{clause_type} before activation. The language "
                    "below is the donor's, carried over as the starting "
                    "draft — not a position this playbook has yet taken "
                    f"on {clause_type}."
                ),
                fallback_language=fallback_donor.get("fallback_language", ""),
            )
        )
    else:
        drafted.append(
            PlaybookProposalEntry(
                id=f"{stem}-02",
                title=f"{title} — fallback language (PROPOSED)",
                clause_type=clause_type,
                text=(
                    f"PROPOSED — no covered family is close enough to "
                    f"'{clause_type}' to adapt fallback language from "
                    f"honestly (nearest by shape: {donor or '—'}). "
                    f"Counsel drafts the fallback wording for "
                    f"{clause_type} here. Until this entry carries "
                    "language, activating the proposal adds the "
                    "position entries only, and flagged "
                    f"{clause_type} clauses still reach counsel with "
                    "no proposed language — stated, not hidden."
                ),
            )
        )
    if related and walk_donor:
        drafted.append(
            PlaybookProposalEntry(
                id=f"{stem}-03",
                title=f"{title} — walk-away line (PROPOSED)",
                clause_type=clause_type,
                text=(
                    f"PROPOSED walk-away line, adapted from "
                    f"{walk_donor.get('id', '')}'s final position for "
                    f"{donor}: counsel confirms where {clause_type}'s "
                    "line actually sits before activation."
                ),
                walk_away_line=walk_donor.get("walk_away_line", ""),
            )
        )
    return PlaybookProposal(
        proposal_id=f"PROP-{clause_type.upper().replace('_', '-')}",
        clause_type=clause_type,
        status="proposed",
        based_on_type=donor,
        based_on_entry_ids=[e.get("id", "") for e in donor_entries],
        basis=basis,
        entries=drafted,
        flagged=flagged,
        without_fallback=without_fallback,
        example_contract_ids=list(example_contract_ids or []),
        created_at=created_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
