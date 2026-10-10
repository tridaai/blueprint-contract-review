"""Negotiation brief export (round 5): one counsel-facing document
per chain, assembled in code from the stored records and exported
through the object-store port."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from contract_agent.api import app
from contract_agent.objectstore import InMemoryObjectStore
from contract_agent.samples import sample_contract_models
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore


@pytest.fixture(scope="module")
def samples() -> dict:
    return {c.contract_id: c for c in sample_contract_models()}


def _service(objects: InMemoryObjectStore | None = None) -> ContractService:
    return ContractService(
        store=InMemoryStore(), checkpointer=False, object_store=objects
    )


def _chain_service(samples) -> ContractService:
    svc = _service()
    svc.analyze(samples["CTR-2014"])
    svc.analyze(samples["CTR-2015"])
    svc.analyze(samples["CTR-2016"])
    return svc


def test_brief_covers_the_whole_chain(samples):
    svc = _chain_service(samples)
    result = svc.get("CTR-2016")
    brief = svc.negotiation_brief("CTR-2016")
    md = brief["markdown"]
    assert brief["key"] == "contracts/CTR-2016/negotiation-brief.md"
    assert "DRAFT — FOR COUNSEL REVIEW" in md
    assert "CTR-2014 → CTR-2015 → CTR-2016" in md
    # Figures in the brief are the stored result's own numbers.
    assert result.facts.posture in md
    assert f"{result.round_chain.re_trades}" in md
    assert "Re-trades:" in md  # the chain has re-trades; the brief says so
    for clause_type in result.round_chain.re_traded_clause_types:
        assert clause_type in md
    # Every section the brief promises is present.
    for heading in (
        "## Negotiation chain",
        "## Open positions by tier",
        "## Proposed edits (draft for counsel review)",
        "## Exhibit findings",
        "## Obligations register (current round)",
    ):
        assert heading in md
    # Open positions carry their tier language.
    tier_two = [d for d in result.clause_diffs if d.proposed_tier == 2]
    assert tier_two, "the chain's final round should propose tier 2 somewhere"
    for diff in tier_two:
        assert (diff.tier_language or diff.playbook_fallback)[:40] in md


def test_brief_is_exported_through_the_object_store(samples):
    objects = InMemoryObjectStore()
    svc = _service(objects)
    svc.analyze(samples["CTR-2001"])
    brief = svc.negotiation_brief("CTR-2001")
    stored = objects.get("contracts/CTR-2001/negotiation-brief.md")
    assert stored is not None
    assert stored.decode("utf-8") == brief["markdown"]


def test_single_round_brief_says_there_is_no_movement(samples):
    svc = _service()
    result = svc.analyze(samples["CTR-2017"])
    md = svc.negotiation_brief("CTR-2017")["markdown"]
    assert "A single round is on record" in md
    # The exhibit findings of the current round are in the brief.
    assert "breach_notice_window" in md
    assert "mismatch" in md
    assert result.facts.posture in md


def test_brief_unknown_contract_raises(samples):
    svc = _service()
    with pytest.raises(KeyError):
        svc.negotiation_brief("CTR-9999")


@pytest.fixture()
def client():
    return TestClient(app)


def test_brief_download_endpoint(client, samples):
    payload = samples["CTR-2001"].model_dump(mode="json")
    assert client.post("/contracts/analyze", json=payload).status_code == 200
    res = client.get("/contracts/CTR-2001/brief")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/markdown")
    assert "attachment" in res.headers["content-disposition"]
    assert "CTR-2001-negotiation-brief.md" in res.headers["content-disposition"]
    assert res.headers["x-object-key"] == "contracts/CTR-2001/negotiation-brief.md"
    assert "Negotiation brief — CTR-2001" in res.text
    assert client.get("/contracts/CTR-9999/brief").status_code == 404
