"""Covenant biography (round 18): the annexes and the
certificate chain have whole-chain biographies; the
restrictive covenants were the register's remaining pairwise
story — each round's register entry, the lapse a later round
names, the follow-through acts that close it — with no single
view that showed a covenant's whole life. The biography reads
the stored registers and records into one timeline per chain:
each round's covenants and clocks, each step's movement, each
lapse and the state of its loop, and the new-bargain readings
riding on the later rounds. Employment and commercial
covenants alike, since round 18 put both on the register.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from contract_agent.covenant_biography import build_covenant_biography
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def _letter(
    samples,
    contract_id: str,
    *,
    termination: str | None = None,
    supersedes: str = "",
) -> ContractInput:
    """CTR-2025 (both covenants at 12 months) renamed, with an
    optional stated termination date and chain link — the
    lapsed-reading tests' idiom."""
    data = samples["CTR-2025"].model_dump(mode="json")
    data["contract_id"] = contract_id
    data["supersedes"] = supersedes
    if termination:
        for doc in data["documents"]:
            if doc["doc_type"] == "agreement":
                doc["raw_text"] += (
                    "\n\n5. Fixed Term\n"
                    "The parties record that the employment terminates "
                    f"on {termination}, when the programme of work "
                    "concludes.\n"
                )
    return ContractInput.model_validate(data)


def _employment_chain(samples) -> ContractService:
    """Letter A terminated 2024-06-30 — its covenants expired
    2025-06-30 — with the non-compete's lapse followed through
    and confirmed, the non-solicit's left open; letter B
    succeeds it carrying fresh covenants."""
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(_letter(samples, "CTR-BIO-A", termination="June 30, 2024"))
    svc.record_covenant_follow_through(
        "CTR-BIO-A", "non_compete", "counterparty_notified",
        "Counsel — A. Reyes", today=date(2026, 1, 1),
    )
    svc.record_covenant_follow_through(
        "CTR-BIO-A", "non_compete", "restriction_confirmed_lifted",
        "Counsel — A. Reyes", today=date(2026, 1, 2),
    )
    svc.analyze(_letter(samples, "CTR-BIO-B", supersedes="CTR-BIO-A"))
    return svc


# ---------------------------------------------------------------------------
# The employment chain's biography
# ---------------------------------------------------------------------------


def test_biography_reads_every_rounds_covenants_and_clocks(samples):
    bio = _employment_chain(samples).annex_history("CTR-BIO-B")["covenants"]
    assert bio is not None
    assert [r["contract_id"] for r in bio["rounds"]] == ["CTR-BIO-A", "CTR-BIO-B"]
    first = {c["kind"]: c for c in bio["rounds"][0]["covenants"]}
    assert set(first) == {"non_compete", "non_solicit"}
    for covenant in first.values():
        assert covenant["date"] == "2025-06-30"
        assert covenant["lapsed"] is True
    # The two lapses stand in different states, and the
    # biography reads both off the record.
    assert first["non_compete"]["follow_through_state"] == (
        "restriction_confirmed_lifted"
    )
    assert len(first["non_compete"]["follow_through"]) == 2
    assert first["non_solicit"]["follow_through_state"] == "open"
    # The later letter's covenants are undated — its clock
    # never started — and carry no lapse or follow-through.
    second = {c["kind"]: c for c in bio["rounds"][1]["covenants"]}
    for covenant in second.values():
        assert covenant["date"] == ""
        assert covenant["lapsed"] is False
        assert covenant["follow_through_state"] == ""


def test_biography_carries_the_new_bargain_readings(samples):
    bio = _employment_chain(samples).annex_history("CTR-BIO-B")["covenants"]
    readings = bio["rounds"][1]["lapsed_readings"]
    assert len(readings) == 2
    assert all("NEW bargain" in r["detail"] for r in readings)
    assert bio["rounds"][0]["lapsed_readings"] == []


def test_biography_step_narrates_the_lapse_and_the_fresh_covenant(samples):
    bio = _employment_chain(samples).annex_history("CTR-BIO-B")["covenants"]
    (step,) = bio["steps"]
    assert step["from_contract_id"] == "CTR-BIO-A"
    assert step["to_contract_id"] == "CTR-BIO-B"
    # Same length, dated in A and undated in B: the expiry
    # moved — and the summary says what that means beside a
    # lapsed prior covenant.
    assert {m["kind"]: m["change"] for m in step["movements"]} == {
        "non_compete": "expiry_moved",
        "non_solicit": "expiry_moved",
    }
    assert "had lapsed on 2025-06-30" in step["summary"]
    assert "new bargain" in step["summary"]


def test_biography_summary_names_open_and_confirmed_lapses(samples):
    bio = _employment_chain(samples).annex_history("CTR-BIO-B")["covenants"]
    summary = "\n".join(bio["summary"])
    assert "2 covenant(s) have lapsed by today" in summary
    assert "CTR-BIO-A's non-solicitation" in summary  # the open one
    assert "still standing open" in summary
    assert "CTR-BIO-A's non-compete" in summary  # the confirmed one
    assert "2 new-bargain reading(s)" in summary


def test_biography_builder_reads_lapse_as_of_its_own_today(samples):
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(_letter(samples, "CTR-BIO-A", termination="June 30, 2024"))
    chain = [samples["CTR-2025"].model_copy(update={"contract_id": "CTR-BIO-A"})]
    bio = build_covenant_biography(
        chain,
        {"CTR-BIO-A": svc.get("CTR-BIO-A")},
        {},
        today=date(2025, 1, 1),  # before the 2025-06-30 expiry
    )
    (covenant,) = [
        c for c in bio["rounds"][0]["covenants"] if c["kind"] == "non_compete"
    ]
    assert covenant["lapsed"] is False
    assert covenant["follow_through_state"] == "pending_lapse"


# ---------------------------------------------------------------------------
# The commercial chain's biography (CTR-2038 → CTR-2039)
# ---------------------------------------------------------------------------


def _commercial_chain(samples) -> ContractService:
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(samples["CTR-2038"])
    svc.analyze(samples["CTR-2039"])
    return svc


def test_commercial_biography_reads_the_amendment_step(samples):
    bio = _commercial_chain(samples).annex_history("CTR-2039")["covenants"]
    assert bio is not None
    (step,) = bio["steps"]
    movements = {m["kind"]: m["change"] for m in step["movements"]}
    assert movements == {
        "exclusivity": "duration_changed",
        "non_circumvention": "dropped",
    }
    assert "tail changes from 12 months" in step["summary"]
    assert "the non-circumvention covenant is dropped" in step["summary"]
    summary = "\n".join(bio["summary"])
    assert "exclusivity" in summary and "non-circumvention" in summary


def test_biography_is_none_for_a_chain_without_covenants(samples):
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(samples["CTR-2001"])
    assert svc.annex_history("CTR-2001")["covenants"] is None


def test_biography_endpoint(samples):
    from contract_agent.api import app

    client = TestClient(app)
    for cid in ("CTR-2038", "CTR-2039"):
        res = client.post(
            "/contracts/analyze", json=samples[cid].model_dump(mode="json")
        )
        assert res.status_code == 200
    res = client.get("/contracts/CTR-2039/annex-history")
    assert res.status_code == 200
    body = res.json()
    assert [r["contract_id"] for r in body["covenants"]["rounds"]] == [
        "CTR-2038", "CTR-2039",
    ]
