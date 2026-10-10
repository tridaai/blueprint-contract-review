"""Follow-through-aware covenant announcements (round 17):
round 15 taught the deadline sweep to announce covenant
expiries as covenant_expiring; round 16 gave a lapsed covenant
a follow-through that closes the loop on the record. But the
sweep had no dedupe at all: it announced every due covenant on
every run, forever — including lapses that were announced,
acted on, and confirmed closed. The sweep now reads the
follow-through state it helped create: a covenant whose lapse
stands confirmed (restriction_confirmed_lifted) is counted as
closed, not re-announced, while a lapse still open — or only
as far as the counterparty being notified — keeps announcing.
The acts stay on the record untouched: the announced history is
the record's, and the sweep only reads it.
"""

from __future__ import annotations

from datetime import date

import pytest

from contract_agent.events import CollectingSink
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore

LAPSED = date(2028, 8, 15)  # after the 2028-06-30 covenant expiry


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def _dated_letter(samples, contract_id: str = "CTR-FTA-1") -> ContractInput:
    """CTR-2025 with a stated termination date (the covenant
    sweep tests' idiom): both covenants expire 2028-06-30."""
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


def _service(samples) -> ContractService:
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(_dated_letter(samples))
    return svc


def _close(svc: ContractService, kind: str) -> None:
    svc.record_covenant_follow_through(
        "CTR-FTA-1", kind, "counterparty_notified",
        actor="A. Counsel", today=LAPSED,
    )
    svc.record_covenant_follow_through(
        "CTR-FTA-1", kind, "restriction_confirmed_lifted",
        actor="A. Counsel", today=LAPSED,
    )


def _sweep(svc: ContractService):
    sink = CollectingSink()
    report = svc.dispatch_obligation_sweep(
        days=90, today=LAPSED, event_sink=sink
    )
    events = [e for e in sink.events if e.type == "covenant_expiring"]
    return report, events


def test_a_confirmed_lapse_is_counted_closed_not_announced(samples):
    svc = _service(samples)
    _close(svc, "non_compete")
    _close(svc, "non_solicit")
    report, events = _sweep(svc)
    assert events == []
    # Both covenants are still due rows on the register...
    assert report["covenant_count"] == 2
    # ...but neither drew an announcement; both read as closed.
    assert report["covenant_events_emitted"] == 0
    assert report["covenant_closed_count"] == 2
    assert {row["kind"] for row in report["closed_covenants"]} == {
        "non_compete", "non_solicit",
    }
    for row in report["closed_covenants"]:
        assert row["follow_through_state"] == "restriction_confirmed_lifted"
    # The split still adds up: events emitted = due rows minus
    # the closed covenants (plus the certificate watch's own).
    assert report["events_emitted"] == (
        report["due_count"] - 2
    )
    assert report["obligation_events_emitted"] == (
        report["due_count"] - report["covenant_count"]
    )


def test_an_open_lapse_keeps_announcing(samples):
    svc = _service(samples)
    report, events = _sweep(svc)
    assert report["covenant_closed_count"] == 0
    assert report["closed_covenants"] == []
    assert report["covenant_events_emitted"] == 2
    assert len(events) == 2
    assert all(
        e.detail["follow_through_state"] == "open" for e in events
    )


def test_a_notified_but_unconfirmed_lapse_keeps_announcing(samples):
    # Half-closed is not closed: the counterparty has been told,
    # but until the lapse stands confirmed the chase continues.
    svc = _service(samples)
    svc.record_covenant_follow_through(
        "CTR-FTA-1", "non_compete", "counterparty_notified",
        actor="A. Counsel", today=LAPSED,
    )
    report, events = _sweep(svc)
    assert report["covenant_closed_count"] == 0
    assert report["covenant_events_emitted"] == 2
    by_kind = {e.detail["kind"]: e for e in events}
    assert by_kind["non_compete"].detail["follow_through_state"] == (
        "counterparty_notified"
    )
    assert by_kind["non_solicit"].detail["follow_through_state"] == "open"


def test_the_kinds_close_independently(samples):
    svc = _service(samples)
    _close(svc, "non_compete")
    report, events = _sweep(svc)
    assert report["covenant_count"] == 2
    assert report["covenant_closed_count"] == 1
    assert report["covenant_events_emitted"] == 1
    assert [row["kind"] for row in report["closed_covenants"]] == [
        "non_compete"
    ]
    assert [e.detail["kind"] for e in events] == ["non_solicit"]


def test_closing_stops_the_chase_and_the_record_keeps_the_history(samples):
    svc = _service(samples)
    # Announced while open...
    _, first = _sweep(svc)
    assert len(first) == 2
    # ...then closed, and every later sweep counts it closed
    # instead of announcing it again.
    _close(svc, "non_compete")
    _close(svc, "non_solicit")
    for _ in range(2):
        report, events = _sweep(svc)
        assert events == []
        assert report["covenant_closed_count"] == 2
    # The announced history stays on the record: both acts, in
    # order, exactly as recorded — the sweep read them, it did
    # not spend them.
    reading = svc.covenant_follow_through("CTR-FTA-1", today=LAPSED)
    for row in reading["covenants"]:
        assert [a["state"] for a in row["follow_through"]] == [
            "counterparty_notified",
            "restriction_confirmed_lifted",
        ]
        assert row["follow_through_state"] == "restriction_confirmed_lifted"
    assert reading["open_lapses"] == 0
