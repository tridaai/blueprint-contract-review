"""Serial-renewal visibility for suspensions (round 15):
re-confirmation (round 13) made renewing a set-aside one quiet
act — and a quiet act can repeat forever, one review at a time,
until a "temporary" set-aside is permanent in everything but
name. The calibration report now reads each suspension's
renewals AS A SERIES: the count, the span from the original
act to the latest renewal, and the full history ride on the
row, and a suspension renewed at least three times is named
in the headline list serially_renewed_types — a standing
decision wearing a review date is a different fact from a
fresh suspension. Naming changes nothing about the scorer:
the suspension binds exactly as before.
"""

from __future__ import annotations

from datetime import date

from contract_agent.calibration_store import InMemoryCalibrationStore
from contract_agent.pattern_calibration import (
    SERIAL_RENEWAL_THRESHOLD,
    pattern_calibration,
    renewal_series,
)
from contract_agent.schemas import PatternTermSuspension, SuspensionReconfirmation
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore

TODAY = date(2026, 10, 10)


def _suspension(renewals: list[tuple[str, str, str]],
                status: str = "active") -> PatternTermSuspension:
    return PatternTermSuspension(
        clause_type="payment_terms",
        status=status,
        suspended_by="C. Ounsel",
        suspended_at="2026-01-05T09:00:00+00:00",
        reason="The term never decided anything on the winter chains.",
        review_by="2027-01-01",
        reconfirmations=[
            SuspensionReconfirmation(
                actor=actor, at=at, note=note, review_by="2027-01-01"
            )
            for actor, at, note in renewals
        ],
    )


_THREE_RENEWALS = [
    ("D. Reviewer", "2026-03-05T09:00:00+00:00", "Still never decisive."),
    ("D. Reviewer", "2026-05-20T09:00:00+00:00", "Spring chains agree."),
    ("E. Second", "2026-08-05T09:00:00+00:00", "Kept again on review."),
]


def _report(suspension: PatternTermSuspension) -> dict:
    return pattern_calibration([], suspensions=[suspension], today=TODAY)


def _row(report: dict) -> dict:
    return next(
        row for row in report["clause_types"]
        if row["clause_type"] == "payment_terms"
    )


# ---------------------------------------------------------------------------
# The series reading itself
# ---------------------------------------------------------------------------


def test_a_thrice_renewed_suspension_reads_as_a_series():
    suspension = _suspension(_THREE_RENEWALS)
    series = renewal_series(suspension)
    assert series["renewal_count"] == 3
    assert series["renewal_span_days"] == 212  # Jan 5 -> Aug 5
    assert series["renewal_span_months"] == 7.0
    assert series["serially_renewed"] is True
    assert [h["actor"] for h in series["renewal_history"]] == [
        "D. Reviewer", "D. Reviewer", "E. Second",
    ]
    assert series["renewal_history"][0]["note"] == "Still never decisive."


def test_the_threshold_is_the_line_between_a_renewal_and_a_pattern():
    two = renewal_series(_suspension(_THREE_RENEWALS[:2]))
    assert two["renewal_count"] == SERIAL_RENEWAL_THRESHOLD - 1
    assert two["serially_renewed"] is False
    assert renewal_series(_suspension([])) is None


# ---------------------------------------------------------------------------
# The report: the series on the row, the pattern in the headline
# ---------------------------------------------------------------------------


def test_the_report_names_the_serial_pattern_and_carries_the_series():
    report = _report(_suspension(_THREE_RENEWALS))
    assert report["serially_renewed_types"] == ["payment_terms"]
    block = _row(report)["suspension"]
    assert block["renewal_count"] == 3
    assert block["renewal_span_days"] == 212
    assert block["renewal_span_months"] == 7.0
    assert block["serially_renewed"] is True
    assert len(block["renewal_history"]) == 3
    detail = _row(report)["detail"]
    assert "Re-confirmed 3 time(s) across 7 months" in detail
    assert "serial renewal" in detail


def test_two_renewals_carry_the_series_without_the_pattern_name():
    report = _report(_suspension(_THREE_RENEWALS[:2]))
    assert report["serially_renewed_types"] == []
    block = _row(report)["suspension"]
    assert block["renewal_count"] == 2
    assert block["serially_renewed"] is False
    assert "serial renewal" not in _row(report)["detail"]


def test_a_never_renewed_suspension_has_no_series_on_its_row():
    report = _report(_suspension([]))
    assert report["serially_renewed_types"] == []
    block = _row(report)["suspension"]
    assert "renewal_count" not in block
    assert "renewal_history" not in block


def test_a_lifted_suspension_is_not_a_standing_pattern():
    # The series reading is about set-asides still in force: a
    # lifted suspension's renewals are its history, not a
    # pattern counsel is currently maintaining.
    report = _report(_suspension(_THREE_RENEWALS, status="lifted"))
    assert report["serially_renewed_types"] == []


def test_the_series_rides_the_service_report_and_metrics():
    store = InMemoryCalibrationStore()
    store.save(_suspension(_THREE_RENEWALS))
    svc = ContractService(
        store=InMemoryStore(), checkpointer=False, calibration_store=store
    )
    assert svc.pattern_calibration()["serially_renewed_types"] == [
        "payment_terms"
    ]
    assert svc.metrics()["pattern_calibration"]["serially_renewed_types"] == [
        "payment_terms"
    ]
