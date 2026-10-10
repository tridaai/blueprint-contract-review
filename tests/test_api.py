"""API surface: analysis, gate decisions, auth, probes, evals."""

from __future__ import annotations

from fastapi.testclient import TestClient

from contract_agent.api import app
from contract_agent.samples import sample_contract_models

client = TestClient(app)


def test_health_and_readiness():
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    readiness = client.get("/readiness")
    assert readiness.status_code == 200
    assert readiness.json()["status"] == "ready"
    assert readiness.json()["checks"]["database"].startswith("in-memory")


def test_console_and_samples():
    console = client.get("/")
    assert console.status_code == 200
    assert "Contract Review Agent" in console.text
    samples = client.get("/samples")
    assert samples.status_code == 200
    assert len(samples.json()) == 12
    playbook = client.get("/playbook")
    assert playbook.status_code == 200
    assert len(playbook.json()) >= 20


def test_analyze_then_gate_round_trip():
    contract = sample_contract_models()[1]  # uncapped liability
    res = client.post("/contracts/analyze", json=contract.model_dump())
    assert res.status_code == 200
    result = res.json()
    assert result["status"] == "awaiting_counsel"
    assert result["facts"]["posture"] == "escalation_required"
    assert result["contract_id"] == contract.contract_id
    assert res.headers.get("X-Request-ID")

    decided = client.post(
        f"/contracts/{contract.contract_id}/approve",
        json={"actor": "C. Ounsel", "reason": "escalate to the counterparty"},
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "approved"
    assert decided.json()["external_action_taken"] is False

    fetched = client.get(f"/contracts/{contract.contract_id}")
    assert fetched.status_code == 200
    assert fetched.json()["decision"] == "approved"


def test_unknown_contract_404s():
    assert client.get("/contracts/CTR-9999").status_code == 404
    assert client.post(
        "/contracts/CTR-9999/approve", json={"actor": "C. Ounsel"}
    ).status_code == 404


def test_decision_requires_an_actor():
    res = client.post("/contracts/CTR-2001/approve", json={})
    assert res.status_code in (404, 422)


def test_api_key_gate(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-secret")
    assert client.get("/samples").status_code == 401
    ok = client.get("/samples", headers={"X-API-Key": "test-secret"})
    assert ok.status_code == 200
    # Probes stay open either way.
    assert client.get("/health").status_code == 200
    assert client.get("/readiness").status_code == 200


def test_stream_endpoint_emits_sse_events():
    contract = sample_contract_models()[0]
    res = client.post("/contracts/analyze/stream", json=contract.model_dump())
    assert res.status_code == 200
    assert "text/event-stream" in res.headers["content-type"]
    body = res.text
    assert "run_completed" in body
    assert '"contract_id": "CTR-2001"' in body


def test_eval_results_endpoint():
    res = client.get("/evals/results")
    assert res.status_code == 200
    payload = res.json()
    assert payload["total"] == 42
    assert payload["passed"] is True
