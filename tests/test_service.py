"""Service tests: decisions, the guardrail gate on approval, batch,
memory/feedback, and document archiving — all with test doubles."""

from __future__ import annotations

import pytest

from contract_agent.model_backends import MockModelBackend
from contract_agent.objectstore import InMemoryObjectStore
from contract_agent.retriever import KeywordRetriever
from contract_agent.schemas import ContractInput, DocumentInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore
from helpers import contract


def service() -> ContractService:
    return ContractService(
        backend=MockModelBackend(),
        retriever=KeywordRetriever(),
        store=InMemoryStore(),
        checkpointer=False,
        object_store=InMemoryObjectStore(),
    )


def test_analyze_then_approve():
    svc = service()
    result = svc.analyze(contract())
    assert result.status == "awaiting_counsel"
    decided = svc.approve("CTR-1001", approver="counsel@example.com", reason="Fallback wording accepted.")
    assert decided.status == "approved"
    assert decided.decided_by == "counsel@example.com"
    with pytest.raises(ValueError):
        svc.approve("CTR-1001", approver="counsel@example.com")


def test_reject_records_reason():
    svc = service()
    svc.analyze(contract())
    decided = svc.reject("CTR-1001", reviewer="counsel@example.com", reason="Walk-away clause stands.")
    assert decided.status == "rejected"
    assert decided.decision_reason == "Walk-away clause stands."


def test_unknown_contract_raises():
    svc = service()
    with pytest.raises(KeyError):
        svc.approve("CTR-XXXX", approver="counsel@example.com")


def test_batch_isolates_failures():
    svc = service()
    items = svc.analyze_batch([contract(), {"bogus": True}, contract(contract_id="CTR-1002")])
    assert items[0].result is not None
    assert items[1].error is not None
    assert items[2].result is not None


def test_documents_archived_to_object_store():
    svc = service()
    svc.analyze(contract())
    objects = svc._get_object_store()
    assert objects.exists("contracts/CTR-1001/AGR-1.txt")
    assert objects.exists("contracts/CTR-1001/review-packet.txt")


def test_feedback_loop_teaches_later_runs():
    svc = service()
    svc.analyze(contract())
    svc.approve("CTR-1001", approver="counsel@example.com", reason="Standard terms; cleared as drafted.")
    second = svc.analyze(contract(contract_id="CTR-1002"))
    evidence = " ".join(second.assessment.evidence)
    assert "counsel feedback" in evidence
    assert "Standard terms; cleared as drafted." in evidence
