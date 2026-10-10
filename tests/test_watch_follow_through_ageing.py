"""Watch follow-through ageing (round 18): round 17 gave the
certificate watch its follow-through acts, but a chase is a
question — and a question can go unanswered. A renewal chased
past the stale threshold (CERTIFICATE_WATCH_FOLLOW_THROUGH_
STALE_DAYS, default 30) with the watch still standing — no
evidenced renewal has arrived, or the watch would have moved —
is a chase that went quiet: it reads stale on the watch row
and resurfaces on the counsel queue beside the other
attention lists. An accepted risk is a decision, not a
question, and never goes stale; an open watch has no chase
to age.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from contract_agent.certificates import follow_through_ageing
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore

TODAY = date(2026, 10, 10)  # CTR-2029's cert is 82 days out: watched


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def _service(samples) -> ContractService:
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(samples["CTR-2029"])  # cert consistent, expires 2026-12-31
    return svc


def _chase(svc: ContractService, actor: str = "Counsel — A. Reyes") -> None:
    svc.record_certificate_watch_follow_through(
        "CTR-2029", "renewal_chased", actor, today=TODAY
    )


def _act_day(svc: ContractService) -> date:
    """The calendar day the recorded chase was stamped with —
    ages are measured from the act, not assumed."""
    record = svc._get_store().get("CTR-2029")
    act = next(
        a for a in record.certificate_watch_follow_throughs
        if a["state"] == "renewal_chased"
    )
    return date.fromisoformat(act["at"][:10])


# ---------------------------------------------------------------------------
# The ageing reading itself
# ---------------------------------------------------------------------------


def _acts(*stamps: tuple[str, str, str]) -> list[dict]:
    return [
        {"state": state, "actor": actor, "at": at}
        for state, actor, at in stamps
    ]


def test_ageing_reads_an_open_watch_as_ageless():
    ageing = follow_through_ageing([], date(2026, 10, 10))
    assert ageing["state"] == "open"
    assert ageing["age_days"] is None
    assert ageing["stale"] is False


def test_ageing_measures_the_chase_and_its_threshold():
    acts = _acts(("renewal_chased", "Counsel — A. Reyes", "2026-10-01T09:00:00+00:00"))
    fresh = follow_through_ageing(acts, date(2026, 10, 20))
    assert fresh["age_days"] == 19
    assert fresh["stale"] is False
    assert fresh["chased_by"] == "Counsel — A. Reyes"
    # The threshold is a crossing, not an approach: at the
    # threshold day the chase is stale.
    boundary = follow_through_ageing(acts, date(2026, 10, 31))
    assert boundary["age_days"] == 30
    assert boundary["stale"] is True
    # The threshold is tunable per reading.
    assert follow_through_ageing(acts, date(2026, 10, 31), 45)["stale"] is False


def test_an_accepted_risk_never_goes_stale():
    acts = _acts(
        ("renewal_chased", "Counsel — A. Reyes", "2026-08-01T09:00:00+00:00"),
        ("risk_accepted", "Counsel — A. Reyes", "2026-08-02T09:00:00+00:00"),
    )
    ageing = follow_through_ageing(acts, date(2026, 12, 31))
    assert ageing["state"] == "risk_accepted"
    assert ageing["stale"] is False


# ---------------------------------------------------------------------------
# The watch rows and the queue
# ---------------------------------------------------------------------------


def test_a_fresh_chase_ages_but_is_not_stale(samples):
    svc = _service(samples)
    _chase(svc)
    view = svc.certificate_watches(today=_act_day(svc))
    (row,) = view["certificates"]
    assert row["follow_through_ageing"]["age_days"] == 0
    assert row["follow_through_ageing"]["stale"] is False
    assert view["stale_follow_through_count"] == 0
    assert svc.stale_certificate_watch_follow_throughs(today=_act_day(svc)) == []


def test_a_quiet_chase_reads_stale_and_resurfaces_on_the_queue(samples):
    svc = _service(samples)
    _chase(svc)
    later = _act_day(svc) + timedelta(days=45)
    view = svc.certificate_watches(today=later)
    (row,) = view["certificates"]
    ageing = row["follow_through_ageing"]
    assert ageing["age_days"] == 45
    assert ageing["stale"] is True
    assert view["stale_follow_through_count"] == 1
    (alert,) = svc.stale_certificate_watch_follow_throughs(today=later)
    assert alert["contract_id"] == "CTR-2029"
    assert alert["chased_by"] == "Counsel — A. Reyes"
    assert alert["age_days"] == 45
    assert alert["expiry_date"] == "2026-12-31"
    # The single-watch reading carries the ageing too.
    reading = svc.certificate_watch("CTR-2029", today=later)
    assert reading["follow_through_ageing"]["stale"] is True


def test_an_accepted_risk_does_not_resurface(samples):
    svc = _service(samples)
    _chase(svc)
    svc.record_certificate_watch_follow_through(
        "CTR-2029", "risk_accepted", "Counsel — A. Reyes",
        note="Cover lapses for two weeks mid-term; the exposure is priced.",
        today=TODAY,
    )
    later = _act_day(svc) + timedelta(days=90)
    assert svc.stale_certificate_watch_follow_throughs(today=later) == []
    view = svc.certificate_watches(today=later)
    (row,) = view["certificates"]
    assert row["follow_through_ageing"]["stale"] is False


def test_the_queue_payload_carries_the_stale_chases(samples):
    from contract_agent.api import app

    client = TestClient(app)
    res = client.post(
        "/contracts/analyze", json=samples["CTR-2029"].model_dump(mode="json")
    )
    assert res.status_code == 200
    res = client.get("/queue")
    assert res.status_code == 200
    body = res.json()
    # The surface exists on the payload; the frozen-clock
    # assertions live at the service level above (the API reads
    # the real clock, and a chase recorded now is fresh).
    assert body["stale_watch_follow_throughs"] == []
