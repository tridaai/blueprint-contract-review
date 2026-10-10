"""Sub-processor list drift: the annex is a living list, and a
chain's rounds are its history.

The single-round checks (``exhibit_review.py``, round 10) read one
round's annex against that round's own terms. But a sub-processor
list is the operative document in a DPA precisely because it
CHANGES — processors are engaged and dropped between drafts — and
a change is exactly what the clause's notice machinery exists to
govern. This module reads two consecutive rounds of a chain
together:

- the annexes' processor sets are diffed — added, removed, and
  moved (same processor, different stated location) — the way
  clause movement is diffed in ``round_diff.py``;
- an ADDED processor is checked against the notice clock: the
  clause promises the controller notice of an addition at least
  N days ahead, so an addition that first appears in a round
  received fewer than N days after the previous round cannot
  have had the promised notice run — that is a finding
  (``mismatch``), not just a change.

Changes that break nothing are still reported — counsel should
see the list move — with status ``consistent`` and the movement
spelled out in the detail: drift is information, the notice
violation is the contradiction. The check is named
``subprocessor_list_drift`` and rides on the result separately
from the single-round exhibit checks (``AgentResult.
subprocessor_drift``), the way round diffs ride separately: the
facts node has already read the single-round checks by the time
a chain comparison can be made, and this module does not rewrite
its arithmetic.

Both rounds must carry a list annex for a comparison to exist —
when either does not, there is no drift to report and the
single-round checks own the absence.
"""

from __future__ import annotations

import re

from .exhibit_review import (
    _find_subprocessor_list_document,
    _item_location,
    _item_name,
    _list_items,
    _normalise_name,
    _promised_notice,
)
from .schemas import ContractInput, ExhibitCheck


def subprocessor_list_entries(contract: ContractInput) -> list[dict]:
    """The annex's processors as comparable entries: display name,
    stated location, and the normalised key the diff matches on.
    Empty when the package carries no sub-processor list annex."""
    doc = _find_subprocessor_list_document(contract)
    if doc is None:
        return []
    entries: list[dict] = []
    seen: set[str] = set()
    for item in _list_items(doc.raw_text):
        name = _item_name(item)
        key = _normalise_name(name)
        if not key or key in seen:
            continue
        seen.add(key)
        entries.append(
            {"name": name, "location": _item_location(item), "key": key}
        )
    return entries


def _subprocessor_clause_text(contract: ContractInput) -> str:
    """The agreement's sub-processor sentences — the same fallback
    the single-round checks use when no standalone subprocessor
    clause was segmented."""
    sentences = [
        s
        for s in re.split(r"(?<=[.!?])\s+", contract.agreement_text())
        if "sub-processor" in s.lower() or "subprocessor" in s.lower()
    ]
    return " ".join(sentences)


def _received_date(contract: ContractInput):
    from .facts import _parse_date

    return _parse_date(contract.received_at)


def build_subprocessor_drift(
    prior: ContractInput, current: ContractInput
) -> list[ExhibitCheck]:
    """The drift check between two consecutive rounds of a chain,
    or an empty list when either round carries no list annex."""
    prior_entries = subprocessor_list_entries(prior)
    current_entries = subprocessor_list_entries(current)
    if not prior_entries or not current_entries:
        return []
    doc = _find_subprocessor_list_document(current)
    doc_id = (doc.document_id or doc.doc_type) if doc else ""
    prior_by_key = {e["key"]: e for e in prior_entries}
    current_by_key = {e["key"]: e for e in current_entries}
    added = [e for k, e in current_by_key.items() if k not in prior_by_key]
    removed = [e for k, e in prior_by_key.items() if k not in current_by_key]
    moved = [
        (prior_by_key[k], current_by_key[k])
        for k in prior_by_key.keys() & current_by_key.keys()
        if _normalise_name(prior_by_key[k]["location"])
        != _normalise_name(current_by_key[k]["location"])
    ]
    expectation = (
        "Across a negotiation chain the sub-processor list may "
        "change only the way the clause's notice machinery allows: "
        "an addition must have had the promised notice period run "
        "before it takes effect. Movement in the list between "
        "rounds is reported either way — counsel sees the list "
        "move; an addition inside the notice window is a finding."
    )
    if not added and not removed and not moved:
        return [ExhibitCheck(
            name="subprocessor_list_drift",
            expectation=expectation,
            exhibit_document_id=doc_id,
            promised=f"list as at {prior.contract_id}",
            found="list unchanged",
            status="consistent",
            detail=(
                f"The sub-processor list is unchanged since "
                f"{prior.contract_id} — the same "
                f"{len(current_entries)} processor(s), in the same "
                "places."
            ),
        )]

    changes: list[str] = []
    if added:
        changes.append(
            "added: " + ", ".join(
                f"{e['name']} ({e['location']})" if e["location"] else e["name"]
                for e in added
            )
        )
    if removed:
        changes.append(
            "removed: " + ", ".join(e["name"] for e in removed)
        )
    if moved:
        changes.append(
            "moved: " + ", ".join(
                f"{new['name']} ({old['location'] or 'no location stated'}"
                f" → {new['location'] or 'no location stated'})"
                for old, new in moved
            )
        )
    movement = "; ".join(changes)

    # The notice clock: only additions run against it. Removals and
    # moves take effect when the controller is told of them; an
    # addition is the engagement the notice promise exists to
    # precede.
    clock_broken = False
    clock_note = ""
    if added:
        _promised, promised_days = _promised_notice(
            _subprocessor_clause_text(current)
        )
        if promised_days:
            prior_date = _received_date(prior)
            current_date = _received_date(current)
            if prior_date is not None and current_date is not None:
                gap = (current_date - prior_date).days
                if gap < promised_days:
                    clock_broken = True
                    clock_note = (
                        f" The addition first appears in a round "
                        f"received {gap} day(s) after the previous "
                        f"round, inside the {promised_days} days' "
                        "notice the clause promises for additions — "
                        "the promised notice period cannot have run."
                    )
                else:
                    clock_note = (
                        " The additions stand outside the notice "
                        "window: the rounds are far enough apart "
                        "for the promised notice to have run."
                    )
            else:
                clock_note = (
                    " The notice clock could not be checked: one "
                    "of the rounds carries no received date."
                )
    status = "mismatch" if clock_broken else "consistent"
    detail = (
        f"Between {prior.contract_id} and {current.contract_id} "
        f"the list moved — {movement}.{clock_note}"
    )
    return [ExhibitCheck(
        name="subprocessor_list_drift",
        expectation=expectation,
        exhibit_document_id=doc_id,
        promised=f"list as at {prior.contract_id}",
        found=movement,
        status=status,
        detail=detail,
    )]
