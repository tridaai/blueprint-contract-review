"""Matter-scoped concession profiles (round 14): the concession
profile used to be one counterparty, one blend — a counterparty
that concedes a point on its platform renewal and fights it on
a services dispute got a single tendency averaged across both,
and a new review on either matter read priors from the other.
The profile now takes a matter tag: only records carrying it
count, chains threaded within the scope, so each matter keeps
its own pattern. An analysis carrying a matter tag reads its
own matter's profile — a matter without a multi-round history
yet gets no pattern lines, rather than borrowing another
matter's. The all-matters profile stays the default.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from contract_agent.api import app
from contract_agent.concessions import concession_evidence_lines
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def _variant(samples, source_id: str, new_id: str, supersedes: str,
             matter: str) -> ContractInput:
    data = samples[source_id].model_dump(mode="json")
    data["contract_id"] = new_id
    data["supersedes"] = supersedes
    data["matter"] = matter
    return ContractInput.model_validate(data)


def _two_matter_service(samples) -> ContractService:
    """Vertex on two matters with genuinely different behaviour:
    the platform renewal ran the full three rounds (indemnity
    won, then re-traded); the services dispute ran rounds 1 and
    3 only — no improvement round, no re-trades, and
    data_protection never moved."""
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    for source, new_id, supersedes in (
        ("CTR-2014", "CTR-MA-1", ""),
        ("CTR-2015", "CTR-MA-2", "CTR-MA-1"),
        ("CTR-2016", "CTR-MA-3", "CTR-MA-2"),
    ):
        svc.analyze(_variant(samples, source, new_id, supersedes, "Platform renewal"))
    for source, new_id, supersedes in (
        ("CTR-2014", "CTR-MB-1", ""),
        ("CTR-2016", "CTR-MB-2", "CTR-MB-1"),
    ):
        svc.analyze(_variant(samples, source, new_id, supersedes, "Services dispute"))
    return svc


def test_scoped_profiles_diverge_and_the_default_blends(samples):
    svc = _two_matter_service(samples)
    renewal = svc.counterparty_profile("Vertex Analytics Ltd", matter="Platform renewal")
    dispute = svc.counterparty_profile("Vertex Analytics Ltd", matter="Services dispute")
    blended = svc.counterparty_profile("Vertex Analytics Ltd")
    assert renewal["scope"] == "matter" and renewal["matter"] == "Platform renewal"
    assert renewal["contracts"] == 3 and renewal["re_trades"] == 2
    assert dispute["contracts"] == 2 and dispute["re_trades"] == 0
    # data_protection was held on the dispute and never moved on
    # the renewal — each scope says its own truth.
    assert "data_protection" in dispute["holds"]
    assert "data_protection" not in renewal["holds"]
    assert blended["scope"] == "all" and blended["matter"] == ""
    assert blended["contracts"] == 5


def test_scoped_evidence_lines_name_the_matter(samples):
    svc = _two_matter_service(samples)
    profile = svc.counterparty_profile("Vertex Analytics Ltd", matter="Platform renewal")
    lines = concession_evidence_lines(profile)
    assert lines[0].startswith(
        "concession profile: Vertex Analytics Ltd on matter Platform renewal — "
    )
    # The scoped profile's re-trade facts are the renewal's own.
    assert "re-trades: 2" in lines[0]


def test_a_review_reads_its_own_matters_profile(samples):
    svc = _two_matter_service(samples)
    # A fresh round on the renewal matter: priors from the
    # renewal's history, named as such.
    fresh = _variant(samples, "CTR-2014", "CTR-MA-9", "", "Platform renewal")
    result = svc.analyze(fresh)
    lines = [
        line for line in result.assessment.evidence
        if line.startswith("concession profile:")
    ]
    assert lines and "on matter Platform renewal" in lines[0]
    # A matter with no history of its own borrows nothing.
    orphan = _variant(samples, "CTR-2014", "CTR-MC-1", "", "Employment dispute")
    result = svc.analyze(orphan)
    assert not any(
        line.startswith("concession profile:")
        for line in result.assessment.evidence
    )
    # An untagged review keeps the all-matters reading.
    untagged = samples["CTR-2014"].model_copy(deep=True)
    untagged.contract_id = "CTR-MU-1"
    result = svc.analyze(untagged)
    lines = [
        line for line in result.assessment.evidence
        if line.startswith("concession profile:")
    ]
    assert lines and "on matter" not in lines[0]


def test_unknown_matter_is_a_zero_profile_in_scope(samples):
    svc = _two_matter_service(samples)
    profile = svc.counterparty_profile("Vertex Analytics Ltd", matter="No such matter")
    assert profile["contracts"] == 0
    assert concession_evidence_lines(profile) == []


@pytest.fixture()
def client():
    return TestClient(app)


def test_profile_endpoint_takes_a_matter_scope(client, samples):
    for source, new_id, supersedes in (
        ("CTR-2014", "CTR-MA-1", ""),
        ("CTR-2015", "CTR-MA-2", "CTR-MA-1"),
    ):
        payload = _variant(
            samples, source, new_id, supersedes, "Platform renewal"
        ).model_dump(mode="json")
        assert client.post("/contracts/analyze", json=payload).status_code == 200
    res = client.get(
        "/counterparties/Vertex%20Analytics%20Ltd/profile",
        params={"matter": "Platform renewal"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["scope"] == "matter"
    assert body["matter"] == "Platform renewal"
    assert body["contracts"] == 2
    missing = client.get(
        "/counterparties/Vertex%20Analytics%20Ltd/profile",
        params={"matter": "Services dispute"},
    )
    assert missing.status_code == 404
    assert "Services dispute" in missing.json()["detail"]
