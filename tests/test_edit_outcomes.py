"""Per-edit acceptance tracking (round 6): counsel marks each
proposed edit accepted / modified / rejected on the record; the
follow-up round's reflection check reads the agreed edits and flags
the ones the new draft does not show — agreed, but not reflected."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from contract_agent.api import app
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import EditOutcomeInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore, _dump_record, _load_record
from helpers import contract

# Round 1: an unlimited liability clause (walk_away) and net-90
# payment terms (review_required, tier-1 fallback proposes net 45).
_R1 = """1. Limitation of Liability
The Provider's liability under this Agreement shall be unlimited and shall not be limited in any way.

2. Payment Terms
The Customer shall pay all invoices within ninety (90) days of the invoice date.
"""

# Round 2 carrying the agreed net-45 sentence (the operative term of
# the fallback; its interest rider is a second proposition).
_R2_ADOPTED = """1. Limitation of Liability
The Provider's liability under this Agreement shall be unlimited and shall not be limited in any way.

2. Payment Terms
Undisputed invoices are payable within forty-five (45) days of the invoice date.
"""


@pytest.fixture(scope="module")
def samples() -> dict:
    return {c.contract_id: c for c in sample_contract_models()}


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


def _round1(svc: ContractService):
    return svc.analyze(contract(_R1, contract_id="CTR-EDIT-1"))


def _edit_index(result, clause_type: str) -> int:
    return next(
        i for i, e in enumerate(result.proposed_edits) if e.clause_type == clause_type
    )


# ---------------------------------------------------------------------------
# Recording outcomes
# ---------------------------------------------------------------------------


def test_outcomes_are_recorded_with_snapshots_and_actor():
    svc = _service()
    r1 = _round1(svc)
    pay = _edit_index(r1, "payment_terms")
    liab = _edit_index(r1, "liability_cap")
    result = svc.record_edit_outcomes(
        "CTR-EDIT-1",
        [
            EditOutcomeInput(edit_index=pay, outcome="accepted", note="agreed on the call"),
            EditOutcomeInput(edit_index=liab, outcome="rejected", note="they would not move"),
        ],
        actor="C. Ounsel",
    )
    outcomes = {o.edit_index: o for o in result.edit_outcomes}
    assert outcomes[pay].outcome == "accepted"
    assert outcomes[pay].note == "agreed on the call"
    assert outcomes[pay].clause_type == "payment_terms"
    assert outcomes[pay].clause_id == r1.proposed_edits[pay].clause_id
    assert outcomes[pay].kind == r1.proposed_edits[pay].kind
    assert outcomes[pay].marked_by == "C. Ounsel"
    assert outcomes[pay].marked_at
    assert outcomes[liab].outcome == "rejected"
    # And they survive the record serialisation round-trip.
    record = svc._get_store().get("CTR-EDIT-1")
    restored = _load_record(_dump_record(record))
    assert [o.outcome for o in restored.result.edit_outcomes] == ["rejected", "accepted"]


def test_remarking_an_edit_replaces_its_outcome():
    svc = _service()
    r1 = _round1(svc)
    pay = _edit_index(r1, "payment_terms")
    svc.record_edit_outcomes(
        "CTR-EDIT-1", [EditOutcomeInput(edit_index=pay, outcome="rejected")], actor="A"
    )
    result = svc.record_edit_outcomes(
        "CTR-EDIT-1",
        [EditOutcomeInput(edit_index=pay, outcome="modified", modified_text="Net 60, settled.")],
        actor="B",
    )
    assert len(result.edit_outcomes) == 1
    assert result.edit_outcomes[0].outcome == "modified"
    assert result.edit_outcomes[0].modified_text == "Net 60, settled."
    assert result.edit_outcomes[0].marked_by == "B"


def test_outcome_validation_names_the_problem():
    svc = _service()
    _round1(svc)
    with pytest.raises(KeyError):
        svc.record_edit_outcomes(
            "CTR-9999", [EditOutcomeInput(edit_index=0, outcome="accepted")], actor="A"
        )
    with pytest.raises(ValueError, match="out of range"):
        svc.record_edit_outcomes(
            "CTR-EDIT-1", [EditOutcomeInput(edit_index=99, outcome="accepted")], actor="A"
        )
    with pytest.raises(ValueError, match="must be one of"):
        svc.record_edit_outcomes(
            "CTR-EDIT-1", [EditOutcomeInput(edit_index=0, outcome="shrugged")], actor="A"
        )


def test_approve_can_carry_outcomes_and_they_reach_the_record():
    svc = _service()
    r1 = _round1(svc)
    pay = _edit_index(r1, "payment_terms")
    result = svc.approve(
        "CTR-EDIT-1",
        approver="C. Ounsel",
        edit_outcomes=[EditOutcomeInput(edit_index=pay, outcome="accepted")],
    )
    assert result.status == "approved"
    assert [o.outcome for o in result.edit_outcomes] == ["accepted"]


# ---------------------------------------------------------------------------
# Reflection: agreed edits checked against the next draft
# ---------------------------------------------------------------------------


def _two_rounds(r2_text: str, outcome: EditOutcomeInput | None = None):
    svc = _service()
    r1 = _round1(svc)
    svc.record_edit_outcomes(
        "CTR-EDIT-1",
        [outcome or EditOutcomeInput(
            edit_index=_edit_index(r1, "payment_terms"), outcome="accepted"
        )],
        actor="C. Ounsel",
    )
    r2 = svc.analyze(contract(r2_text, contract_id="CTR-EDIT-2", supersedes="CTR-EDIT-1"))
    return svc, r2


def test_agreed_language_that_landed_is_reflected():
    _, r2 = _two_rounds(_R2_ADOPTED)
    assert len(r2.edit_reflection) == 1
    entry = r2.edit_reflection[0]
    assert entry.clause_type == "payment_terms"
    assert entry.status == "reflected"
    assert entry.coverage == 0.5  # the net-45 sentence landed; the rider did not
    assert entry.clause_id == "C-02"


def test_agreed_language_missing_from_the_draft_is_flagged():
    _, r2 = _two_rounds(_R1)  # round 2 repeats round 1 verbatim
    entry = r2.edit_reflection[0]
    assert entry.status == "agreed_not_reflected"
    assert entry.coverage == 0.0
    assert "Agreed, but not reflected" in entry.detail


def test_rejected_edits_are_not_checked():
    svc = _service()
    r1 = _round1(svc)
    svc.record_edit_outcomes(
        "CTR-EDIT-1",
        [EditOutcomeInput(edit_index=_edit_index(r1, "liability_cap"), outcome="rejected")],
        actor="C. Ounsel",
    )
    r2 = svc.analyze(contract(_R1, contract_id="CTR-EDIT-2", supersedes="CTR-EDIT-1"))
    assert r2.edit_reflection == []


def test_modified_outcome_checks_the_settled_language():
    svc = _service()
    r1 = _round1(svc)
    svc.record_edit_outcomes(
        "CTR-EDIT-1",
        [EditOutcomeInput(
            edit_index=_edit_index(r1, "payment_terms"),
            outcome="modified",
            modified_text=(
                "The Customer shall pay all undisputed invoices within "
                "sixty (60) days of the invoice date."
            ),
            note="settled at net 60",
        )],
        actor="C. Ounsel",
    )
    r2_text = _R1.replace("pay all invoices", "pay all undisputed invoices").replace(
        "ninety (90)", "sixty (60)"
    )
    r2 = svc.analyze(contract(r2_text, contract_id="CTR-EDIT-2", supersedes="CTR-EDIT-1"))
    entry = r2.edit_reflection[0]
    assert entry.outcome == "modified"
    assert entry.status == "reflected"
    assert entry.coverage == 1.0


def test_agreed_edit_whose_clause_disappeared_is_flagged():
    r2_text = """1. Limitation of Liability
