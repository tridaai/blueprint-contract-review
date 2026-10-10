"""Counterparty concession profiles (round 6): clause types a
counterparty concedes vs holds across its negotiation chains,
computed in code from the stored records — and fed to the next
review's assessment as evidence."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from contract_agent.api import app
from contract_agent.concessions import (
    build_concession_profile,
    concession_evidence_lines,
)
from contract_agent.samples import sample_contract_models
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore


@pytest.fixture(scope="module")
def samples() -> dict:
    return {c.contract_id: c for c in sample_contract_models()}


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


def _vertex_service(samples) -> ContractService:
    svc = _service()
    svc.analyze(samples["CTR-2014"])
    svc.analyze(samples["CTR-2015"])
    svc.analyze(samples["CTR-2016"])
    return svc


def test_vertex_profile_counts_chains_retrades_and_clearance(samples):
    svc = _vertex_service(samples)
    profile = svc.counterparty_profile("Vertex Analytics Ltd")
    assert profile["contracts"] == 3
    assert profile["chains"] == 1
    assert profile["multi_round_chains"] == 1
    assert profile["re_trades"] == 2  # indemnity + payment_terms, per the chain
    assert profile["rounds_to_no_walk_away"] == [2]
    assert profile["best_rounds_to_no_walk_away"] == 2
    assert profile["decisions"] == {"approved": 0, "rejected": 0, "awaiting": 3}


def test_vertex_concedes_what_it_conceded_and_holds_what_it_held(samples):
    svc = _vertex_service(samples)
    profile = svc.counterparty_profile("Vertex Analytics Ltd")
    # walk_away -> preferred in round 2 and never given back.
    assert "liability_cap" in profile["concedes"]
    assert "ip_ownership" in profile["concedes"]
    assert "governing_law" in profile["concedes"]
    # Won in round 2, re-traded in round 3: no net movement = held.
    assert "indemnity" in profile["holds"]
    assert "payment_terms" in profile["holds"]
    rows = {r["clause_type"]: r for r in profile["clause_types"]}
    liability = rows["liability_cap"]
    assert liability["first_positions"] == {"walk_away": 1}
    assert liability["final_positions"] == {"preferred": 1}
    assert liability["closes_preferred"] == 1
    indemnity = rows["indemnity"]
    assert indemnity["re_trades"] == 1
    assert indemnity["improved_steps"] == 1
    assert indemnity["worsened_steps"] == 1
    # A type that never opened flagged is uncontested, never a "hold".
    assert rows["confidentiality"]["tendency"] == "uncontested"


def test_decisions_are_counted_from_the_store(samples):
    svc = _vertex_service(samples)
    svc.approve("CTR-2014", approver="C. Ounsel")
    svc.reject("CTR-2015", reviewer="C. Ounsel")
    profile = svc.counterparty_profile("Vertex Analytics Ltd")
    assert profile["decisions"] == {"approved": 1, "rejected": 1, "awaiting": 1}


def test_single_contract_counterparty_has_no_pattern(samples):
    svc = _service()
    svc.analyze(samples["CTR-2001"])  # Northwind, one contract
    profile = svc.counterparty_profile("Northwind Traders Ltd")
    assert profile["contracts"] == 1
    assert profile["multi_round_chains"] == 0
    assert profile["concedes"] == [] and profile["holds"] == []
    assert concession_evidence_lines(profile) == []
    unknown = svc.counterparty_profile("Nobody Industries")
    assert unknown["contracts"] == 0
    assert concession_evidence_lines(unknown) == []


def test_evidence_lines_lead_with_retraded_holds(samples):
    svc = _vertex_service(samples)
    profile = svc.counterparty_profile("Vertex Analytics Ltd")
    lines = concession_evidence_lines(profile)
    assert len(lines) <= 6
    assert lines[0].startswith("concession profile: Vertex Analytics Ltd — ")
    assert "re-trades: 2" in lines[0]
    joined = "\n".join(lines)
    assert "concedes liability_cap" in joined
    assert "holds indemnity" in joined
    assert "re-traded 1×" in joined


def test_profile_feeds_the_next_reviews_assessment_evidence(samples):
    svc = _service()
    svc.analyze(samples["CTR-2014"])
    svc.analyze(samples["CTR-2015"])
    r16 = svc.analyze(samples["CTR-2016"])
    concession_lines = [
        line for line in r16.assessment.evidence if line.startswith("concession profile:")
    ]
    assert concession_lines, "the third round's assessment must cite the profile"
    assert any("Vertex Analytics Ltd" in line for line in concession_lines)
    # At that point indemnity had only ever improved — the profile
    # said so; the re-trade is round 3's own news, not a prior.
    assert any("concedes liability_cap" in line for line in concession_lines)
    # A counterparty with no chain gets no concession evidence.
    fresh = svc.analyze(samples["CTR-2001"])
    assert not any(
        line.startswith("concession profile:") for line in fresh.assessment.evidence
    )


def test_profile_is_per_counterparty(samples):
    svc = _vertex_service(samples)
    svc.analyze(samples["CTR-2001"])
    northwind = svc.counterparty_profile("Northwind Traders Ltd")
    assert northwind["contracts"] == 1
    vertex = svc.counterparty_profile("Vertex Analytics Ltd")
    assert vertex["contracts"] == 3


def test_profile_builder_ignores_other_counterparties_records(samples):
    svc = _vertex_service(samples)
    records = svc._get_store().all_records()
    profile = build_concession_profile("Vertex Analytics Ltd", records)
    assert profile["contracts"] == 3


@pytest.fixture()
def client():
    return TestClient(app)


def test_profile_endpoint(client, samples):
    payload = samples["CTR-2014"].model_dump(mode="json")
    assert client.post("/contracts/analyze", json=payload).status_code == 200
    res = client.get("/counterparties/Vertex%20Analytics%20Ltd/profile")
    assert res.status_code == 200
    body = res.json()
    assert body["counterparty"] == "Vertex Analytics Ltd"
    assert body["contracts"] >= 1
    assert client.get("/counterparties/Nobody%20Industries/profile").status_code == 404
