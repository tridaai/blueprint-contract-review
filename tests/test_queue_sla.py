"""Counsel queue SLA + ownership (round 5): budgets by severity
tier in business days, breaches sorted first, assignees recorded."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from contract_agent.api import app
from contract_agent.samples import sample_contract_models
from contract_agent.service import (
    ContractService,
    business_days_between,
    sla_budget_business_days,
)
from contract_agent.store import InMemoryStore, _dump_record, _load_record


@pytest.fixture(scope="module")
def samples() -> dict:
    return {c.contract_id: c for c in sample_contract_models()}


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


# ---------------------------------------------------------------------------
# Business-day arithmetic + budgets
# ---------------------------------------------------------------------------


def test_business_days_skip_weekends():
    friday = date(2026, 10, 2)
    monday = date(2026, 10, 5)
    assert business_days_between(friday, monday) == 1  # only Monday counts
    assert business_days_between(friday, friday) == 0
    assert business_days_between(monday, friday) == 0  # end before start
    # A full week Mon→Mon counts the five weekdays in between + Monday.
    assert business_days_between(date(2026, 10, 5), date(2026, 10, 12)) == 5


def test_budgets_by_severity_tier(monkeypatch):
    for var in (
        "QUEUE_SLA_WALK_AWAY_BUSINESS_DAYS",
        "QUEUE_SLA_REVIEW_REQUIRED_BUSINESS_DAYS",
        "QUEUE_SLA_ACCEPTABLE_FALLBACK_BUSINESS_DAYS",
    ):
        monkeypatch.delenv(var, raising=False)
    assert sla_budget_business_days("walk_away") == 2
    assert sla_budget_business_days("review_required") == 5
    assert sla_budget_business_days("acceptable_fallback") == 10
    assert sla_budget_business_days("preferred") is None
    monkeypatch.setenv("QUEUE_SLA_WALK_AWAY_BUSINESS_DAYS", "30")
    assert sla_budget_business_days("walk_away") == 30


# ---------------------------------------------------------------------------
# SLA state on the queue
# ---------------------------------------------------------------------------


def test_breach_is_computed_against_a_frozen_clock(samples):
    svc = _service()
    contract = samples["CTR-2002"].model_copy(deep=True)  # walk_away, escalation
    contract.received_at = "2026-09-01T09:30:00"  # a Tuesday
    svc.analyze(contract)
    within = svc.queue(today=date(2026, 9, 3))  # 2 business days in
    assert within[0]["business_days_waiting"] == 2
    assert within[0]["sla_budget_business_days"] == 2
    assert within[0]["sla_breached"] is False  # at budget is not past it
    blown = svc.queue(today=date(2026, 9, 4))  # 3 business days in
    assert blown[0]["business_days_waiting"] == 3
    assert blown[0]["sla_breached"] is True


def test_breached_items_sort_ahead_of_fresher_worse_ones(samples):
    svc = _service()
    stale = samples["CTR-2011"].model_copy(deep=True)  # review_required, negotiable
    stale.received_at = "2026-08-03T09:30:00"
    svc.analyze(stale)
    fresh = samples["CTR-2002"].model_copy(deep=True)  # walk_away, escalation
    fresh.received_at = "2026-10-09T09:30:00"
    svc.analyze(fresh)
    queue = svc.queue(today=date(2026, 10, 10))
    # The stale negotiable contract has blown its 5-day budget; the
    # fresh escalation has not blown its 2-day one — breach wins.
    assert queue[0]["contract_id"] == "CTR-2011"
    assert queue[0]["sla_breached"] is True
    assert queue[1]["contract_id"] == "CTR-2002"
    assert queue[1]["sla_breached"] is False


def test_preferred_contracts_carry_no_budget(samples):
    svc = _service()
    contract = samples["CTR-2001"].model_copy(deep=True)  # aligned, preferred
    contract.received_at = "2026-01-05T09:30:00"
    svc.analyze(contract)
    item = svc.queue(today=date(2026, 10, 10))[0]
    assert item["sla_budget_business_days"] is None
    assert item["sla_breached"] is False


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


def test_assign_records_the_owner_and_survives_serialisation(samples):
    svc = _service()
    svc.analyze(samples["CTR-2002"])
    item = svc.assign("CTR-2002", assignee="Priya Nair", actor="Team Lead")
    assert item["assignee"] == "Priya Nair"
    assert item["assigned_at"]
    record = svc._get_store().get("CTR-2002")
    assert record.assigned_by == "Team Lead"
    restored = _load_record(_dump_record(record))
    assert restored.assignee == "Priya Nair"
    assert restored.assigned_by == "Team Lead"
    assert restored.assigned_at == record.assigned_at


def test_assign_rejects_unknown_and_decided_contracts(samples):
    svc = _service()
    svc.analyze(samples["CTR-2001"])
    with pytest.raises(KeyError):
        svc.assign("CTR-9999", assignee="Priya Nair", actor="Team Lead")
    svc.approve("CTR-2001", approver="C. Ounsel")
    with pytest.raises(ValueError, match="not awaiting counsel"):
        svc.assign("CTR-2001", assignee="Priya Nair", actor="Team Lead")


def test_metrics_report_the_queue_sla_state(samples):
    svc = _service()
    stale = samples["CTR-2002"].model_copy(deep=True)
    stale.received_at = "2026-08-03T09:30:00"
    svc.analyze(stale)
    svc.analyze(samples["CTR-2001"])
    sla = svc.metrics()["queue_sla"]
    assert sla["awaiting"] == 2
    assert sla["sla_breached"] >= 1  # the stale walk_away, on any real clock after receipt
    assert sla["unassigned"] == 2
    assert sla["unassigned_breached"] >= 1
    assert sla["budgets_business_days"]["walk_away"] == 2
    svc.assign("CTR-2002", assignee="Priya Nair", actor="Team Lead")
    sla = svc.metrics()["queue_sla"]
    assert sla["unassigned"] == 1
    assert sla["unassigned_breached"] == 0


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    return TestClient(app)


def test_assign_endpoint_round_trips_through_the_queue(client, samples):
    payload = samples["CTR-2002"].model_dump(mode="json")
    assert client.post("/contracts/analyze", json=payload).status_code == 200
    res = client.post(
        "/contracts/CTR-2002/assign",
        json={"actor": "Team Lead", "assignee": "Priya Nair"},
    )
    assert res.status_code == 200
    assert res.json()["assignee"] == "Priya Nair"
    queue = client.get("/queue").json()["queue"]
    mine = next(i for i in queue if i["contract_id"] == "CTR-2002")
    assert mine["assignee"] == "Priya Nair"
    assert mine["sla_budget_business_days"] == 2
    assert "business_days_waiting" in mine
    assert "sla_breached" in mine


def test_assign_endpoint_validates(client, samples):
    payload = samples["CTR-2001"].model_dump(mode="json")
    assert client.post("/contracts/analyze", json=payload).status_code == 200
    assert client.post(
        "/contracts/CTR-2001/assign", json={"actor": "Team Lead"}
    ).status_code == 422
    assert client.post(
        "/contracts/CTR-9999/assign",
        json={"actor": "Team Lead", "assignee": "Priya Nair"},
    ).status_code == 404
