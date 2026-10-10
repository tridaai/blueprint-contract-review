"""Acting on calibration candidates (round 11): round 10's
calibration report flags clause types whose pattern term never
decides — and proposes nothing more. Counsel can now close the
loop: suspend the term for a type (actor + reason on the record,
reversible by a recorded lift). While suspended, the scorer does
not apply the term to the type, and the calibration report reads
the type as suspended instead of re-flagging it every run.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from contract_agent.api import app
from contract_agent.calibration_store import InMemoryCalibrationStore
from contract_agent.options import PATTERN_TERM_CAP
from contract_agent.pattern_calibration import pattern_calibration
from contract_agent.schemas import (
    AgentResult,
    ClauseOption,
    ClauseVerdict,
    EditOutcomeInput,
)
from contract_agent.service import ContractService
from contract_agent.store import ApprovalRecord, InMemoryStore
from helpers import contract


# ---------------------------------------------------------------------------
# The acts themselves: validation, stamps, reversibility
# ---------------------------------------------------------------------------


def _service() -> ContractService:
    return ContractService(
        store=InMemoryStore(),
        checkpointer=False,
        calibration_store=InMemoryCalibrationStore(),
    )


def test_suspend_requires_a_reason():
    svc = _service()
    with pytest.raises(ValueError, match="requires a reason"):
        svc.suspend_pattern_term("payment_terms", actor="C. Ounsel", reason="  ")


def test_suspend_stamps_actor_reason_and_time():
    svc = _service()
    suspension = svc.suspend_pattern_term(
        "payment_terms", actor="C. Ounsel",
        reason="Six points never moved a recommendation in four chains.",
    )
    assert suspension.status == "active"
    assert suspension.suspended_by == "C. Ounsel"
    assert suspension.suspended_at
    assert suspension.reason.startswith("Six points")
    assert svc.pattern_suspensions() == [suspension]


def test_double_suspension_is_refused_until_lifted():
    svc = _service()
    svc.suspend_pattern_term("payment_terms", actor="C. Ounsel", reason="r1")
    with pytest.raises(ValueError, match="already suspended"):
        svc.suspend_pattern_term("payment_terms", actor="D. Uo", reason="r2")


def test_lift_reverses_and_keeps_both_acts_on_the_record():
    svc = _service()
    svc.suspend_pattern_term("payment_terms", actor="C. Ounsel", reason="r1")
    lifted = svc.lift_pattern_suspension(
        "payment_terms", actor="D. Uo", note="The pattern recurred with teeth."
    )
    assert lifted.status == "lifted"
    assert lifted.suspended_by == "C. Ounsel"  # the act it reverses stays readable
    assert lifted.lifted_by == "D. Uo"
    assert lifted.lift_note == "The pattern recurred with teeth."
    # And the type can be suspended again on fresh evidence.
    again = svc.suspend_pattern_term("payment_terms", actor="D. Uo", reason="r3")
    assert again.status == "active"
    assert again.lifted_by == ""


def test_lift_without_a_suspension_is_a_key_error():
    svc = _service()
    with pytest.raises(KeyError):
        svc.lift_pattern_suspension("payment_terms", actor="C. Ounsel")


# ---------------------------------------------------------------------------
# The report: suspended reads as suspended, never re-flagged
# ---------------------------------------------------------------------------


def _option(clause_id, score, pattern=0.0, recommended=False) -> ClauseOption:
    return ClauseOption(
        clause_id=clause_id,
        kind="escalate_to_senior_counsel",
        score=score,
        breakdown={"position": score - pattern, "pattern": pattern},
        recommended=recommended,
    )


def _record(cid: str) -> ApprovalRecord:
    # Two applications of the term for payment_terms, neither
    # decisive (the escalations it rode on were not recommended):
    # the textbook calibration candidate.
    return ApprovalRecord(
        result=AgentResult(
            contract_id=cid,
            options=[
                _option("C-2", 88.0, pattern=PATTERN_TERM_CAP),
                _option("C-2", 88.0, pattern=PATTERN_TERM_CAP),
            ],
            clause_verdicts=[
                ClauseVerdict(
                    clause_id="C-2", clause_type="payment_terms",
                    position="review_required",
                )
            ],
        )
    )


def test_candidate_is_flagged_until_suspended_then_read_as_suspended():
    records = [_record("CTR-SUS-1")]
    report = pattern_calibration(records)
    assert report["calibration_candidates"] == ["payment_terms"]

    svc = _service()
    suspension = svc.suspend_pattern_term(
        "payment_terms", actor="C. Ounsel", reason="Never decisive."
    )
    report = pattern_calibration(records, suspensions=[suspension])
    assert report["calibration_candidates"] == []
    assert report["suspended_types"] == ["payment_terms"]
    row = next(r for r in report["clause_types"] if r["clause_type"] == "payment_terms")
    assert row["calibration_candidate"] is False
    assert row["suspended"] is True
    assert row["suspension"]["suspended_by"] == "C. Ounsel"
    assert row["applications"] == 2  # the facts the act was taken on stand
    assert "SUSPENDED" in row["detail"]


def test_suspension_of_an_unseen_type_still_gets_a_row():
    svc = _service()
    suspension = svc.suspend_pattern_term(
        "data_protection", actor="C. Ounsel", reason="Pre-emptive set-aside."
    )
    report = pattern_calibration([], suspensions=[suspension])
    row = next(r for r in report["clause_types"] if r["clause_type"] == "data_protection")
    assert row["applications"] == 0
    assert row["suspended"] is True


def test_metrics_headline_carries_suspended_types():
    svc = _service()
    svc.suspend_pattern_term("payment_terms", actor="C. Ounsel", reason="r")
    summary = svc.metrics()["pattern_calibration"]
    assert summary["suspended_types"] == ["payment_terms"]


# ---------------------------------------------------------------------------
# Scoring honours the suspension — and the lift restores the term
# ---------------------------------------------------------------------------


_NET90 = """1. Limitation of Liability
Each party's aggregate liability shall not exceed the fees paid in the
twelve (12) months preceding the claim.

