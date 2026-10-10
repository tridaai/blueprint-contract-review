"""Employment obligations (round 13): the obligations register
learns the employment letter's own machinery. An MSA review has
always left a dated trail — renewals, notice deadlines, payment
terms; an employment review left only the confidentiality tail,
while the letter's operative duties (the termination notice
period, any garden-leave window, the severance promise) sat
outside the register. Now the employment-terms signals build
them: the notice period as a tracked duty, garden leave where
the letter provides it, and the severance as a COMPUTED figure —
the letter's formula multiplied in code against the salary
basis the package states, flagged computed, never a figure
quoted from the letter. The clocks run from termination, so
the entries are undated unless the letter itself states a
termination date — then the severance falls due on it and the
notice deadline is derived from it.
"""

from __future__ import annotations

from datetime import date

import pytest

from contract_agent.graph import run_contract
from contract_agent.model_backends import MockModelBackend
from contract_agent.obligations import build_obligations, due_obligations
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
# The samples: notice tracked, severance computed
# ---------------------------------------------------------------------------


def test_ctr2034_register_carries_notice_and_computed_severance(samples):
    result = _run(samples["CTR-2034"])
    by_kind = {o.kind: o for o in result.obligations}
    notice = by_kind["notice_period"]
    assert notice.days == 30
    assert notice.detail == "30 days' written notice"
    assert notice.source_clause_id == "C-01"
    assert notice.date is None  # the letter states no termination date
    severance = by_kind["severance"]
    # 3 months at $186,000 per annum, computed in code — the same
    # figure the exhibit check verifies against the schedule.
    assert severance.detail.startswith("computed $46,500")
    assert "3 months of base salary" in severance.detail
    assert severance.computed is True
    assert severance.date is None
    assert "without cause" in severance.description
    assert "$46,500" in severance.description


def test_ctr2035_register_computes_against_its_own_basis(samples):
    result = _run(samples["CTR-2035"])
    severance = next(o for o in result.obligations if o.kind == "severance")
    # 4 months at $240,000 per annum — the figure the package's
    # stated $60,000 contradicts (the exhibit check's mismatch);
    # the register carries the computed number, not the stated one.
    assert severance.detail.startswith("computed $80,000")


def test_ctr2025_no_salary_basis_no_severance_entry(samples):
    # CTR-2025 promises the same formula but states no salary
    # anywhere: there is nothing to compute, so the register
    # carries the notice duty and no severance figure — an
    # unverifiable promise is not entered as a number.
    result = _run(samples["CTR-2025"])
    kinds = [o.kind for o in result.obligations]
    assert "notice_period" in kinds
    assert "severance" not in kinds


def test_undated_employment_entries_stay_out_of_the_due_view(samples):
    result = _run(samples["CTR-2034"])
    due = due_obligations(result.obligations, date(2026, 10, 10), 3650)
    assert all(o["kind"] != "severance" for o in due)
    assert all(o["kind"] != "notice_period" for o in due)


# ---------------------------------------------------------------------------
# The dated half: a letter that states its termination date
# ---------------------------------------------------------------------------


def test_stated_termination_date_anchors_severance_and_notice():
    units = [
        unit("C-01", (
            "The Employee's employment is at will and may be terminated "
            "by either party at any time, with or without cause, on "
            "thirty (30) days' written notice. If the Company "
            "terminates the employment without cause, the Employee "
            "will receive severance of three (3) months of base salary."
        )),
        unit("C-02", "The Employee's base salary is $120,000 per annum.", ref="2"),
        unit("C-03", (
            "The parties record that the employment terminates on "
            "June 30, 2027, when the programme of work concludes."
        ), ref="3"),
    ]
    extractions = [
        extraction("C-01", "employment_terms",
                   {"notice_days": 30, "severance_months": 3}),
        extraction("C-02", "other", {}),
        extraction("C-03", "other", {}),
    ]
    obligations = build_obligations(units, extractions)
    by_kind = {o.kind: o for o in obligations}
    severance = by_kind["severance"]
    assert severance.detail.startswith("computed $30,000")
    assert severance.date == "2027-06-30"  # quoted from the letter
    notice = by_kind["notice_period"]
    assert notice.date == "2027-05-31"  # derived: termination − 30 days
    assert notice.computed is True
    # And the dated entries join the due view like any other.
    due = due_obligations(obligations, date(2027, 5, 1), 90)
    due_kinds = {o["kind"] for o in due}
    assert {"severance", "notice_period"} <= due_kinds


def test_garden_leave_window_is_registered():
    units = [
        unit("C-01", (
            "Either party may terminate the employment on sixty (60) "
            "days' written notice. During the notice period the "
            "Employee may be placed on garden leave for three (3) "
            "months."
        )),
    ]
    extractions = [
        extraction("C-01", "employment_terms",
                   {"notice_days": 60, "garden_leave_months": 3}),
    ]
    obligations = build_obligations(units, extractions)
    garden = next(o for o in obligations if o.kind == "garden_leave")
    assert garden.detail == "3 months' garden leave"
    assert garden.source_clause_id == "C-01"


def test_garbled_employment_signals_yield_no_entries():
    obligations = build_obligations(
        [unit("C-01", "Employment on some terms.")],
        [extraction("C-01", "employment_terms",
                    {"notice_days": 0, "garden_leave_months": -1,
                     "severance_months": None})],
    )
    assert obligations == []


# ---------------------------------------------------------------------------
# Through the service: the register joins /obligations
# ---------------------------------------------------------------------------


def test_service_result_and_due_surface_carry_employment_entries(samples):
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    result = svc.analyze(samples["CTR-2034"])
    kinds = {o.kind for o in result.obligations}
    assert {"notice_period", "severance"} <= kinds
    view = svc.obligations_due(days=3650)
    # Undated entries exist on the record even though the due
    # view (dated only) does not list them.
    record = svc._get_store().get("CTR-2034")
    assert any(o.kind == "severance" for o in record.result.obligations)
    assert all(
        row["contract_id"] != "CTR-2034" or row["kind"] != "severance"
        for row in view["obligations"]
    )
