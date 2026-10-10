"""Coverage-driven playbook proposals (round 7): clause types that
keep arriving flagged with no fallback language become a proposal
queue — candidate entries drafted in code from the nearest covered
family, stored PROPOSED, and live only when counsel activates one.
The agent drafts the paperwork; the playbook stays human-owned."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from contract_agent.api import app
from contract_agent.clause_review import review_clause
from contract_agent.fallback_tiers import walk_away_line
from contract_agent.playbook_proposals import (
    draft_playbook_proposal,
    nearest_covered_type,
)
from contract_agent.playbook_registry import clear_registered, entries_for_type
from contract_agent.proposal_store import InMemoryProposalStore
from contract_agent.schemas import (
    ClauseExtraction,
    ClauseUnit,
    PlaybookProposal,
)
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore
from helpers import CLEAN, contract

# A force-majeure clause matches no playbook family: typed "other",
# flagged review_required, and PB-GEN offers no fallback language —
# the standing coverage gap.
_WITH_FM = CLEAN + """
7. Force Majeure
Neither party shall be liable for delay or failure to perform caused
by events beyond its reasonable control, including acts of God, war,
or embargo.
"""


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registered()
    yield
    clear_registered()


def _service(**kwargs) -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False, **kwargs)


def _gap_service() -> ContractService:
    svc = _service(proposal_store=InMemoryProposalStore())
    svc.analyze(contract(_WITH_FM, contract_id="CTR-GAP-1"))
    svc.analyze(contract(_WITH_FM, contract_id="CTR-GAP-2"))
    return svc


# ---------------------------------------------------------------------------
# Drafting: the nearest-covered rule and the honesty of the scaffold
# ---------------------------------------------------------------------------


def test_nearest_covered_type_by_shared_name_tokens():
    donor, basis = nearest_covered_type("confidentiality_breach")
    assert donor == "confidentiality"
    assert "confidentiality" in basis
    donor, basis = nearest_covered_type("other")
    # No family shares a token with "other": the largest covered
    # family (liability_cap, 3 entries) donates its shape, and the
    # basis says the substance is counsel's to draft.
    assert donor == "liability_cap"
    assert "no covered family shares a name token" in basis


def test_related_donor_carries_its_language_labelled_as_adapted():
    proposal = draft_playbook_proposal(
        "confidentiality_breach", flagged=3, without_fallback=3
    )
    assert proposal.based_on_type == "confidentiality"
    by_id = {e.id: e for e in proposal.entries}
    fallback = by_id["PROP-CONFIDENTIALITY-BREACH-02"]
    assert "five (5) years" in fallback.fallback_language  # PB-CON-01's
    assert "adapted from PB-CON-01" in fallback.text
    walk = by_id["PROP-CONFIDENTIALITY-BREACH-03"]
    assert "Final position on confidentiality" in walk.walk_away_line  # PB-CON-02's


def test_unrelated_donor_carries_structure_only():
    proposal = draft_playbook_proposal("other", flagged=4, without_fallback=4)
    by_id = {e.id: e for e in proposal.entries}
    assert by_id["PROP-OTHER-02"].fallback_language == ""
    assert "no covered family is close enough" in by_id["PROP-OTHER-02"].text
    assert "PROP-OTHER-03" not in by_id  # no walk-away line is invented


# ---------------------------------------------------------------------------
# Generation from the stored runs
# ---------------------------------------------------------------------------


def test_generation_needs_a_repeated_gap_and_is_idempotent():
    svc = _service(proposal_store=InMemoryProposalStore())
    svc.analyze(contract(_WITH_FM, contract_id="CTR-GAP-1"))
    assert svc.generate_playbook_proposals() == []  # one arrival is not a pattern
    svc.analyze(contract(_WITH_FM, contract_id="CTR-GAP-2"))
    created = svc.generate_playbook_proposals()
    assert [p.clause_type for p in created] == ["other"]
    proposal = created[0]
    assert proposal.status == "proposed"
    assert proposal.without_fallback == 2
    assert proposal.example_contract_ids == ["CTR-GAP-1", "CTR-GAP-2"]
    assert svc.generate_playbook_proposals() == []  # never re-drafted


def test_dismissed_gap_is_never_redrafted():
    svc = _gap_service()
    (proposal,) = svc.generate_playbook_proposals()
    svc.dismiss_playbook_proposal(proposal.proposal_id, actor="C. Ounsel")
    assert svc.generate_playbook_proposals() == []
    assert svc.list_playbook_proposals(status="dismissed")[0].decided_by == "C. Ounsel"


# ---------------------------------------------------------------------------
# Activation: counsel's act, and the entries become real
# ---------------------------------------------------------------------------


def test_activation_stamps_the_actor_and_registers_the_entries():
    svc = _gap_service()
    (proposal,) = svc.generate_playbook_proposals()
    assert entries_for_type("other") == [
        e for e in entries_for_type("other") if e["id"].startswith("PB-")
    ]  # nothing registered before activation
    activated = svc.activate_playbook_proposal(
        proposal.proposal_id, actor="C. Ounsel", note="shape accepted"
    )
    assert activated.status == "activated"
    assert activated.decided_by == "C. Ounsel"
    assert activated.decided_at
    ids = {e["id"] for e in entries_for_type("other")}
    assert "PROP-OTHER-01" in ids and "PROP-OTHER-02" in ids
    # A decision is final: no re-activation, no dismissal after.
    with pytest.raises(ValueError):
        svc.activate_playbook_proposal(proposal.proposal_id, actor="C. Ounsel")
    with pytest.raises(ValueError):
        svc.dismiss_playbook_proposal(proposal.proposal_id, actor="C. Ounsel")
    with pytest.raises(KeyError):
        svc.activate_playbook_proposal("PROP-NOPE", actor="C. Ounsel")


def test_activated_proposal_language_reaches_verdicts_and_tiers():
    store = InMemoryProposalStore()
    svc = _service(proposal_store=store)
    # A gap type one token away from a covered family: the draft
    # carries the family's language, so activation has teeth.
    proposal = draft_playbook_proposal(
        "confidentiality_breach", flagged=3, without_fallback=3
    )
    assert proposal.based_on_type == "confidentiality"
    store.save(proposal)
    svc.activate_playbook_proposal(proposal.proposal_id, actor="C. Ounsel")
    unit = ClauseUnit(
        clause_id="C-09",
        ref="9",
        heading="Confidentiality Breach Duties",
        text="The receiving party shall handle confidentiality breaches somehow.",
    )
    extraction = ClauseExtraction(
        clause_id="C-09", clause_type="confidentiality_breach"
    )
    verdict = review_clause(unit, extraction)
    assert "five (5) years" in verdict.fallback_language  # PB-CON-01's, via the proposal
    assert "PROP-CONFIDENTIALITY-BREACH-02" in verdict.playbook_refs
    # The adapted walk-away line is tier-2 material now.
    line = walk_away_line("confidentiality_breach")
    assert line is not None and line[0] == "PROP-CONFIDENTIALITY-BREACH-03"


def test_activations_survive_a_fresh_service_via_the_store():
    store = InMemoryProposalStore()
    svc = _service(proposal_store=store)
    proposal = draft_playbook_proposal("other", flagged=2, without_fallback=2)
    store.save(proposal)
    svc.activate_playbook_proposal(proposal.proposal_id, actor="C. Ounsel")
    clear_registered()  # the "restart": process-local registry is empty
    assert not [e for e in entries_for_type("other") if e["id"].startswith("PROP-")]
    fresh = _service(proposal_store=store)
    fresh.list_playbook_proposals()  # resolving the store re-registers
    assert [e for e in entries_for_type("other") if e["id"].startswith("PROP-")]


def test_proposal_json_round_trip():
    proposal = draft_playbook_proposal("other", flagged=2, without_fallback=2)
    clone = PlaybookProposal.model_validate_json(
        proposal.model_dump_json()
    )
    assert clone == proposal


# ---------------------------------------------------------------------------
# API: the queue, generation, and named decisions
# ---------------------------------------------------------------------------


def test_proposals_api_flow():
    client = TestClient(app)
    payload = contract(_WITH_FM, contract_id="CTR-GAP-API-1").model_dump()
    client.post("/contracts/analyze", json=payload)
    payload = contract(_WITH_FM, contract_id="CTR-GAP-API-2").model_dump()
    client.post("/contracts/analyze", json=payload)
    created = client.post("/playbook/proposals/generate").json()
    assert created["created"] >= 1
    listing = client.get("/playbook/proposals").json()
    other = next(p for p in listing["proposals"] if p["clause_type"] == "other")
    assert other["status"] == "proposed"
    # Activation without a name is refused; with one, it lands.
    refused = client.post(
        f"/playbook/proposals/{other['proposal_id']}/activate", json={}
    )
    assert refused.status_code == 422
    decided = client.post(
        f"/playbook/proposals/{other['proposal_id']}/activate",
        json={"actor": "C. Ounsel"},
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "activated"
    assert decided.json()["decided_by"] == "C. Ounsel"
    again = client.post(
        f"/playbook/proposals/{other['proposal_id']}/dismiss",
        json={"actor": "C. Ounsel"},
    )
    assert again.status_code == 422  # a decided proposal stays decided
    missing = client.post(
        "/playbook/proposals/PROP-NOPE/activate", json={"actor": "C. Ounsel"}
    )
    assert missing.status_code == 404
