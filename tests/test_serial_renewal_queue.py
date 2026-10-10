"""Serial renewals on the queue (round 16): round 15 read a
suspension's renewals as a series and named the serial ones in
the calibration report, /metrics, and the console — but the
queue, where counsel works and where the next re-confirmation
would be signed, carried only the due-review alerts. A
serially renewed set-aside is the same kind of attention as a
due review, so the queue now carries it as its own alert list,
with the renewal count and span attached. The naming moves
nothing: the scorer honours a serially renewed suspension
exactly as before, and a lifted suspension — history, not a
standing pattern — never alerts.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from contract_agent.calibration_store import InMemoryCalibrationStore
from contract_agent.pattern_calibration import serial_renewal_alerts
from contract_agent.schemas import PatternTermSuspension, SuspensionReconfirmation
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore


def _suspension(renewals: int, status: str = "active",
                clause_type: str = "payment_terms") -> PatternTermSuspension:
    acts = [
        ("D. Reviewer", "2026-03-05T09:00:00+00:00", "Still never decisive."),
        ("D. Reviewer", "2026-05-20T09:00:00+00:00", "Spring chains agree."),
        ("E. Second", "2026-08-05T09:00:00+00:00", "Kept again on review."),
        ("E. Second", "2026-09-15T09:00:00+00:00", "And again."),
    ][:renewals]
    return PatternTermSuspension(
        clause_type=clause_type,
        status=status,
        suspended_by="C. Ounsel",
        suspended_at="2026-01-05T09:00:00+00:00",
        reason="The term never decided anything on the winter chains.",
        review_by="2027-01-01",
        reconfirmations=[
            SuspensionReconfirmation(
                actor=actor, at=at, note=note, review_by="2027-01-01"
            )
            for actor, at, note in acts
        ],
    )


def _service(*suspensions: PatternTermSuspension) -> ContractService:
    store = InMemoryCalibrationStore()
    for suspension in suspensions:
        store.save(suspension)
    return ContractService(
        store=InMemoryStore(), checkpointer=False, calibration_store=store
    )


# ---------------------------------------------------------------------------
# The alert reading itself
# ---------------------------------------------------------------------------


def test_only_serial_active_suspensions_alert_with_the_series_attached():
    alerts = serial_renewal_alerts(
        [
            _suspension(3),
            _suspension(2, clause_type="liability_cap"),
            _suspension(4, status="lifted", clause_type="term_termination"),
            _suspension(0, clause_type="confidentiality"),
        ]
    )
    assert [a["clause_type"] for a in alerts] == ["payment_terms"]
    alert = alerts[0]
    assert alert["renewal_count"] == 3
    assert alert["renewal_span_days"] == 212  # Jan 5 -> Aug 5
    assert alert["renewal_span_months"] == 7.0
    assert alert["suspended_by"] == "C. Ounsel"
    assert alert["last_reconfirmed_by"] == "E. Second"
    assert alert["last_reconfirmed_at"] == "2026-08-05T09:00:00+00:00"
    assert alert["review_by"] == "2027-01-01"


def test_alerts_sort_most_renewed_first():
    alerts = serial_renewal_alerts(
        [_suspension(3), _suspension(4, clause_type="liability_cap")]
    )
    assert [a["clause_type"] for a in alerts] == [
        "liability_cap", "payment_terms",
    ]


# ---------------------------------------------------------------------------
# The queue payload: the pattern where the next renewal is signed
# ---------------------------------------------------------------------------


def test_the_service_and_queue_endpoint_carry_the_alerts():
    svc = _service(_suspension(3))
    alerts = svc.serial_renewal_alerts()
    assert len(alerts) == 1
    assert alerts[0]["renewal_count"] == 3

    from contract_agent.api import app, service as api_service

    api_store = api_service._get_calibration_store()
    api_store.save(_suspension(3, clause_type="indemnity"))
    client = TestClient(app)
    body = client.get("/queue").json()
    assert "serial_renewal_alerts" in body
    named = {a["clause_type"] for a in body["serial_renewal_alerts"]}
    assert "indemnity" in named
    # The standing alert lists ride along untouched.
    assert "suspension_review_alerts" in body
    assert "stale_webhook_failures" in body


def test_a_fresh_suspension_leaves_the_queue_alert_free():
    svc = _service(_suspension(1))
    assert svc.serial_renewal_alerts() == []
