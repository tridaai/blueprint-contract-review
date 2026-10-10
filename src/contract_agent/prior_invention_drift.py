"""Prior-invention schedule drift: the schedule is a living
list too, and one direction of movement is dangerous.

The single-round checks (``exhibit_review.py``, round 11) read
one round's schedule against that round's assignment clause.
But the schedule decides what the employee keeps only as it
stands at signing, and schedules are edited between rounds like
any other annex. This module reads two consecutive rounds of an
employment chain together, the way ``subprocessor_drift.py``
reads the DPA annex:

- the schedules' invention sets are diffed — added, removed;
- a REMOVED invention is read hardest. The assignment the
  schedule serves excludes what the schedule lists; an
  invention that leaves the schedule between rounds is no
  longer listed, so — where the assignment ties its exclusion
  to the schedule — it falls into the assignment: the employee
  assigns away, by an edit to an annex, the very thing the
  schedule existed to keep theirs. That is a finding
  (``mismatch``), not a change.
- an ADDED invention only widens the record of what predates
  the employment; it is reported as movement, ``consistent``,
  with the names spelled out — counsel sees the list move
  either way.

The check is named ``prior_invention_schedule_drift`` and rides
on the result beside the single-round checks
(``AgentResult.prior_invention_drift``), attached by the service
after the run, like the sub-processor drift. A prior round with
no schedule listing anything has no baseline to drift from —
the single-round checks own that absence — but a schedule that
EXISTED and is gone in the current round is the loudest removal
of all, and is reported as one.
"""

from __future__ import annotations

import re

from .exhibit_review import (
    _PRIOR_INVENTION_DOC,
    _assignment_relies_on_schedule,
    _find_schedule_document,
    _item_name,
    _list_items,
    _normalise_name,
)
from .schemas import ContractInput, ExhibitCheck


def prior_invention_entries(contract: ContractInput) -> list[dict]:
    """The schedule's inventions as comparable entries: display
    name and the normalised key the diff matches on. Empty when
    the package carries no schedule of prior inventions, or the
    schedule lists none."""
    doc = _find_schedule_document(contract, _PRIOR_INVENTION_DOC)
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
        entries.append({"name": name, "key": key})
    return entries


def _assignment_text(contract: ContractInput) -> str:
    """The agreement's assignment sentences — the read the drift
    makes of whether the current assignment leans on the
    schedule, mirroring the fallback the sub-processor drift
    uses for its clause text."""
    sentences = [
        s
        for s in re.split(r"(?<=[.!?])\s+|\n", contract.agreement_text())
        if "assign" in s.lower()
    ]
    return " ".join(sentences)


def build_prior_invention_drift(
    prior: ContractInput, current: ContractInput
) -> list[ExhibitCheck]:
    """The drift check between two consecutive rounds of a chain,
    or an empty list when the prior round's schedule listed
    nothing (no baseline to drift from)."""
    prior_entries = prior_invention_entries(prior)
    if not prior_entries:
        return []
    current_entries = prior_invention_entries(current)
    doc = _find_schedule_document(current, _PRIOR_INVENTION_DOC)
    doc_id = (doc.document_id or doc.doc_type) if doc else ""
    prior_by_key = {e["key"]: e for e in prior_entries}
    current_by_key = {e["key"]: e for e in current_entries}
    added = [e for k, e in current_by_key.items() if k not in prior_by_key]
    removed = [e for k, e in prior_by_key.items() if k not in current_by_key]
    expectation = (
        "Across a negotiation chain the schedule of prior "
        "inventions may grow — the employee records more of what "
        "predates the employment — but an invention that leaves "
        "the schedule falls out of the exclusion the schedule "
        "carries: where the assignment excludes what the "
        "schedule lists, a removal assigns the removed invention "
        "away. Movement is reported either way; a removal is a "
        "finding."
    )
    if not added and not removed:
        return [ExhibitCheck(
            name="prior_invention_schedule_drift",
            expectation=expectation,
            exhibit_document_id=doc_id,
            promised=f"schedule as at {prior.contract_id}",
            found="schedule unchanged",
            status="consistent",
            detail=(
                f"The schedule of prior inventions is unchanged "
                f"since {prior.contract_id} — the same "
                f"{len(current_entries)} invention(s) recorded."
            ),
        )]

    changes: list[str] = []
    if added:
        changes.append("added: " + ", ".join(e["name"] for e in added))
    if removed:
        changes.append("removed: " + ", ".join(e["name"] for e in removed))
    movement = "; ".join(changes)

    relies = _assignment_relies_on_schedule(_assignment_text(current))
    if removed and relies:
        status = "mismatch"
        detail = (
            f"Between {prior.contract_id} and "
            f"{current.contract_id} the schedule moved — "
            f"{movement}. The assignment excludes the inventions "
            "the schedule lists, so what left the schedule — "
            f"{', '.join(e['name'] for e in removed)} — is no "
            "longer excluded: as the chain now stands, the "
            "employee assigns it away. A removal from this "
            "schedule is a transfer, and it should be a "
            "deliberate one, named in the negotiation — not an "
            "edit to an annex."
        )
    elif removed:
        status = "consistent"
        detail = (
            f"Between {prior.contract_id} and "
            f"{current.contract_id} the schedule moved — "
            f"{movement}. The current assignment does not tie "
            "its exclusion to the schedule, so the removal "
            "changes no exclusion the assignment grants; the "
            "movement is reported for the record."
        )
    else:
        status = "consistent"
        detail = (
            f"Between {prior.contract_id} and "
            f"{current.contract_id} the schedule grew — "
            f"{movement}. Additions widen the record of what "
            "predates the employment; whether the enlarged "
            "schedule is protected is the exclusion check's "
            "question, answered on this round's own checks."
        )
    return [ExhibitCheck(
        name="prior_invention_schedule_drift",
        expectation=expectation,
        exhibit_document_id=doc_id,
        promised=f"schedule as at {prior.contract_id}",
        found=movement,
        status=status,
        detail=detail,
    )]
