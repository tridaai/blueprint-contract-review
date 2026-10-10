"""Suspension review dates (round 12): a pattern-term suspension
is a decision with a shelf life. Taken on one chain's evidence,
it carries a review-by date; the calibration report reads dated
suspensions against today — scheduled, due, or overdue (stale) —
and the queue surface carries the due ones as alerts. An overdue
suspension still binds the scorer: staleness is counsel's prompt
to re-confirm or lift, never the agent quietly restoring a term.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from contract_agent.api import app
from contract_agent.calibration_store import InMemoryCalibrationStore
from contract_agent.pattern_calibration import (
    pattern_calibration,
    suspension_review_alerts,
)
from contract_agent.schemas import PatternTermSuspension
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore

TODAY = date(2026, 10, 10)


def _service() -> ContractService:
    return ContractService(
        store=InMemoryStore(),
        checkpointer=False,
        calibration_store=InMemoryCalibrationStore(),
    )


def _suspension(clause_type: str, review_by: str = "",
                status: str = "active") -> PatternTermSuspension:
    return PatternTermSuspension(
        clause_type=clause_type,
        status=status,
        suspended_by="C. Ounsel",
        suspended_at="2026-09-01T09:00:00+00:00",
        reason="The term never decided anything on the spring chains.",
        review_by=review_by,
    )


# ---------------------------------------------------------------------------
# The act: the date is recorded, and must be a date
# ---------------------------------------------------------------------------


def test_suspend_records_the_review_date():
    svc = _service()
    suspension = svc.suspend_pattern_term(
        "payment_terms", actor="C. Ounsel", reason="Never decisive.",
        review_by="2026-12-01",
    )
    assert suspension.review_by == "2026-12-01"
    assert svc.pattern_suspensions()[0].review_by == "2026-12-01"


def test_suspend_rejects_a_review_date_that_is_not_a_date():
    svc = _service()
    with pytest.raises(ValueError, match="not an ISO date"):
        svc.suspend_pattern_term(
            "payment_terms", actor="C. Ounsel", reason="Never decisive.",
            review_by="next spring",
        )


def test_resuspend_after_lift_takes_the_new_review_date():
    svc = _service()
    svc.suspend_pattern_term(
        "payment_terms", actor="C. Ounsel", reason="First take.",
        review_by="2026-11-01",
    )
    svc.lift_pattern_suspension("payment_terms", actor="C. Ounsel")
    suspension = svc.suspend_pattern_term(
        "payment_terms", actor="C. Ounsel", reason="Second take.",
        review_by="2027-03-01",
    )
    assert suspension.review_by == "2027-03-01"
    assert suspension.reason == "Second take."


# ---------------------------------------------------------------------------
# The reading: scheduled, due, overdue — and silence where honest
# ---------------------------------------------------------------------------


def test_review_states_against_today():
    suspensions = [
        _suspension("far_type", review_by="2027-06-01"),
        _suspension("due_type", review_by="2026-10-25"),
        _suspension("stale_type", review_by="2026-09-15"),
        _suspension("undated_type"),
        _suspension("lifted_type", review_by="2026-09-15", status="lifted"),
    ]
    alerts = suspension_review_alerts(suspensions, TODAY)
    assert [a["clause_type"] for a in alerts] == ["stale_type", "due_type"]
    assert alerts[0]["review_state"] == "overdue"
    assert alerts[0]["days_until_review"] == -25
    assert alerts[1]["review_state"] == "due"
    assert alerts[1]["days_until_review"] == 15


def test_report_names_due_reviews_on_the_row_and_the_headline():
    report = pattern_calibration(
        [], suspensions=[_suspension("stale_type", review_by="2026-09-15")],
        today=TODAY,
    )
    assert report["suspensions_due_review"] == ["stale_type"]
    row = report["clause_types"][0]
    assert row["suspended"] is True
    assert row["suspension"]["review_by"] == "2026-09-15"
    assert row["suspension"]["review_state"] == "overdue"
    assert "stale" in row["detail"]
    assert "2026-09-15" in row["detail"]


def test_report_scheduled_review_is_named_but_not_alerted():
    report = pattern_calibration(
        [], suspensions=[_suspension("far_type", review_by="2027-06-01")],
        today=TODAY,
    )
    assert report["suspensions_due_review"] == []
    row = report["clause_types"][0]
    assert row["suspension"]["review_state"] == "scheduled"
    assert "Review falls due on 2027-06-01" in row["detail"]


def test_undated_suspension_reads_as_before():
    report = pattern_calibration(
        [], suspensions=[_suspension("plain_type")], today=TODAY,
    )
    assert report["suspensions_due_review"] == []
    row = report["clause_types"][0]
    assert "review_by" not in row["suspension"]
    assert "Review falls due" not in row["detail"]


# ---------------------------------------------------------------------------
# The surfaces: queue alerts and the metrics headline
# ---------------------------------------------------------------------------


def test_service_alerts_and_metrics_count():
    svc = _service()
    svc.suspend_pattern_term(
        "payment_terms", actor="C. Ounsel", reason="Never decisive.",
        review_by="2026-09-01",  # already past: stale on arrival
    )
    alerts = svc.suspension_review_alerts(today=TODAY)
    assert [a["clause_type"] for a in alerts] == ["payment_terms"]
    assert alerts[0]["review_state"] == "overdue"
    assert svc.metrics()["pattern_calibration"]["suspensions_due_review"] == 1


def test_queue_payload_carries_the_alerts():
    client = TestClient(app)
    res = client.post(
        "/options/pattern-calibration/suspend",
        json={
            "clause_type": "governing_law",
            "actor": "C. Ounsel",
            "note": "Set aside pending the autumn chains.",
            "review_by": "2020-01-01",
        },
    )
    assert res.status_code == 200
    assert res.json()["review_by"] == "2020-01-01"
    queue = client.get("/queue").json()
    alerts = queue["suspension_review_alerts"]
    assert [a["clause_type"] for a in alerts] == ["governing_law"]
    assert alerts[0]["review_state"] == "overdue"
    # The API's service is shared across the suite: leave the
    # calibration store as this test found it.
    lifted = client.post(
        "/options/pattern-calibration/lift",
        json={"clause_type": "governing_law", "actor": "C. Ounsel"},
    )
    assert lifted.status_code == 200


def test_suspend_endpoint_rejects_a_bad_review_date():
    client = TestClient(app)
    res = client.post(
        "/options/pattern-calibration/suspend",
        json={
            "clause_type": "confidentiality",
            "actor": "C. Ounsel",
            "note": "Set aside.",
            "review_by": "whenever",
        },
    )
    assert res.status_code == 422
