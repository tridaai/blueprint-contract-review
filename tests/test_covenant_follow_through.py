"""Covenant-lapse follow-through (round 16): the deadline
sweep announces a covenant's expiry — a restriction lifting —
but an announcement is not a closed loop, and nothing
downstream remembered the lapse: a covenant past its expiry
still read like any other register row. The follow-through
closes the loop on the record: once a covenant has lapsed, a
named act records that the counterparty was notified the
restriction lifted, and a second confirms the lapse — the
register then reads the covenant in its true state (open,
notified, confirmed), never as a live restriction. The acts
bind to reality: no follow-through on an undated covenant or
one still running, and each state is recorded once.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from contract_agent.obligations import follow_through_state
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore

LAPSED = date(2028, 8, 15)  # both covenants expired 2028-06-30
RUNNING = date(2028, 6, 1)  # both covenants still running


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def _dated_letter(samples, contract_id: str = "CTR-FT-1") -> ContractInput:
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


def _service(samples, contract_id: str = "CTR-FT-1") -> ContractService:
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(_dated_letter(samples, contract_id))
    return svc


# ---------------------------------------------------------------------------
# The state reading
# ---------------------------------------------------------------------------


def test_state_reads_the_furthest_act_reached():
    acts = [{"kind": "non_compete", "state": "counterparty_notified"}]
    assert follow_through_state(acts, "non_compete", lapsed=True) == (
        "counterparty_notified"
    )
    assert follow_through_state(acts, "non_solicit", lapsed=True) == "open"
    assert follow_through_state([], "non_compete", lapsed=False) == "pending_lapse"
    both = acts + [
        {"kind": "non_compete", "state": "restriction_confirmed_lifted"}
    ]
    assert follow_through_state(both, "non_compete", lapsed=True) == (
        "restriction_confirmed_lifted"
    )


def test_a_lapsed_covenant_reads_open_before_any_act(samples):
    svc = _service(samples)
    reading = svc.covenant_follow_through("CTR-FT-1", today=LAPSED)
    assert {row["kind"] for row in reading["covenants"]} == {
        "non_compete", "non_solicit",
    }
    for row in reading["covenants"]:
        assert row["date"] == "2028-06-30"
        assert row["lapsed"] is True
        assert row["follow_through"] == []
        assert row["follow_through_state"] == "open"
    assert reading["open_lapses"] == 2
    # Before the expiry the same covenants are not lapses at all.
    early = svc.covenant_follow_through("CTR-FT-1", today=RUNNING)
    assert all(
        row["follow_through_state"] == "pending_lapse"
        for row in early["covenants"]
    )
    assert early["open_lapses"] == 0


# ---------------------------------------------------------------------------
# The acts: named, sequenced by the record, closed once
# ---------------------------------------------------------------------------


def test_the_acts_close_the_loop_in_order(samples):
    svc = _service(samples)
    reading = svc.record_covenant_follow_through(
        "CTR-FT-1", "non_compete", "counterparty_notified",
        actor="C. Ounsel", note="Told their counsel on the call.",
        today=LAPSED,
    )
    row = next(r for r in reading["covenants"] if r["kind"] == "non_compete")
    assert row["follow_through_state"] == "counterparty_notified"
    assert row["follow_through"][0]["actor"] == "C. Ounsel"
    assert row["follow_through"][0]["note"] == "Told their counsel on the call."
    assert reading["open_lapses"] == 1  # the non-solicit is still open

    reading = svc.record_covenant_follow_through(
        "CTR-FT-1", "non_compete", "restriction_confirmed_lifted",
        actor="C. Ounsel", today=LAPSED,
    )
    row = next(r for r in reading["covenants"] if r["kind"] == "non_compete")
    assert row["follow_through_state"] == "restriction_confirmed_lifted"
    assert len(row["follow_through"]) == 2
    # The other covenant is untouched by this one's acts.
    other = next(r for r in reading["covenants"] if r["kind"] == "non_solicit")
    assert other["follow_through_state"] == "open"


def test_the_acts_are_refused_where_there_is_no_lapse(samples):
    svc = _service(samples)
    # Still running: the refusal names the day it lifts.
    with pytest.raises(ValueError, match="does not lapse until 2028-06-30"):
        svc.record_covenant_follow_through(
            "CTR-FT-1", "non_compete", "counterparty_notified",
            actor="C. Ounsel", today=RUNNING,
        )
    # No actor, no act.
    with pytest.raises(ValueError, match="named actor"):
        svc.record_covenant_follow_through(
            "CTR-FT-1", "non_compete", "counterparty_notified",
            actor=" ", today=LAPSED,
        )
    # Not the register's kinds or states.
    with pytest.raises(ValueError, match="not a restrictive covenant"):
        svc.record_covenant_follow_through(
            "CTR-FT-1", "severance", "counterparty_notified",
            actor="C. Ounsel", today=LAPSED,
        )
    with pytest.raises(ValueError, match="not a follow-through state"):
        svc.record_covenant_follow_through(
            "CTR-FT-1", "non_compete", "chased",
            actor="C. Ounsel", today=LAPSED,
        )
    with pytest.raises(KeyError):
        svc.record_covenant_follow_through(
            "CTR-NOPE", "non_compete", "counterparty_notified",
            actor="C. Ounsel", today=LAPSED,
        )


def test_an_undated_covenant_has_no_lapse_to_follow_through(samples):
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(samples["CTR-2025"])  # no termination date stated
    with pytest.raises(ValueError, match="no dated non_compete covenant"):
        svc.record_covenant_follow_through(
            "CTR-2025", "non_compete", "counterparty_notified",
            actor="C. Ounsel", today=LAPSED,
        )
    assert svc.covenant_follow_through("CTR-2025", today=LAPSED)["covenants"] == []


def test_a_state_is_recorded_once(samples):
    svc = _service(samples)
    svc.record_covenant_follow_through(
        "CTR-FT-1", "non_solicit", "counterparty_notified",
        actor="C. Ounsel", today=LAPSED,
    )
    with pytest.raises(ValueError, match="already carries a "
                                         "counterparty_notified act"):
        svc.record_covenant_follow_through(
            "CTR-FT-1", "non_solicit", "counterparty_notified",
            actor="D. Reviewer", today=LAPSED,
        )


def test_the_acts_survive_a_store_round_trip(samples):
    from contract_agent.store import _dump_record, _load_record

    svc = _service(samples)
    svc.record_covenant_follow_through(
        "CTR-FT-1", "non_compete", "counterparty_notified",
        actor="C. Ounsel", today=LAPSED,
    )
    record = svc._get_store().get("CTR-FT-1")
    restored = _load_record(_dump_record(record))
    assert restored.covenant_follow_throughs == record.covenant_follow_throughs
    assert restored.covenant_follow_throughs[0]["state"] == (
        "counterparty_notified"
    )


# ---------------------------------------------------------------------------
# The surfaces: due view rows and the endpoints
# ---------------------------------------------------------------------------


def test_due_rows_carry_the_follow_through(samples):
    svc = _service(samples)
    svc.record_covenant_follow_through(
        "CTR-FT-1", "non_compete", "restriction_confirmed_lifted",
        actor="C. Ounsel", today=LAPSED,
    )
    due = svc.obligations_due(days=90, today=LAPSED)
    covenants = {
        row["kind"]: row for row in due["obligations"]
        if row["kind"].startswith("non_")
    }
    assert covenants["non_compete"]["lapsed"] is True
    assert covenants["non_compete"]["follow_through_state"] == (
        "restriction_confirmed_lifted"
    )
    assert covenants["non_solicit"]["follow_through_state"] == "open"
    # Non-covenant rows carry no follow-through keys.
    others = [r for r in due["obligations"] if not r["kind"].startswith("non_")]
    assert all("follow_through_state" not in r for r in others)


def test_follow_through_endpoints(samples):
    from contract_agent.api import app, service as api_service

    letter = _dated_letter(samples, "CTR-FT-API")
    client = TestClient(app)
    res = client.post(
        "/contracts/analyze", json=letter.model_dump(mode="json")
    )
    assert res.status_code == 200
    # The endpoint reads against the real clock, so the dated
    # assertions live at the service level with frozen clocks;
    # here: the actor gate (date-independent) and the reads.
    missing_actor = client.post(
        "/contracts/CTR-FT-API/covenant-follow-through",
        json={"kind": "non_compete", "state": "counterparty_notified"},
    )
    assert missing_actor.status_code == 422
    assert "named actor" in missing_actor.json()["detail"]
    reading = client.get("/contracts/CTR-FT-API/covenant-follow-through")
    assert reading.status_code == 200
    assert {r["kind"] for r in reading.json()["covenants"]} == {
        "non_compete", "non_solicit",
    }
    assert client.get(
        "/contracts/CTR-UNKNOWN/covenant-follow-through"
    ).status_code == 404
    assert api_service is not None
