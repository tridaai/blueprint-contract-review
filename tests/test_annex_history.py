"""Annex history across the whole chain (round 15): the drift
checks are pairwise — the latest round against the one it
supersedes — which is the right shape for a finding and the
wrong shape for a biography. The annex history walks the whole
chain the way the renewal composition does: each annex family's
timeline in one view — what every round's list held, what each
step changed, which additions landed inside the promised
notice window, and when an invention left the schedule (a
removal is a transfer, and WHEN it happened is part of the
fact). Computed with the drift modules' own readings, so the
biography and the findings cannot disagree.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from contract_agent.annex_history import build_annex_history
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


# ---------------------------------------------------------------------------
# Sub-processor chains (CTR-2031 rounds, the drift tests' idiom)
# ---------------------------------------------------------------------------

_ANNEX_V1 = (
    "Annex 1 — Sub-processor List\n\n"
    "The Processor engages the following sub-processors in the processing under this Addendum:\n"
    "- Hostline GmbH (Germany) — data hosting\n"
    "- Postmark Systems Ltd (Ireland) — email delivery\n"
    "The Processor shall give the Controller at least thirty (30) days' written notice before any change to this list takes effect.\n"
)
_ANNEX_V2 = _ANNEX_V1.replace(
    "- Postmark Systems Ltd (Ireland) — email delivery\n",
    "- Postmark Systems Ltd (Ireland) — email delivery\n"
    "- Cloudmetrics Inc (United States) — product analytics\n",
)
_ANNEX_V3 = _ANNEX_V2.replace(
    "- Postmark Systems Ltd (Ireland) — email delivery\n", ""
).replace("Hostline GmbH (Germany)", "Hostline GmbH (Netherlands)")


def _dpa_round(samples, contract_id: str, annex: str | None,
               supersedes: str = "", received_at: str = "") -> ContractInput:
    data = samples["CTR-2031"].model_dump(mode="json")
    data["contract_id"] = contract_id
    data["supersedes"] = supersedes
    data["received_at"] = received_at
    docs = []
    for doc in data["documents"]:
        if doc["document_id"] == "ANNEX-1":
            if annex is None:
                continue  # the round carries no list annex at all
            doc = dict(doc, raw_text=annex)
        docs.append(doc)
    data["documents"] = docs
    return ContractInput.model_validate(data)


def _dpa_chain(samples) -> list[ContractInput]:
    return [
        _dpa_round(samples, "CTR-AH-1", _ANNEX_V1, received_at="2026-08-01"),
        _dpa_round(samples, "CTR-AH-2", _ANNEX_V2, supersedes="CTR-AH-1",
                   received_at="2026-08-10"),
        _dpa_round(samples, "CTR-AH-3", _ANNEX_V3, supersedes="CTR-AH-2",
                   received_at="2026-10-01"),
    ]


def test_subprocessor_timeline_reads_every_round_and_step(samples):
    history = build_annex_history(_dpa_chain(samples))
    assert history["chain"] == ["CTR-AH-1", "CTR-AH-2", "CTR-AH-3"]
    fam = history["subprocessor"]
    assert fam is not None
    assert [row["count"] for row in fam["rounds"]] == [2, 3, 2]
    step1, step2 = fam["steps"]
    # Step 1: an addition nine days after the prior round —
    # inside the thirty days' notice the clause promises.
    assert step1["added"] == ["Cloudmetrics Inc"]
    assert step1["notice"]["promised_days"] == 30
    assert step1["notice"]["gap_days"] == 9
    assert step1["notice"]["within_window"] is True
    # Step 2: a removal and a move, no addition, no clock to read.
    assert step2["removed"] == ["Postmark Systems Ltd"]
    assert step2["moved"] == [
        {"name": "Hostline GmbH", "from_location": "Germany",
         "to_location": "Netherlands"}
    ]
    assert step2["notice"] is None
    summary = " ".join(fam["summary"])
    assert "gained 1 processor(s) (Cloudmetrics Inc)" in summary
    assert "dropped 1 (Postmark Systems Ltd)" in summary
    assert "inside the promised notice window" in summary
    # No employment schedule anywhere in this chain.
    assert history["prior_inventions"] is None


def test_an_addition_outside_the_window_is_no_violation(samples):
    chain = _dpa_chain(samples)
    chain[1] = _dpa_round(samples, "CTR-AH-2", _ANNEX_V2,
                          supersedes="CTR-AH-1", received_at="2026-09-15")
    fam = build_annex_history(chain)["subprocessor"]
    assert fam["steps"][0]["notice"]["gap_days"] == 45
    assert fam["steps"][0]["notice"]["within_window"] is False
    assert "notice window" not in " ".join(fam["summary"])


def test_a_round_without_the_annex_is_recorded_absent(samples):
    chain = _dpa_chain(samples)
    chain[1] = _dpa_round(samples, "CTR-AH-2", None, supersedes="CTR-AH-1",
                          received_at="2026-08-10")
    fam = build_annex_history(chain)["subprocessor"]
    assert fam["rounds"][1]["present"] is False
    assert "No sub-processor list annex in CTR-AH-2" in fam["steps"][0]["summary"]
    assert fam["steps"][0]["added"] == []


# ---------------------------------------------------------------------------
# Prior-invention chains (CTR-2033 rounds, the drift tests' idiom)
# ---------------------------------------------------------------------------

_SCHEDULE = (
    "Schedule of Prior Inventions\n\n"
    "The Employee records the following inventions, conceived before the "
    "employment, as the Employee's prior inventions:\n"
    "- RouteWeaver — a route-planning prototype for regional delivery fleets\n"
    "- Ledgerline — a bookkeeping reconciliation tool for small practices\n"
)


def _pi_round(samples, contract_id: str, schedule_text: str | None,
              supersedes: str) -> ContractInput:
    data = samples["CTR-2033"].model_dump(mode="json")
    data["contract_id"] = contract_id
    data["supersedes"] = supersedes
    docs = []
    for doc in data["documents"]:
        if doc["document_id"] == "SCHED-A":
            if schedule_text is None:
                continue  # the schedule document is gone entirely
            doc = dict(doc, raw_text=schedule_text)
        docs.append(doc)
    data["documents"] = docs
    return ContractInput.model_validate(data)


def test_prior_invention_timeline_names_each_transfer(samples):
    grown = _SCHEDULE + "- OrchardLens — an orchard survey annotation tool\n"
    shrunk = grown.replace(
        "- Ledgerline — a bookkeeping reconciliation tool for small practices\n",
        "",
    )
    chain = [
        samples["CTR-2033"],
        _pi_round(samples, "CTR-AH-P2", grown, supersedes="CTR-2033"),
        _pi_round(samples, "CTR-AH-P3", shrunk, supersedes="CTR-AH-P2"),
    ]
    history = build_annex_history(chain)
    fam = history["prior_inventions"]
    assert fam is not None
    assert [row["count"] for row in fam["rounds"]] == [2, 3, 2]
    step1, step2 = fam["steps"]
    assert step1["added"] == ["OrchardLens"]
    assert step1["removal_is_transfer"] is False
    # The removal is read as what it is: the assignment excludes
    # what the schedule lists, so Ledgerline fell into it here.
    assert step2["removed"] == ["Ledgerline"]
    assert step2["removal_is_transfer"] is True
    assert "a transfer" in step2["summary"]
    summary = " ".join(fam["summary"])
    assert "Removals the assignment reads as transfers: Ledgerline" in summary
    assert history["subprocessor"] is None


def test_a_vanished_schedule_is_the_loudest_step(samples):
    chain = [
        samples["CTR-2033"],
        _pi_round(samples, "CTR-AH-P9", None, supersedes="CTR-2033"),
    ]
    fam = build_annex_history(chain)["prior_inventions"]
    step = fam["steps"][0]
    assert step["schedule_vanished"] is True
    assert set(step["removed"]) == {"RouteWeaver", "Ledgerline"}
    assert step["removal_is_transfer"] is True
    assert "the schedule document itself is gone" in step["summary"]


# ---------------------------------------------------------------------------
# Service + API: the chain as the store holds it
# ---------------------------------------------------------------------------


def test_service_walks_the_stored_chain(samples):
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    for contract in _dpa_chain(samples):
        svc.analyze(contract)
    history = svc.annex_history("CTR-AH-3")
    assert history["rounds"] == 3
    assert history["subprocessor"]["rounds"][-1]["contract_id"] == "CTR-AH-3"
    # The middle of the chain reads its own shorter biography.
    assert svc.annex_history("CTR-AH-2")["rounds"] == 2
    with pytest.raises(KeyError):
        svc.annex_history("CTR-NOPE")


def test_annex_history_endpoint(samples):
    from contract_agent.api import app

    client = TestClient(app)
    for contract in _dpa_chain(samples):
        res = client.post(
            "/contracts/analyze", json=contract.model_dump(mode="json")
        )
        assert res.status_code == 200
    res = client.get("/contracts/CTR-AH-3/annex-history")
    assert res.status_code == 200
    body = res.json()
    assert body["chain"] == ["CTR-AH-1", "CTR-AH-2", "CTR-AH-3"]
    assert body["subprocessor"]["steps"][0]["notice"]["within_window"] is True
    assert client.get("/contracts/CTR-UNKNOWN/annex-history").status_code == 404


def test_a_single_round_has_a_history_of_one(samples):
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(samples["CTR-2001"])
    history = svc.annex_history("CTR-2001")
    assert history["rounds"] == 1
    assert history["subprocessor"] is None
    assert history["prior_inventions"] is None
