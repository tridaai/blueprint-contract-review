"""Activated-proposal retrieval rebuild (round 8): round 7 left one
honest limit — activated playbook entries joined verdicts, tiers,
and prompts immediately, but reached retrieval only on a retriever
rebuild. Now retrievers snapshot the ACTIVE playbook at
construction and the service refreshes its retriever the moment
counsel activates: an entry activated at 9:00 is retrievable at
9:01, proven here against the service's own retriever."""

from __future__ import annotations

import pytest

from contract_agent.playbook_proposals import draft_playbook_proposal
from contract_agent.playbook_registry import clear_registered, register_entries
from contract_agent.proposal_store import InMemoryProposalStore
from contract_agent.retriever import KeywordRetriever
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registered()
    yield
    clear_registered()


def _force_majeure_proposal():
    # 'force_majeure' is the standing coverage gap: no shipped entry
    # carries its tokens, so a query for it returns nothing until a
    # proposal for it is activated.
    return draft_playbook_proposal("force_majeure", flagged=3, without_fallback=3)


def _register(proposal) -> None:
    register_entries([entry.model_dump() for entry in proposal.entries])


# ---------------------------------------------------------------------------
# The refresh itself
# ---------------------------------------------------------------------------


def test_refresh_makes_registered_entries_retrievable():
    retriever = KeywordRetriever()
    assert retriever.retrieve("force majeure", top_k=3) == []
    _register(_force_majeure_proposal())
    # Registration alone does not rewrite a built corpus — the
    # snapshot is honest until refresh is called.
    assert retriever.retrieve("force majeure", top_k=3) == []
    assert retriever.refresh() is True
    hits = retriever.retrieve("force majeure", top_k=3)
    assert hits
    assert all(
        hit.playbook_id.startswith("PROP-FORCE-MAJEURE") for hit in hits
    )


def test_fresh_retriever_snapshots_the_active_playbook():
    _register(_force_majeure_proposal())
    hits = KeywordRetriever().retrieve("force majeure", top_k=3)
    assert any(
        hit.playbook_id.startswith("PROP-FORCE-MAJEURE") for hit in hits
    )


def test_pinned_corpus_never_refreshes():
    pinned = [{
        "id": "PB-X-01", "title": "Pinned entry",
        "text": "a deliberately pinned corpus", "clause_type": "other",
        "fallback_language": "",
    }]
    retriever = KeywordRetriever(playbooks=pinned)
    _register(_force_majeure_proposal())
    assert retriever.refresh() is False
    assert retriever.retrieve("force majeure", top_k=3) == []


# ---------------------------------------------------------------------------
# Through the service: activation refreshes the held retriever
# ---------------------------------------------------------------------------


def test_service_activation_refreshes_the_held_retriever():
    store = InMemoryProposalStore()
    proposal = _force_majeure_proposal()
    store.save(proposal)
    retriever = KeywordRetriever()
    svc = ContractService(
        store=InMemoryStore(), checkpointer=False,
        proposal_store=store, retriever=retriever,
    )
    assert retriever.retrieve("force majeure", top_k=3) == []
    svc.activate_playbook_proposal(proposal.proposal_id, actor="C. Ounsel")
    hits = retriever.retrieve("force majeure", top_k=3)
    assert any(
        hit.playbook_id == "PROP-FORCE-MAJEURE-01" for hit in hits
    ), [hit.playbook_id for hit in hits]