2. Payment Terms
The Customer shall pay all invoices within ninety (90) days of the invoice date.
"""


def _agree_payment_edit(svc: ContractService, result) -> None:
    index = next(
        i for i, e in enumerate(result.proposed_edits)
        if e.clause_type == "payment_terms"
    )
    svc.record_edit_outcomes(
        result.contract_id,
        [EditOutcomeInput(edit_index=index, outcome="accepted",
                          note="agreed on the call")],
        actor="C. Ounsel",
    )


def _payment_escalation(result):
    clause_id = next(
        v.clause_id for v in result.clause_verdicts
        if v.clause_type == "payment_terms"
    )
    return next(
        o for o in result.options
        if o.clause_id == clause_id and o.kind == "escalate_to_senior_counsel"
    )


def test_suspended_type_scores_without_the_term_until_lifted():
    svc = _service()
    r1 = svc.analyze(contract(_NET90, contract_id="CTR-SUS-1"))
    _agree_payment_edit(svc, r1)
    r2 = svc.analyze(contract(_NET90, contract_id="CTR-SUS-2", supersedes="CTR-SUS-1"))
    _agree_payment_edit(svc, r2)
    r3 = svc.analyze(contract(_NET90, contract_id="CTR-SUS-3", supersedes="CTR-SUS-2"))
    assert any(p.clause_type == "payment_terms" for p in r3.reflection_patterns)

    svc.suspend_pattern_term(
        "payment_terms", actor="C. Ounsel",
        reason="The term never decided a packet in this chain.",
    )
    r4 = svc.analyze(contract(_NET90, contract_id="CTR-SUS-4", supersedes="CTR-SUS-3"))
    # The pattern is still on the result — what is suspended is the
    # score term, not the chain's memory.
    assert any(p.clause_type == "payment_terms" for p in r4.reflection_patterns)
    assert _payment_escalation(r4).breakdown["pattern"] == 0.0

    svc.lift_pattern_suspension("payment_terms", actor="C. Ounsel")
    r5 = svc.analyze(contract(_NET90, contract_id="CTR-SUS-5", supersedes="CTR-SUS-4"))
    assert _payment_escalation(r5).breakdown["pattern"] == PATTERN_TERM_CAP


# ---------------------------------------------------------------------------
# The API: actor + reason enforced, the acts on the record
# ---------------------------------------------------------------------------


client = TestClient(app)


def test_api_suspend_lift_round_trip():
    suspended = client.post(
        "/options/pattern-calibration/suspend",
        json={"clause_type": "governing_law", "actor": "C. Ounsel",
              "note": "Never decisive."},
    )
    assert suspended.status_code == 200
    assert suspended.json()["status"] == "active"

    listing = client.get("/options/pattern-calibration/suspensions")
    assert listing.status_code == 200
    assert any(
        s["clause_type"] == "governing_law" and s["status"] == "active"
        for s in listing.json()["suspensions"]
    )
    report = client.get("/options/pattern-calibration")
    assert "governing_law" in report.json()["suspended_types"]

    lifted = client.post(
        "/options/pattern-calibration/lift",
        json={"clause_type": "governing_law", "actor": "C. Ounsel"},
    )
    assert lifted.status_code == 200
    assert lifted.json()["status"] == "lifted"
    assert lifted.json()["suspended_by"] == "C. Ounsel"


def test_api_suspend_requires_actor_and_reason():
    no_actor = client.post(
        "/options/pattern-calibration/suspend",
        json={"clause_type": "assignment", "note": "r"},
    )
    assert no_actor.status_code == 422
    no_reason = client.post(
        "/options/pattern-calibration/suspend",
        json={"clause_type": "assignment", "actor": "C. Ounsel"},
    )
    assert no_reason.status_code == 422
    unknown_lift = client.post(
        "/options/pattern-calibration/lift",
        json={"clause_type": "assignment", "actor": "C. Ounsel"},
    )
    assert unknown_lift.status_code == 404
