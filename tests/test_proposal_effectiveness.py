"""Proposal effectiveness (round 9): activation used to be where
the record went quiet. Now the proposal record reports what its
entries actually did — verdicts issued under them since activation,
and whether the coverage gap they were activated to close recurred.
The view is computed from the stored records, in code, and it is
honest in both directions: a structure-only activation that gave
verdicts no language shows the gap still recurring."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from contract_agent.api import app
from contract_agent.playbook_registry import clear_registered
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
from helpers import CLEAN, contract

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


def _proposal(**overrides) -> PlaybookProposal:
    base = {
        "proposal_id": "PROP-TEST",
        "clause_type": "confidentiality_breach",
        "status": "activated",
        "decided_by": "C. Ounsel",
        "decided_at": "2026-10-01T00:00:00+00:00",
        "entries": [
            PlaybookProposalEntry(id="PROP-TEST-01", clause_type="confidentiality_breach"),
            PlaybookProposalEntry(id="PROP-TEST-02", clause_type="confidentiality_breach"),
        ],
    }
    base.update(overrides)
    return PlaybookProposal(**base)


def _record(cid: str, verdicts: list[ClauseVerdict], analyzed_at: str) -> ApprovalRecord:
    return ApprovalRecord(
        result=AgentResult(contract_id=cid, clause_verdicts=verdicts),
        analyzed_at=analyzed_at,
    )


def _verdict(refs, position="review_required", fallback="some language", ctype="confidentiality_breach"):
    return ClauseVerdict(
        clause_id="C-1",
        clause_type=ctype,
        position=position,
        playbook_refs=refs,
        fallback_language=fallback,
    )


# ---------------------------------------------------------------------------
# The computation: window, citations, the recurring gap
# ---------------------------------------------------------------------------


def test_effectiveness_counts_only_the_post_activation_window():
    proposal = _proposal()
    records = [
        # Before activation: cited and flagged — outside the window.
        _record("CTR-BEFORE", [_verdict(["PROP-TEST-01"])], "2026-09-01T00:00:00+00:00"),
        # After activation: one citing verdict with language, one
        # flagged verdict for the type still carrying no language.
        _record(
            "CTR-AFTER",
            [
                _verdict(["PB-CON-01", "PROP-TEST-02"], position="acceptable_fallback"),
                _verdict(["PB-GEN-01"], fallback=""),
            ],
            "2026-10-05T00:00:00+00:00",
        ),
        # A verdict for another clause type: not this proposal's story.
        _record(
            "CTR-OTHER",
            [_verdict(["PB-LIA-01"], ctype="liability_cap")],
            "2026-10-06T00:00:00+00:00",
        ),
        # An unstamped legacy record: counted as unstamped, never
        # silently backdated into the window.
        _record("CTR-LEGACY", [_verdict(["PROP-TEST-01"])], ""),
    ]
    view = proposal_effectiveness(proposal, records)
    assert view["active"] is True
    assert view["runs_since_activation"] == 2
    assert view["unstamped_runs"] == 1
    entries = {e["entry_id"]: e for e in view["entries"]}
    assert entries["PROP-TEST-01"]["verdicts"] == 0
    assert entries["PROP-TEST-02"]["verdicts"] == 1
    assert entries["PROP-TEST-02"]["positions"] == {"acceptable_fallback": 1}
    assert view["verdicts_for_type"] == 2
    assert view["verdicts_citing_entries"] == 1
    assert view["flagged_for_type"] == 2
    assert view["flagged_without_fallback_since"] == 1
    assert view["gap_recurring"] is True


def test_effectiveness_for_a_proposal_never_activated():
    view = proposal_effectiveness(_proposal(status="proposed", decided_at=""), [])
    assert view["active"] is False
    assert view["status"] == "proposed"
    assert view["runs_since_activation"] == 0
    assert view["gap_recurring"] is False


# ---------------------------------------------------------------------------
# Service level: the gap flow, backdated runs, and the honest negative
# ---------------------------------------------------------------------------


def test_service_effectiveness_after_activation_with_backdated_runs():
    svc = _service(proposal_store=InMemoryProposalStore())
    svc.analyze(contract(_WITH_FM, contract_id="CTR-GAP-1"))
    svc.analyze(contract(_WITH_FM, contract_id="CTR-GAP-2"))
    (proposal,) = svc.generate_playbook_proposals()
    svc.activate_playbook_proposal(proposal.proposal_id, actor="C. Ounsel")
    # The two gap runs predate the activation (simulate the real
    # ordering: they were analysed days before counsel decided).
    store = svc._get_store()
    for cid in ("CTR-GAP-1", "CTR-GAP-2"):
        record = store.get(cid)
        record.analyzed_at = "2026-09-01T00:00:00+00:00"
        store.save(record)
    svc.analyze(contract(_WITH_FM, contract_id="CTR-GAP-3"))
    view = svc.proposal_effectiveness(proposal.proposal_id)
    assert view["runs_since_activation"] == 1
    assert view["verdicts_for_type"] == 1
    # The "other" proposal is structure-only — activation gave the
    # verdicts no language, and the view says the gap recurred
    # rather than dressing the activation up as a success.
    assert view["flagged_without_fallback_since"] == 1
    assert view["gap_recurring"] is True
    metrics = svc.metrics()
    (row,) = metrics["proposal_effectiveness"]
    assert row["proposal_id"] == proposal.proposal_id
    assert row["gap_recurring"] is True
    with pytest.raises(KeyError):
        svc.proposal_effectiveness("PROP-NOPE")


def test_language_carrying_activation_closes_the_gap_in_the_view():
    # Fabricated records over a real activated proposal: verdicts
    # citing the entries, carrying language — the gap stays closed.
    store = InMemoryProposalStore()
    svc = _service(proposal_store=store)
    proposal = _proposal()
    store.save(proposal)
    records = [
        _record(
            "CTR-NEW",
            [_verdict(["PROP-TEST-01"], position="acceptable_fallback")],
            "2026-10-05T00:00:00+00:00",
        )
    ]
    view = proposal_effectiveness(proposal, records)
    assert view["verdicts_citing_entries"] == 1
    assert view["flagged_without_fallback_since"] == 0
    assert view["gap_recurring"] is False


# ---------------------------------------------------------------------------
# API: the record carries its effectiveness; the endpoint serves it
# ---------------------------------------------------------------------------


def test_effectiveness_api():
    from contract_agent.api import service as app_service

    client = TestClient(app)
    missing = client.get("/playbook/proposals/PROP-NOPE/effectiveness")
    assert missing.status_code == 404
    # Seed an activated proposal directly in the app service's
    # proposal store — deliberately NOT the generated "other"
    # proposal, whose lifecycle another test owns end to end.
    seeded = _proposal(proposal_id="PROP-EFF-API")
    app_service._get_proposal_store().save(seeded)
    res = client.get("/playbook/proposals/PROP-EFF-API/effectiveness")
    assert res.status_code == 200
    body = res.json()
    assert body["active"] is True
    assert body["proposal_id"] == "PROP-EFF-API"
    assert {e["entry_id"] for e in body["entries"]} == {
        "PROP-TEST-01",
        "PROP-TEST-02",
    }
    # The proposal record itself carries the view once activated.
    listing = client.get("/playbook/proposals").json()
    row = next(
        p for p in listing["proposals"] if p["proposal_id"] == "PROP-EFF-API"
    )
    assert row["effectiveness"]["proposal_id"] == "PROP-EFF-API"


# ---------------------------------------------------------------------------
# The baseline: before vs since — did the gap STOP recurring?
# ---------------------------------------------------------------------------


def test_effectiveness_baseline_gap_stopped_recurring():
    proposal = _proposal()
    records = [
        # Before activation the gap was real: two verdicts for the
        # type, both flagged, both with no fallback language.
        _record(
            "CTR-B1",
            [_verdict(["PB-CON-09"], fallback="")],
            "2026-09-01T00:00:00+00:00",
        ),
        _record(
            "CTR-B2",
            [_verdict(["PB-CON-09"], fallback="")],
            "2026-09-15T00:00:00+00:00",
        ),
        # Since activation the type still arrives — but the
        # verdicts cite the new entries and carry language.
        _record(
            "CTR-A1",
            [_verdict(["PROP-TEST-01"], position="acceptable_fallback")],
            "2026-10-05T00:00:00+00:00",
        ),
        _record(
            "CTR-A2",
            [_verdict(["PROP-TEST-02"], position="preferred")],
            "2026-10-06T00:00:00+00:00",
        ),
    ]
    view = proposal_effectiveness(proposal, records)
    assert view["before_activation"] == {
        "runs": 2,
        "verdicts_for_type": 2,
        "flagged_for_type": 2,
        "flagged_without_fallback": 2,
    }
    assert view["runs_since_activation"] == 2
    assert view["verdicts_citing_entries"] == 2
    assert view["flagged_without_fallback_since"] == 0
    assert view["gap_recurring"] is False
    # Before > 0 and since == 0: the gap stopped, and the view
    # credits the activation window with exactly that.
    assert view["gap_stopped_recurring"] is True


def test_effectiveness_no_credit_when_there_was_no_gap_before():
    # No flagged-without-fallback verdicts before the stamp and
    # none since: nothing recurred, but nothing STOPPED either —
    # the activation is not credited with closing a gap that
    # never existed.
    proposal = _proposal()
    records = [
        _record(
            "CTR-B1",
            [_verdict(["PB-CON-01"], position="preferred")],
            "2026-09-01T00:00:00+00:00",
        ),
        _record(
            "CTR-A1",
            [_verdict(["PROP-TEST-01"], position="preferred")],
            "2026-10-05T00:00:00+00:00",
        ),
    ]
    view = proposal_effectiveness(proposal, records)
    assert view["before_activation"]["flagged_without_fallback"] == 0
    assert view["gap_stopped_recurring"] is False
    assert view["gap_recurring"] is False
    # A proposal never activated reports the zeroed baseline too.
    cold = proposal_effectiveness(_proposal(status="proposed", decided_at=""), records)
    assert cold["before_activation"]["runs"] == 0
    assert cold["gap_stopped_recurring"] is False


def test_service_effectiveness_before_window_with_backdated_runs():
    svc = _service(proposal_store=InMemoryProposalStore())
    svc.analyze(contract(_WITH_FM, contract_id="CTR-GAPB-1"))
    svc.analyze(contract(_WITH_FM, contract_id="CTR-GAPB-2"))
    (proposal,) = svc.generate_playbook_proposals()
    svc.activate_playbook_proposal(proposal.proposal_id, actor="C. Ounsel")
    store = svc._get_store()
    for cid in ("CTR-GAPB-1", "CTR-GAPB-2"):
        record = store.get(cid)
        record.analyzed_at = "2026-09-01T00:00:00+00:00"
        store.save(record)
    svc.analyze(contract(_WITH_FM, contract_id="CTR-GAPB-3"))
    view = svc.proposal_effectiveness(proposal.proposal_id)
    # The two runs the activation was drafted from are the
    # baseline: both carried the gap the proposal exists to close.
    assert view["before_activation"]["runs"] == 2
    assert view["before_activation"]["verdicts_for_type"] == 2
    assert view["before_activation"]["flagged_without_fallback"] == 2
    # And the structure-only activation still does not take: the
    # gap recurs since, so it has not stopped.
    assert view["flagged_without_fallback_since"] == 1
    assert view["gap_recurring"] is True
    assert view["gap_stopped_recurring"] is False
    (row,) = svc.metrics()["proposal_effectiveness"]
    assert row["before_activation"]["flagged_without_fallback"] == 2
