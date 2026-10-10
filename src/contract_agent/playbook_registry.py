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

Two deliberate limits, stated rather than implied:

- Registration is process-local. The proposal STORE is the durable
  record; a fresh process re-registers its activated proposals at
  service start, so a restart loses nothing.
- Retrieval corpora are built when a retriever is constructed;
  activated entries join verdicts, tiers, and prompts immediately,
  and the retrieval corpus on the next retriever build.
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
