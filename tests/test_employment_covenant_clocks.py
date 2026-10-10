"""Restrictive-covenant clocks (round 14): round 13 dated the
employment letter's notice and severance, but the covenant tail
stayed prose — the verdicts measured the non-compete's length
and moved on. Now the durations the covenant reading already
extracts become obligations in the same register, on the same
clock discipline: each covenant's period read from the
sentences that state that covenant (a clause carrying both at
different lengths registers both), undated while termination
is a future event, and — when the letter states its
termination date — expiring at termination + duration,
computed in code and flagged computed, so the dated entries
join the due view and the deadline sweep like any other.
"""

from __future__ import annotations

from datetime import date

import pytest

from contract_agent.graph import run_contract
from contract_agent.model_backends import MockModelBackend
from contract_agent.obligations import _add_months, build_obligations, due_obligations
from contract_agent.retriever import KeywordRetriever
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ClauseExtraction, ClauseUnit, ContractInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def unit(clause_id: str, text: str, ref: str = "1") -> ClauseUnit:
    return ClauseUnit(clause_id=clause_id, ref=ref, text=text)


def extraction(clause_id: str, clause_type: str, signals: dict) -> ClauseExtraction:
    return ClauseExtraction(clause_id=clause_id, clause_type=clause_type, signals=signals)


def _run(contract: ContractInput):
    return run_contract(
        contract, backend=MockModelBackend(), retriever=KeywordRetriever()
    )


# ---------------------------------------------------------------------------
# The samples: both covenants registered, on their own lengths
# ---------------------------------------------------------------------------


def test_ctr2025_covenants_enter_the_register_undated(samples):
    result = _run(samples["CTR-2025"])
    covenants = {o.kind: o for o in result.obligations if o.kind.startswith("non_")}
    assert set(covenants) == {"non_compete", "non_solicit"}
    for entry in covenants.values():
        assert entry.detail == "12 months following termination"
        # The letter states no termination date: the clock has
        # not started, so the entries are undated, like the
        # notice and severance entries beside them.
        assert entry.date is None
        assert entry.computed is False
    # Undated covenants stay out of the due view.
    due = due_obligations(result.obligations, date(2026, 10, 10), 3650)
    assert all(not row["kind"].startswith("non_") for row in due)


def test_ctr2034_covenants_ride_beside_notice_and_severance(samples):
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    result = svc.analyze(samples["CTR-2034"])
    kinds = {o.kind for o in result.obligations}
    assert {"notice_period", "severance", "non_compete", "non_solicit"} <= kinds


# ---------------------------------------------------------------------------
# The dated half: expiry = termination + duration, computed
# ---------------------------------------------------------------------------


def _dated_letter() -> tuple[list[ClauseUnit], list[ClauseExtraction]]:
    units = [
        unit("C-01", (
            "The Employee's employment may be terminated by either "
            "party on thirty (30) days' written notice."
        )),
        unit("C-02", (
            "For eighteen (18) months following the end of the "
            "Employee's employment, the Employee shall not be "
            "employed by a business that competes with the Company. "
            "The Employee also agrees not to solicit the Company's "
            "customers for six (6) months following the end of "
            "employment."
        ), ref="2"),
        unit("C-03", (
            "The parties record that the employment terminates on "
            "June 30, 2027, when the programme of work concludes."
        ), ref="3"),
    ]
    extractions = [
        extraction("C-01", "employment_terms", {"notice_days": 30}),
        extraction("C-02", "non_compete", {"duration_months": 18}),
        extraction("C-03", "other", {}),
    ]
    return units, extractions


def test_covenant_expiries_compute_from_the_stated_termination_date():
    obligations = build_obligations(*_dated_letter())
    by_kind = {o.kind: o for o in obligations}
    # Each covenant keeps its own length: 18 months of compete,
    # 6 of solicit — not the clause's longest period twice.
    compete = by_kind["non_compete"]
    assert compete.detail == "18 months following termination"
    assert compete.date == "2028-12-30"  # 2027-06-30 + 18 months
    assert compete.computed is True
    assert compete.source_clause_id == "C-02"
    solicit = by_kind["non_solicit"]
    assert solicit.detail == "6 months following termination"
    assert solicit.date == "2027-12-30"  # 2027-06-30 + 6 months
    assert solicit.computed is True
    # And the dated covenants join the due view on their dates.
    due = due_obligations(obligations, date(2027, 12, 1), 60)
    assert {row["kind"] for row in due} >= {"non_solicit"}
    due = due_obligations(obligations, date(2028, 12, 1), 60)
    assert {row["kind"] for row in due} >= {"non_compete", "non_solicit"}


def test_non_solicit_only_clause_registers_no_compete_clock():
    units = [
        unit("C-01", "Employment on the terms in this letter."),
        unit("C-02", (
            "The Employee agrees not to solicit the Company's "
            "customers or employees for twenty-four (24) months "
            "following the end of employment."
        ), ref="2"),
    ]
    extractions = [
        extraction("C-01", "employment_terms", {"notice_days": 30}),
        extraction("C-02", "non_compete",
                   {"duration_months": 24, "non_solicit_only": True}),
    ]
    obligations = build_obligations(units, extractions)
    kinds = {o.kind for o in obligations}
    assert "non_solicit" in kinds
    assert "non_compete" not in kinds
    solicit = next(o for o in obligations if o.kind == "non_solicit")
    assert solicit.detail == "24 months following termination"


def test_commercial_covenants_register_since_round_18():
    # Round 14 scoped the covenant pass to employment letters,
    # and this test pinned that scoping. Round 18 reverses it —
    # services agreements carry non-competes too — so the same
    # fixture now registers a commercial non_compete, on the
    # commercial anchor (the agreement's termination), undated
    # because the fixture's agreement states no end of its own.
    # The commercial behaviour is pinned in full in
    # test_commercial_covenants.py.
    units = [
        unit("C-01", (
            "The Contractor shall not compete with the Customer "
            "for twelve (12) months following termination of "
            "this agreement."
        )),
    ]
    extractions = [extraction("C-01", "non_compete", {"duration_months": 12})]
    obligations = build_obligations(units, extractions)
    assert [o.kind for o in obligations] == ["non_compete"]
    assert obligations[0].detail == (
        "12 months following termination of the agreement"
    )
    assert obligations[0].date is None


def test_add_months_clamps_to_the_target_month():
    assert _add_months(date(2027, 1, 31), 1) == date(2027, 2, 28)
    assert _add_months(date(2027, 6, 30), 18) == date(2028, 12, 30)
    assert _add_months(date(2027, 12, 15), 2) == date(2028, 2, 15)
