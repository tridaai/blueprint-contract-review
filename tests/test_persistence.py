"""Persistence tests: the checkpointed counsel gate (in-memory saver
as the test double for the production PostgresSaver)."""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver

from contract_agent.model_backends import MockModelBackend
from contract_agent.retriever import KeywordRetriever
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore
from helpers import contract


def test_gate_pauses_and_decision_resumes_thread():
    saver = MemorySaver()
    svc = ContractService(
        backend=MockModelBackend(),
        retriever=KeywordRetriever(),
        store=InMemoryStore(),
        checkpointer=saver,
    )
    result = svc.analyze(contract())
    assert result.status == "awaiting_counsel"
    # The thread is parked at the gate.
    from contract_agent.graph import _build_graph
    from contract_agent.model_backends import MockModelBackend as Mock
    from contract_agent.resilience import ResilientBackend

    graph = _build_graph(ResilientBackend(Mock()), KeywordRetriever(), sink=_NullSink(), checkpointer=saver)
    snapshot = graph.get_state({"configurable": {"thread_id": "CTR-1001"}})
    assert snapshot.next  # something is still pending: the gate
    decided = svc.approve("CTR-1001", approver="counsel@example.com", reason="Cleared.")
    assert decided.status == "approved"
    snapshot = graph.get_state({"configurable": {"thread_id": "CTR-1001"}})
    assert not snapshot.next  # thread completed


class _NullSink:
    def emit(self, event):
        pass
