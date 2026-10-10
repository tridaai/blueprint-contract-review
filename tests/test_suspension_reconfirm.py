"""Suspension re-confirmation (round 13): round 12 gave a
suspension a shelf life and made staleness visible, but acting
on the review meant lift + re-suspend — two acts, and the
record lost the continuity of a set-aside that was simply
renewed. Re-confirmation is now a first-class act on the same
record: counsel's name and note are stamped into the
re-confirmation history, the review clock resets to the date
the act names, and the original suspension's stamps — who set
the term aside, when, and why — stand untouched. It is not a
lift: the scorer honours the suspension throughout. And it is
not a second suspension: the set-aside was taken once.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from contract_agent.api import app
from contract_agent.calibration_store import InMemoryCalibrationStore
from contract_agent.pattern_calibration import pattern_calibration
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore

TODAY = date(2026, 10, 10)


def _service() -> ContractService:
    return ContractService(
        store=InMemoryStore(),
        checkpointer=False,
        calibration_store=InMemoryCalibrationStore(),
    )


def _suspended(svc: ContractService, review_by: str = "2026-09-15"):
    return svc.suspend_pattern_term(
        "payment_terms", actor="C. Ounsel",
        reason="The term never decided anything on the spring chains.",
        review_by=review_by,
    )


# ---------------------------------------------------------------------------
# The act: stamped, clock reset, original stamps untouched
# ---------------------------------------------------------------------------


def test_reconfirm_stamps_the_act_and_resets_the_clock():
    svc = _service()
    original = _suspended(svc)
    renewed = svc.reconfirm_pattern_suspension(
        "payment_terms", actor="D. Reviewer",
        note="Still never decisive on the autumn chains.",
        review_by="2027-03-01",
    )
    # The original set-aside stands exactly as taken.
    assert renewed.status == "active"
    assert renewed.suspended_by == original.suspended_by
    assert renewed.suspended_at == original.suspended_at
    assert renewed.reason == original.reason
    # The clock moved — deliberately, to the date the act named.
    assert renewed.review_by == "2027-03-01"
    # And the act itself is on the record.
    assert len(renewed.reconfirmations) == 1
    act = renewed.reconfirmations[0]
    assert act.actor == "D. Reviewer"
    assert act.note == "Still never decisive on the autumn chains."
    assert act.review_by == "2027-03-01"
    assert act.at  # stamped


def test_reconfirm_without_a_new_date_keeps_the_date_on_record():
    svc = _service()
    _suspended(svc, review_by="2027-01-15")
    renewed = svc.reconfirm_pattern_suspension(
        "payment_terms", actor="D. Reviewer", note="Reviewed; stands.",
    )
    assert renewed.review_by == "2027-01-15"
    assert renewed.reconfirmations[0].review_by == "2027-01-15"


def test_reconfirmations_accumulate_in_order():
    svc = _service()
    _suspended(svc)
    svc.reconfirm_pattern_suspension(
        "payment_terms", actor="D. Reviewer", note="First renewal.",
        review_by="2027-01-01",
    )
    renewed = svc.reconfirm_pattern_suspension(
        "payment_terms", actor="E. Second", note="Second renewal.",
        review_by="2027-06-01",
    )
    assert [r.note for r in renewed.reconfirmations] == [
        "First renewal.", "Second renewal.",
    ]
    assert renewed.review_by == "2027-06-01"


def test_reconfirm_requires_a_note():
    svc = _service()
    _suspended(svc)
    with pytest.raises(ValueError, match="requires a note"):
        svc.reconfirm_pattern_suspension(
            "payment_terms", actor="D. Reviewer", note="  ",
        )


def test_reconfirm_rejects_a_review_date_that_is_not_a_date():
    svc = _service()
    _suspended(svc)
    with pytest.raises(ValueError, match="not an ISO date"):
        svc.reconfirm_pattern_suspension(
            "payment_terms", actor="D. Reviewer", note="Reviewed.",
            review_by="next spring",
        )


def test_reconfirm_unknown_type_is_a_key_error():
    svc = _service()
    with pytest.raises(KeyError, match="No pattern-term suspension"):
        svc.reconfirm_pattern_suspension(
            "payment_terms", actor="D. Reviewer", note="Reviewed.",
        )


def test_reconfirm_a_lifted_suspension_is_refused():
    svc = _service()
    _suspended(svc)
    svc.lift_pattern_suspension("payment_terms", actor="C. Ounsel")
    with pytest.raises(ValueError, match="nothing to re-confirm"):
        svc.reconfirm_pattern_suspension(
            "payment_terms", actor="D. Reviewer", note="Reviewed.",
        )


def test_resuspension_after_lift_starts_a_fresh_history():
    svc = _service()
    _suspended(svc)
    svc.reconfirm_pattern_suspension(
        "payment_terms", actor="D. Reviewer", note="Renewed once.",
    )
    svc.lift_pattern_suspension("payment_terms", actor="C. Ounsel")
    fresh = svc.suspend_pattern_term(
        "payment_terms", actor="C. Ounsel", reason="A new take.",
        review_by="2027-03-01",
    )
    # The new set-aside is a new act: the old life's renewals do
    # not ride along as if they belonged to it.
    assert fresh.reconfirmations == []


# ---------------------------------------------------------------------------
# The readings: staleness clears, the scorer never blinked
# ---------------------------------------------------------------------------


def test_reconfirmed_suspension_is_no_longer_stale():
    svc = _service()
    _suspended(svc, review_by="2026-09-15")  # overdue at TODAY
    assert [a["clause_type"] for a in svc.suspension_review_alerts(today=TODAY)] == [
        "payment_terms"
    ]
    svc.reconfirm_pattern_suspension(
        "payment_terms", actor="D. Reviewer", note="Reviewed; stands.",
        review_by="2027-03-01",
    )
    assert svc.suspension_review_alerts(today=TODAY) == []
    assert svc.metrics()["pattern_calibration"]["suspensions_due_review"] == 0


def test_scorer_honours_the_suspension_throughout():
    svc = _service()
    _suspended(svc)
    svc.reconfirm_pattern_suspension(
        "payment_terms", actor="D. Reviewer", note="Reviewed; stands.",
        review_by="2027-03-01",
    )
    assert svc._suspended_pattern_types() == {"payment_terms"}


def test_report_names_the_reconfirmation_on_the_row():
    svc = _service()
    suspension = _suspended(svc, review_by="2026-09-15")
    svc.reconfirm_pattern_suspension(
        "payment_terms", actor="D. Reviewer", note="Reviewed; stands.",
        review_by="2027-03-01",
    )
    report = pattern_calibration(
        [], suspensions=svc.pattern_suspensions(), today=TODAY,
    )
    row = report["clause_types"][0]
    block = row["suspension"]
    assert block["reconfirmations"] == 1
    assert block["last_reconfirmed_by"] == "D. Reviewer"
    assert block["review_state"] == "scheduled"
    assert "Re-confirmed 1 time(s)" in row["detail"]
    assert "D. Reviewer" in row["detail"]
    # The original act is still the row's headline, not the renewal.
    assert block["suspended_by"] == suspension.suspended_by
    assert report["suspensions_due_review"] == []


# ---------------------------------------------------------------------------
# The API surface
# ---------------------------------------------------------------------------


def test_reconfirm_endpoint_round_trip():
    client = TestClient(app)
    res = client.post(
        "/options/pattern-calibration/suspend",
        json={
            "clause_type": "data_protection",
            "actor": "C. Ounsel",
            "note": "Set aside pending review.",
            "review_by": "2020-01-01",
        },
    )
    assert res.status_code == 200
    res = client.post(
        "/options/pattern-calibration/reconfirm",
        json={
            "clause_type": "data_protection",
            "actor": "D. Reviewer",
            "note": "Reviewed at the autumn sitting; stands.",
            "review_by": "2027-03-01",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "active"
    assert body["review_by"] == "2027-03-01"
    assert body["suspended_by"] == "C. Ounsel"
    assert len(body["reconfirmations"]) == 1
    assert body["reconfirmations"][0]["actor"] == "D. Reviewer"
    queue = client.get("/queue").json()
    assert queue["suspension_review_alerts"] == [] or all(
        a["clause_type"] != "data_protection"
        for a in queue["suspension_review_alerts"]
    )
    # The API's service is shared across the suite: leave the
    # calibration store as this test found it.
    lifted = client.post(
        "/options/pattern-calibration/lift",
        json={"clause_type": "data_protection", "actor": "C. Ounsel"},
    )
    assert lifted.status_code == 200


def test_reconfirm_endpoint_validates_actor_note_and_existence():
    client = TestClient(app)
    res = client.post(
        "/options/pattern-calibration/reconfirm",
        json={"clause_type": "confidentiality", "note": "Reviewed."},
    )
    assert res.status_code == 422  # no actor
    res = client.post(
        "/options/pattern-calibration/reconfirm",
        json={"clause_type": "confidentiality", "actor": "D. Reviewer"},
    )
    assert res.status_code == 422  # no note
    res = client.post(
        "/options/pattern-calibration/reconfirm",
        json={
            "clause_type": "confidentiality",
            "actor": "D. Reviewer",
            "note": "Reviewed.",
        },
    )
    assert res.status_code == 404  # nothing on record for the type
