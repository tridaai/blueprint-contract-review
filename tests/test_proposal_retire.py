"""Proposal retire path (round 10): activation used to be a one-way
door — a proposal counsel activated stayed in the playbook forever,
however the world moved. Retirement closes the lifecycle:
proposed → activated → retired, by counsel, with a reason on the
record. The entries leave the active playbook and retrieval at
once; the proposal and its effectiveness record stay, and the
effectiveness view gains a post-retirement window so the coverage
gap the entry was closing re-opens honestly instead of vanishing
with the entry."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from contract_agent import api
from contract_agent.playbook_registry import (
    active_entry_by_id,
    active_playbook,
    clear_registered,
)
from contract_agent.proposal_effectiveness import proposal_effectiveness
from contract_agent.proposal_store import InMemoryProposalStore
from contract_agent.schemas import (
    AgentResult,
    ClauseVerdict,
    PlaybookProposal,
    PlaybookProposalEntry,
)
from contract_agent.service import ContractService
from contract_agent.store import ApprovalRecord, InMemoryStore


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registered()
    yield
    clear_registered()


def _service(**kwargs) -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False, **kwargs)


def _proposal(**overrides) -> PlaybookProposal:
    base = {
        "proposal_id": "PROP-RETIRE",
        "clause_type": "confidentiality_breach",
        "status": "proposed",
        "entries": [
            PlaybookProposalEntry(
                id="PROP-RETIRE-01",
                clause_type="confidentiality_breach",
                title="Confidentiality breach — fallback (PROPOSED)",
                fallback_language="Notify within 72 hours.",
            ),
        ],
    }
    base.update(overrides)
    return PlaybookProposal(**base)


def _activated_service() -> tuple[ContractService, InMemoryProposalStore]:
    store = InMemoryProposalStore()
    store.save(_proposal())
    svc = _service(proposal_store=store)
    svc.activate_playbook_proposal("PROP-RETIRE", actor="C. Ounsel")
    return svc, store


# ---------------------------------------------------------------------------
# The lifecycle: activate → retire, with the reason on record
# ---------------------------------------------------------------------------


def test_retire_closes_the_lifecycle_and_stamps_the_reason():
    svc, store = _activated_service()
    assert active_entry_by_id("PROP-RETIRE-01") is not None
    retired = svc.retire_playbook_proposal(
        "PROP-RETIRE",
        actor="S. Ensey",
        reason="The shipped playbook now covers this family directly.",
    )
    assert retired.status == "retired"
    assert retired.retired_by == "S. Ensey"
    assert retired.retired_at
    assert retired.retire_reason.startswith("The shipped playbook")
    # The activation record is untouched — retirement adds to the
    # lifecycle; it does not rewrite it.
    assert retired.decided_by == "C. Ounsel"
    assert retired.decided_at
    assert store.get("PROP-RETIRE").status == "retired"


def test_retirement_drops_the_entries_from_the_active_playbook():
    svc, _store = _activated_service()
    assert any(e["id"] == "PROP-RETIRE-01" for e in active_playbook())
    svc.retire_playbook_proposal("PROP-RETIRE", actor="S. Ensey", reason="superseded")
    assert active_entry_by_id("PROP-RETIRE-01") is None
    assert not any(e["id"] == "PROP-RETIRE-01" for e in active_playbook())


def test_retirement_requires_a_reason_and_an_activated_proposal():
    svc, _store = _activated_service()
    with pytest.raises(ValueError, match="requires a reason"):
        svc.retire_playbook_proposal("PROP-RETIRE", actor="S. Ensey", reason="  ")
    svc.retire_playbook_proposal("PROP-RETIRE", actor="S. Ensey", reason="done")
    with pytest.raises(ValueError, match="only an activated proposal"):
        svc.retire_playbook_proposal("PROP-RETIRE", actor="S. Ensey", reason="again")
    with pytest.raises(KeyError):
        svc.retire_playbook_proposal("PROP-NOPE", actor="S. Ensey", reason="?")


def test_a_proposed_proposal_cannot_be_retired():
    store = InMemoryProposalStore()
    store.save(_proposal())
    svc = _service(proposal_store=store)
    with pytest.raises(ValueError, match="only an activated proposal"):
        svc.retire_playbook_proposal("PROP-RETIRE", actor="S. Ensey", reason="early")


def test_retired_entries_do_not_come_back_on_a_fresh_service():
    svc, store = _activated_service()
    svc.retire_playbook_proposal("PROP-RETIRE", actor="S. Ensey", reason="superseded")
    clear_registered()  # a fresh process starts from the store alone
    _service(proposal_store=store)._get_proposal_store()
    assert active_entry_by_id("PROP-RETIRE-01") is None


# ---------------------------------------------------------------------------
# Effectiveness: the live window is preserved, the gap re-opens in the open
# ---------------------------------------------------------------------------


def _record(cid: str, verdicts: list[ClauseVerdict], analyzed_at: str) -> ApprovalRecord:
    return ApprovalRecord(
        result=AgentResult(contract_id=cid, clause_verdicts=verdicts),
        analyzed_at=analyzed_at,
    )


def _verdict(refs, position="review_required", fallback="some language"):
    return ClauseVerdict(
        clause_id="C-1",
        clause_type="confidentiality_breach",
        position=position,
        playbook_refs=refs,
        fallback_language=fallback,
    )


def test_effectiveness_splits_at_retirement_and_names_the_reopened_gap():
    proposal = _proposal(
        status="retired",
        decided_by="C. Ounsel",
        decided_at="2026-10-01T00:00:00+00:00",
        retired_by="S. Ensey",
        retired_at="2026-10-10T00:00:00+00:00",
        retire_reason="superseded",
    )
    records = [
        # Live window: one verdict issued under the entry.
        _record("CTR-LIVE", [_verdict(["PROP-RETIRE-01"])], "2026-10-05T00:00:00+00:00"),
        # After retirement: the type arrives flagged with no fallback
        # language again — the gap the activation had closed.
        _record("CTR-AFTER", [_verdict(["PB-GEN-01"], fallback="")], "2026-10-12T00:00:00+00:00"),
    ]
    view = proposal_effectiveness(proposal, records)
    assert view["active"] is False
    assert view["retired"] is True
    assert view["retired_by"] == "S. Ensey"
    assert view["retire_reason"] == "superseded"
    assert view["runs_since_activation"] == 1
    entries = {e["entry_id"]: e for e in view["entries"]}
    assert entries["PROP-RETIRE-01"]["verdicts"] == 1
    post = view["post_retirement"]
    assert post["runs"] == 1
    assert post["flagged_without_fallback"] == 1
    assert post["gap_reopened"] is True


def test_effectiveness_post_retirement_quiet_when_the_gap_stays_closed():
    proposal = _proposal(
        status="retired",
        decided_at="2026-10-01T00:00:00+00:00",
        retired_at="2026-10-10T00:00:00+00:00",
        retire_reason="shipped coverage landed",
    )
    records = [
        _record(
            "CTR-AFTER",
            [_verdict(["PB-CON-09"], position="preferred")],
            "2026-10-12T00:00:00+00:00",
        )
    ]
    view = proposal_effectiveness(proposal, records)
    assert view["post_retirement"]["gap_reopened"] is False
    assert view["post_retirement"]["flagged_for_type"] == 0


# ---------------------------------------------------------------------------
# API: the endpoint enforces actor + reason, the list keeps the record
# ---------------------------------------------------------------------------


def test_retire_endpoint(monkeypatch):
    store = InMemoryProposalStore()
    store.save(_proposal())
    svc = _service(proposal_store=store)
    svc.activate_playbook_proposal("PROP-RETIRE", actor="C. Ounsel")
    monkeypatch.setattr(api, "service", svc)
    client = TestClient(api.app, raise_server_exceptions=False)
    missing_actor = client.post(
        "/playbook/proposals/PROP-RETIRE/retire", json={"note": "superseded"}
    )
    assert missing_actor.status_code == 422
    missing_reason = client.post(
        "/playbook/proposals/PROP-RETIRE/retire", json={"actor": "S. Ensey"}
    )
    assert missing_reason.status_code == 422
    done = client.post(
        "/playbook/proposals/PROP-RETIRE/retire",
        json={"actor": "S. Ensey", "note": "superseded by the shipped family"},
    )
    assert done.status_code == 200
    body = done.json()
    assert body["status"] == "retired"
    assert body["retire_reason"] == "superseded by the shipped family"
    listing = client.get("/playbook/proposals").json()
    row = next(p for p in listing["proposals"] if p["proposal_id"] == "PROP-RETIRE")
    assert row["status"] == "retired"
    assert row["effectiveness"]["retired"] is True
    assert row["effectiveness"]["post_retirement"] is not None