The Provider's liability under this Agreement shall be unlimited and shall not be limited in any way.
"""
    _, r2 = _two_rounds(r2_text)
    entry = r2.edit_reflection[0]
    assert entry.status == "agreed_not_reflected"
    assert entry.clause_id == ""
    assert "absent from the new round's draft" in entry.detail


def test_outcomes_marked_after_the_followup_refresh_its_reflection():
    svc = _service()
    r1 = _round1(svc)
    r2 = svc.analyze(
        contract(_R2_ADOPTED, contract_id="CTR-EDIT-2", supersedes="CTR-EDIT-1")
    )
    assert r2.edit_reflection == []  # no outcomes existed at analysis time
    svc.record_edit_outcomes(
        "CTR-EDIT-1",
        [EditOutcomeInput(edit_index=_edit_index(r1, "payment_terms"), outcome="accepted")],
        actor="C. Ounsel",
    )
    refreshed = svc.get("CTR-EDIT-2")
    assert len(refreshed.edit_reflection) == 1
    assert refreshed.edit_reflection[0].status == "reflected"


def test_vertex_chain_reflection_against_real_samples(samples):
    svc = _service()
    r14 = svc.analyze(samples["CTR-2014"])
    gov = _edit_index(r14, "governing_law")
    svc.record_edit_outcomes(
        "CTR-2014",
        [EditOutcomeInput(edit_index=gov, outcome="accepted", note="NY law agreed")],
        actor="C. Ounsel",
    )
    r15 = svc.analyze(samples["CTR-2015"])
    entry = next(e for e in r15.edit_reflection if e.clause_type == "governing_law")
    # The agreed language proposed New York law; CTR-2015's draft
    # kept Delaware. The shared boilerplate must not inflate the
    # check — the change itself never landed.
    assert entry.status == "agreed_not_reflected"


# ---------------------------------------------------------------------------
# Surfaces: brief + API
# ---------------------------------------------------------------------------


def test_brief_shows_outcomes_and_reflection(samples):
    svc = _service()
    r14 = svc.analyze(samples["CTR-2014"])
    gov = _edit_index(r14, "governing_law")
    svc.record_edit_outcomes(
        "CTR-2014",
        [EditOutcomeInput(edit_index=gov, outcome="accepted", note="NY law agreed")],
        actor="C. Ounsel",
    )
    svc.analyze(samples["CTR-2015"])
    md = svc.negotiation_brief("CTR-2015")["markdown"]
    assert "## Edit outcomes and reflection" in md
    assert "Outcomes recorded on CTR-2014" in md
    assert "governing_law" in md
    assert "AGREED BUT NOT REFLECTED" in md
    # A brief with no outcomes on record says so, plainly.
    svc2 = _service()
    svc2.analyze(samples["CTR-2001"])
    md2 = svc2.negotiation_brief("CTR-2001")["markdown"]
    assert "No per-edit outcomes are on record yet" in md2


@pytest.fixture()
def client():
    return TestClient(app)


def test_edits_endpoint_round_trips(client, samples):
    payload = samples["CTR-2014"].model_dump(mode="json")
    assert client.post("/contracts/analyze", json=payload).status_code == 200
    result = client.get("/contracts/CTR-2014").json()
    gov = next(
        i for i, e in enumerate(result["proposed_edits"]) if e["clause_type"] == "governing_law"
    )
    res = client.post(
        "/contracts/CTR-2014/edits",
        json={"actor": "C. Ounsel", "outcomes": [{"edit_index": gov, "outcome": "accepted", "note": "NY law agreed"}]},
    )
    assert res.status_code == 200
    outcomes = res.json()["edit_outcomes"]
    assert outcomes[0]["outcome"] == "accepted"
    assert outcomes[0]["marked_by"] == "C. Ounsel"
    # Validation: no actor / no outcomes / bad index / unknown contract.
    assert client.post(
        "/contracts/CTR-2014/edits", json={"outcomes": [{"edit_index": 0, "outcome": "accepted"}]}
    ).status_code == 422
    assert client.post(
        "/contracts/CTR-2014/edits", json={"actor": "C. Ounsel", "outcomes": []}
    ).status_code == 422
    assert client.post(
        "/contracts/CTR-2014/edits",
        json={"actor": "C. Ounsel", "outcomes": [{"edit_index": 99, "outcome": "accepted"}]},
    ).status_code == 422
    assert client.post(
        "/contracts/CTR-9999/edits",
        json={"actor": "C. Ounsel", "outcomes": [{"edit_index": 0, "outcome": "accepted"}]},
    ).status_code == 404


def test_approve_endpoint_carries_edit_outcomes(client, samples):
    payload = samples["CTR-2014"].model_dump(mode="json")
    assert client.post("/contracts/analyze", json=payload).status_code == 200
    result = client.get("/contracts/CTR-2014").json()
    assert result["proposed_edits"], "CTR-2014 must propose edits for this test"
    res = client.post(
        "/contracts/CTR-2014/approve",
        json={
            "actor": "C. Ounsel",
            "edit_outcomes": [{"edit_index": 0, "outcome": "accepted"}],
        },
    )
    assert res.status_code == 200
    assert res.json()["status"] == "approved"
    assert res.json()["edit_outcomes"][0]["outcome"] == "accepted"
