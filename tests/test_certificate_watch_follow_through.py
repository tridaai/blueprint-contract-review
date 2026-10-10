"""Certificate-watch follow-through (round 17): the renewal
watch announced a relied-upon certificate expiring mid-term,
and until now nothing recorded that anyone acted on the
announcement — the covenant lapse got its closing acts in
round 16, and the watch is the same shape of open loop. A
named act on the record the watch stands on now records
renewal_chased (counsel chased the renewal) or risk_accepted
(counsel accepted the lapsing cover, the note carrying the
reason), and the watch rows read the acts back as a state.
The acts close the question of whether anyone acted; they do
not close the watch — only a renewal evidencing the
requirement set does that.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from contract_agent.api import app
from contract_agent.certificates import watch_follow_through_state
from contract_agent.samples import sample_contract_models
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore

TODAY = date(2026, 10, 10)  # CTR-2029's cert is 82 days out: watched


@pytest.fixture(scope="module")
def samples() -> dict:
    return {c.contract_id: c for c in sample_contract_models()}


def _service(samples) -> ContractService:
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(samples["CTR-2027"])  # cert consistent, expires 2027-03-31
    svc.analyze(samples["CTR-2028"])  # cert failed review — never a watch
    svc.analyze(samples["CTR-2029"])  # cert consistent, expires 2026-12-31
    return svc


# ---------------------------------------------------------------------------
# The state reading
# ---------------------------------------------------------------------------


def test_state_reads_the_furthest_act_reached():
    assert watch_follow_through_state([]) == "open"
    assert watch_follow_through_state(
        [{"state": "renewal_chased"}]
    ) == "renewal_chased"
    assert watch_follow_through_state(
        [{"state": "renewal_chased"}, {"state": "risk_accepted"}]
    ) == "risk_accepted"


def test_a_watched_certificate_reads_open_before_any_act(samples):
    svc = _service(samples)
    reading = svc.certificate_watch("CTR-2029", today=TODAY)
    assert reading["watched"] is True
    assert reading["expiry_date"] == "2026-12-31"
    assert reading["follow_through"] == []
    assert reading["follow_through_state"] == "open"
    # The list surface reads the same state.
    view = svc.certificate_watches(today=TODAY)
    row = view["certificates"][0]
    assert row["contract_id"] == "CTR-2029"
    assert row["follow_through_state"] == "open"


def test_an_unwatched_contract_reads_as_unwatched(samples):
    svc = _service(samples)
    # Failed review: a finding, never a watch.
    failed = svc.certificate_watch("CTR-2028", today=TODAY)
    assert failed["watched"] is False
    # Relied upon but outside the window at this date.
    outside = svc.certificate_watch("CTR-2027", today=TODAY)
    assert outside["watched"] is False
    with pytest.raises(KeyError):
        svc.certificate_watch("CTR-9999", today=TODAY)


# ---------------------------------------------------------------------------
# The acts: named, on the record the watch stands on, closed once
# ---------------------------------------------------------------------------


def test_the_acts_close_the_question_in_order(samples):
    svc = _service(samples)
    reading = svc.record_certificate_watch_follow_through(
        "CTR-2029", "renewal_chased", actor="A. Counsel",
        note="Broker chased by email", today=TODAY,
    )
    assert reading["follow_through_state"] == "renewal_chased"
    act = reading["follow_through"][0]
    assert act["actor"] == "A. Counsel"
    assert act["note"] == "Broker chased by email"
    assert act["expiry_date"] == "2026-12-31"
    assert act["exhibit_document_id"]
    reading = svc.record_certificate_watch_follow_through(
        "CTR-2029", "risk_accepted", actor="B. Counsel",
        note="Counterparty is exiting the service; cover lapses "
             "with the engagement", today=TODAY,
    )
    assert reading["follow_through_state"] == "risk_accepted"
    assert [a["state"] for a in reading["follow_through"]] == [
        "renewal_chased", "risk_accepted",
    ]
    # The watch itself is still open: the acts record that
    # someone acted; only evidenced renewal closes a watch.
    assert reading["watched"] is True


def test_the_acts_are_refused_where_there_is_no_watch(samples):
    svc = _service(samples)
    # A certificate that failed review is a finding, not a watch.
    with pytest.raises(ValueError, match="no certificate on the renewal watch"):
        svc.record_certificate_watch_follow_through(
            "CTR-2028", "renewal_chased", actor="A. Counsel", today=TODAY,
        )
    # Relied upon, but outside the window: no running watch.
    with pytest.raises(ValueError, match="no certificate on the renewal watch"):
        svc.record_certificate_watch_follow_through(
            "CTR-2027", "renewal_chased", actor="A. Counsel", today=TODAY,
        )
    with pytest.raises(KeyError):
        svc.record_certificate_watch_follow_through(
            "CTR-9999", "renewal_chased", actor="A. Counsel", today=TODAY,
        )


def test_the_acts_bind_to_a_name_a_state_and_a_reason(samples):
    svc = _service(samples)
    with pytest.raises(ValueError, match="named actor"):
        svc.record_certificate_watch_follow_through(
            "CTR-2029", "renewal_chased", actor="  ", today=TODAY,
        )
    with pytest.raises(ValueError, match="not a watch follow-through state"):
        svc.record_certificate_watch_follow_through(
            "CTR-2029", "renewed", actor="A. Counsel", today=TODAY,
        )
    # Accepting a risk without its reason is not a record.
    with pytest.raises(ValueError, match="needs the note"):
        svc.record_certificate_watch_follow_through(
            "CTR-2029", "risk_accepted", actor="A. Counsel", today=TODAY,
        )


def test_a_state_is_recorded_once(samples):
    svc = _service(samples)
    svc.record_certificate_watch_follow_through(
        "CTR-2029", "renewal_chased", actor="A. Counsel", today=TODAY,
    )
    with pytest.raises(ValueError, match="already carries a renewal_chased act"):
        svc.record_certificate_watch_follow_through(
            "CTR-2029", "renewal_chased", actor="B. Counsel", today=TODAY,
        )


def test_the_acts_survive_a_store_round_trip(samples):
    store = InMemoryStore()
    svc = ContractService(store=store, checkpointer=False)
    svc.analyze(samples["CTR-2029"])
    svc.record_certificate_watch_follow_through(
        "CTR-2029", "renewal_chased", actor="A. Counsel", today=TODAY,
    )
    record = store.get("CTR-2029")
    assert [a["state"] for a in record.certificate_watch_follow_throughs] == [
        "renewal_chased"
    ]
    # A fresh service over the same store reads the acts back.
    svc2 = ContractService(store=store, checkpointer=False)
    reading = svc2.certificate_watch("CTR-2029", today=TODAY)
    assert reading["follow_through_state"] == "renewal_chased"


def test_the_biography_carries_the_watchs_acts(samples):
    svc = _service(samples)
    svc.record_certificate_watch_follow_through(
        "CTR-2029", "renewal_chased", actor="A. Counsel", today=TODAY,
    )
    history = svc.annex_history("CTR-2029")
    watch = history["certificates"]["watch"]
    assert watch["follow_through_state"] == "renewal_chased"
    assert watch["follow_through"][0]["actor"] == "A. Counsel"


def test_watch_follow_through_endpoints(samples):
    svc = _service(samples)
    import contract_agent.api as api

    original = api.service
    api.service = svc
    try:
        client = TestClient(app)
        reading = client.get("/contracts/CTR-2029/certificate-watch")
        assert reading.status_code == 200
        assert reading.json()["follow_through_state"] == "open"
        acted = client.post(
            "/contracts/CTR-2029/certificate-watch/follow-through",
            json={"state": "renewal_chased", "actor": "A. Counsel"},
        )
        assert acted.status_code == 200
        assert acted.json()["follow_through_state"] == "renewal_chased"
        refused = client.post(
            "/contracts/CTR-2028/certificate-watch/follow-through",
            json={"state": "renewal_chased", "actor": "A. Counsel"},
        )
        assert refused.status_code == 422
        missing = client.get("/contracts/CTR-9999/certificate-watch")
        assert missing.status_code == 404
    finally:
        api.service = original
