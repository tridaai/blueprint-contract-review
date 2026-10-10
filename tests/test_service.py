"""Service tests: decisions, the guardrail gate on approval, batch,
memory/feedback, and document archiving — all with test doubles."""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.request

import pytest

from contract_agent.model_backends import MockModelBackend
from contract_agent.objectstore import InMemoryObjectStore
from contract_agent.retriever import KeywordRetriever
from contract_agent.samples import sample_contract_models
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


def test_decision_records_timestamp():
    svc = service()
    svc.analyze(contract())
    decided = svc.approve("CTR-1001", approver="counsel@example.com")
    assert decided.decided_at is not None
    assert decided.decided_at.startswith("20")


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_approval_webhook_is_hmac_signed(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["data"] = request.data
        captured["signature"] = next(
            (v for k, v in request.headers.items() if k.lower() == "x-trida-signature"),
            None,
        )
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("ACTION_WEBHOOK_URL", "https://clm.example.com/hook")
    monkeypatch.setenv("ACTION_WEBHOOK_SECRET", "hook-secret")
    svc = service()
    svc.analyze(contract())
    decided = svc.approve("CTR-1001", approver="counsel@example.com")
    assert decided.dispatch_status == "sent"
    expected = hmac.new(b"hook-secret", captured["data"], hashlib.sha256).hexdigest()
    assert captured["signature"] == f"sha256={expected}"
    payload = json.loads(captured["data"])
    assert payload["contract_id"] == "CTR-1001"
    assert "obligations" in payload and "clause_diffs" in payload


def test_metrics_and_audit_rows():
    svc = service()
    svc.analyze(contract())
    svc.analyze(contract(contract_id="CTR-1002"))
    svc.approve("CTR-1001", approver="counsel@example.com", reason="Cleared.")
    metrics = svc.metrics()
    assert metrics["runs_total"] == 2
    assert metrics["decisions"] == {"approved": 1, "rejected": 0, "awaiting": 1}
    assert metrics["by_status"]["approved"] == 1
    assert metrics["telemetry"]["provider_runs"] == 0  # offline: no tokens, honestly
    rows = {row["contract_id"]: row for row in svc.audit_rows()}
    assert rows["CTR-1001"]["decided_by"] == "counsel@example.com"
    assert rows["CTR-1001"]["decision_reason"] == "Cleared."
    assert rows["CTR-1001"]["decided_at"]
    assert rows["CTR-1001"]["validation_passed"] is True
    assert rows["CTR-1002"]["decision"] == ""


def test_queue_orders_worst_first_and_drops_decided():
    svc = service()
    samples = {c.contract_id: c for c in sample_contract_models()}
    svc.analyze(samples["CTR-2001"])  # aligned
    svc.analyze(samples["CTR-2011"])  # negotiable
    svc.analyze(samples["CTR-2002"])  # escalation_required (uncapped liability)
    queue = svc.queue()
    assert [item["contract_id"] for item in queue] == ["CTR-2002", "CTR-2011", "CTR-2001"]
    worst = queue[0]
    assert worst["posture"] == "escalation_required"
    assert worst["worst_position"] == "walk_away"
    assert worst["flagged_clauses"] >= 1
    assert all(item["autonomy_eligible"] is False or item["posture"] == "aligned" for item in queue)
    svc.approve("CTR-2002", approver="counsel@example.com", reason="Escalated and cleared.")
    assert [item["contract_id"] for item in svc.queue()] == ["CTR-2011", "CTR-2001"]


def test_queue_tie_breaks_on_age():
    svc = service()
    samples = {c.contract_id: c for c in sample_contract_models()}
    # Both escalation_required with a walk_away: CTR-2014 arrived later.
    svc.analyze(samples["CTR-2014"])
    svc.analyze(samples["CTR-2002"])
    ids = [item["contract_id"] for item in svc.queue()]
    assert ids.index("CTR-2002") < ids.index("CTR-2014")


def test_playbook_coverage_analytics():
    svc = service()
    samples = {c.contract_id: c for c in sample_contract_models()}
    svc.analyze(samples["CTR-2002"])  # uncapped liability -> PB-LIA-03 fires
    svc.analyze(samples["CTR-2014"])  # Vertex round 1: several walk-aways
    coverage = svc.metrics()["playbook_coverage"]
    assert coverage["playbook_firing"]["PB-LIA-03"] == 2
    assert "PB-LIA-03" not in coverage["playbook_never_fired"]
    assert "PB-GEN-02" in coverage["playbook_never_fired"]  # absence entry never cites a verdict
    vertex = coverage["positions_by_counterparty"]["Vertex Analytics Ltd"]
    assert vertex["walk_away"] == 2
    assert vertex["review_required"] == 3
    # every clause type entry carries its flagged denominator
    for row in coverage["clause_types_without_fallback"]:
        assert row["without_fallback"] <= row["flagged"]


def test_feedback_loop_teaches_later_runs():
    svc = service()
    svc.analyze(contract())
    svc.approve("CTR-1001", approver="counsel@example.com", reason="Standard terms; cleared as drafted.")
    second = svc.analyze(contract(contract_id="CTR-1002"))
    evidence = " ".join(second.assessment.evidence)
    assert "counsel feedback" in evidence
    assert "Standard terms; cleared as drafted." in evidence
