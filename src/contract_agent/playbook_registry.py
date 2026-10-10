"""The active playbook: the shipped corpus, plus what counsel
activated.

The playbook corpus in ``playbook_data.py`` is human-owned and
ships with the blueprint. Coverage proposals
(``playbook_proposals.py``) draft additions for clause types the
corpus does not cover, and counsel may ACTIVate one — at which
point its entries must become real: verdicts may cite them, their
fallback language may be offered, their walk-away lines may be
proposed as tier 2. This registry is where that happens: the
service registers activated proposals' entries here on activation
(and on startup, from the proposal store), and the verdict rules,
tier rules, and cross-check prompts read the ACTIVE playbook
through here instead of the static corpus.

One deliberate limit, stated rather than implied:

- Registration is process-local. The proposal STORE is the durable
  record; a fresh process re-registers its activated proposals at
  service start, so a restart loses nothing.

Retrieval is NOT a limit (round 8): retrievers snapshot the ACTIVE
playbook — this registry's merged view — at construction, and the
service calls ``refresh()`` on its retriever the moment a proposal
is activated (and after the startup re-registration), so activated
entries are retrievable in the same minute they are activated —
an entry counsel activates at 9:00 answers queries at 9:01.
"""

from __future__ import annotations

from .playbook_data import PLAYBOOK, PLAYBOOK_BY_ID

_registered: dict[str, dict] = {}


def register_entries(entries: list[dict]) -> None:
    """Register activated proposal entries by id. Re-registering an
    id replaces it (an activation replayed after restart is a
    no-op, not a duplicate)."""
    for entry in entries:
        entry_id = str(entry.get("id") or "")
        if entry_id:
            _registered[entry_id] = dict(entry)


def unregister_entries(entry_ids: list[str]) -> None:
    """Drop registered entries by id — a proposal's retirement
    (round 10). Only registry entries are dropped: a shipped corpus
    id is never in the registry under its own key in a way this
    could shadow, and unknown ids are ignored, so a retirement
    replayed after a restart that never re-registered them is a
    no-op."""
    for entry_id in entry_ids:
        _registered.pop(entry_id, None)


def clear_registered() -> None:
    """Drop every registered entry (tests; never called at runtime)."""
    _registered.clear()


def registered_entries() -> list[dict]:
    return list(_registered.values())


def active_playbook() -> list[dict]:
    """The shipped corpus plus registered entries, in corpus order
    (registered entries appended; a registered id never shadows a
    shipped one)."""
    shipped_ids = set(PLAYBOOK_BY_ID)
    return list(PLAYBOOK) + [
        entry for entry_id, entry in _registered.items() if entry_id not in shipped_ids
    ]


def active_entry_by_id(entry_id: str) -> dict | None:
    if entry_id in _registered:
        return _registered[entry_id]
    return PLAYBOOK_BY_ID.get(entry_id)


def entries_for_type(clause_type: str) -> list[dict]:
    """Every active entry for a clause type: registered (proposal)
    entries first — they exist because the shipped family fell
    short — then the shipped family in corpus order."""
    registered = [
        entry for entry in _registered.values() if entry.get("clause_type") == clause_type
    ]
    shipped = [entry for entry in PLAYBOOK if entry["clause_type"] == clause_type]
    return registered + shipped
