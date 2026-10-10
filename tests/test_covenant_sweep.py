"""Covenant-expiry announcements in the deadline sweep
(round 15): round 14 put the restrictive covenants on the
register's clock, so their expiries joined /obligations/due —
but the sweep announced them as obligation_due, in the same
vocabulary as a notice deadline. A covenant's date is the day
a restriction LIFTS, operationally the opposite of a duty
falling due, and the counterparty's counsel watches for it
for exactly that reason. The sweep now announces covenant
expiries as covenant_expiring events (kind and
effect: restriction_lapses on the event), counts them
separately in the report, and lists them under covenants —
while ordinary deadlines keep the obligation_due reading.
"""

from __future__ import annotations

from datetime import date

import pytest

from contract_agent.events import CollectingSink
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def _dated_letter(samples, contract_id: str = "CTR-COV-1") -> ContractInput:
    """CTR-2025 (both covenants at 12 months) with the letter
    stating its termination date: the covenant clocks start,
    expiring 2028-06-30, and the notice/severance machinery
    dates itself against the same day."""
    data = samples["CTR-2025"].model_dump(mode="json")
    data["contract_id"] = contract_id
    for doc in data["documents"]:
        if doc["doc_type"] == "agreement":
            doc["raw_text"] += (
                "\n\n5. Fixed Term\n"
                "The parties record that the employment terminates "
                "on June 30, 2027, when the programme of work "
                "concludes.\n"
            )
    return ContractInput.model_validate(data)


def _service_with(contract: ContractInput) -> ContractService:
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(contract)
    return svc


def test_covenant_expiries_are_announced_distinctly(samples):
    svc = _service_with(_dated_letter(samples))
    sink = CollectingSink()
    report = svc.dispatch_obligation_sweep(
        days=60, today=date(2028, 6, 1), event_sink=sink
    )
    # Both covenants expire 2028-06-30 — inside the window —
    # and neither is announced as a duty falling due.
    assert report["covenant_count"] == 2
    assert report["covenant_events_emitted"] == 2
    covenant_events = [e for e in sink.events if e.type == "covenant_expiring"]
    assert len(covenant_events) == 2
    assert {e.detail["kind"] for e in covenant_events} == {
        "non_compete", "non_solicit",
    }
    for event in covenant_events:
        assert event.contract_id == "CTR-COV-1"
        assert event.detail["via"] == "deadline_sweep"
        assert event.detail["effect"] == "restriction_lapses"
        assert event.detail["date"] == "2028-06-30"
        assert event.detail["days_until"] == 29
        assert event.detail["computed"] is True
    due_events = [e for e in sink.events if e.type == "obligation_due"]
    assert all(e.detail["kind"] not in ("non_compete", "non_solicit")
               for e in due_events)
    # The report's split adds up: every due row drew exactly one
    # event, of one vocabulary or the other.
    assert report["obligation_events_emitted"] == len(due_events)
    assert report["events_emitted"] == report["due_count"]
    assert {row["kind"] for row in report["covenants"]} == {
        "non_compete", "non_solicit",
    }


def test_an_overdue_covenant_reads_as_lapsed_not_missed(samples):
    svc = _service_with(_dated_letter(samples))
    sink = CollectingSink()
    report = svc.dispatch_obligation_sweep(
        days=30, today=date(2028, 8, 15), event_sink=sink
    )
    assert report["covenant_count"] == 2
    for row in report["covenants"]:
        assert row["overdue"] is True
    events = [e for e in sink.events if e.type == "covenant_expiring"]
    assert all(e.detail["overdue"] is True for e in events)


def test_undated_covenants_are_never_announced(samples):
    # The letter as sampled states no termination date: the
    # covenant clocks never start, the entries are undated,
    # and there is no expiry to announce however wide the
    # window.
    svc = _service_with(samples["CTR-2025"])
    sink = CollectingSink()
    report = svc.dispatch_obligation_sweep(
        days=3650, today=date(2028, 6, 1), event_sink=sink
    )
    assert report["covenant_count"] == 0
    assert report["covenant_events_emitted"] == 0
    assert not [e for e in sink.events if e.type == "covenant_expiring"]


def test_covenant_expiry_outside_the_window_is_not_announced(samples):
    svc = _service_with(_dated_letter(samples))
    sink = CollectingSink()
    report = svc.dispatch_obligation_sweep(
        days=30, today=date(2027, 1, 1), event_sink=sink
    )
    # The expiry (2028-06-30) is far outside a 30-day window
    # from January 2027.
    assert report["covenant_count"] == 0
    assert not [e for e in sink.events if e.type == "covenant_expiring"]
