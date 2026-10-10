"""Severance arithmetic (round 12): an offer letter's severance
promise is a formula — months of base salary — while the
compensation schedule attached to the same letter often states
the severance as a lump sum, sometimes drafted against last
year's salary. The check computes the formula against the salary
basis the package states, in code, and compares it with the
figure the package states: agreement is ``consistent``, a
disagreement is a ``mismatch``, and a formula whose salary basis
is stated nowhere — so the stated figure cannot be computed or
checked — is ``missing_in_exhibit``. A formula alone, with no
numbers anywhere, generates no check.
"""

from __future__ import annotations

import pytest

from contract_agent.exhibit_review import build_exhibit_checks
from contract_agent.extractor import extract_clauses
from contract_agent.samples import sample_contract_models
from contract_agent.segmentation import segment_contract
from contract_agent.schemas import ContractInput, DocumentInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore
from helpers import contract


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


def _checks(result, name: str):
    return [c for c in result.exhibit_checks if c.name == name]


# ---------------------------------------------------------------------------
# The samples: the schedule that agrees, and the one drafted stale
# ---------------------------------------------------------------------------


def test_ctr2034_schedule_figure_is_the_formula_computed(samples):
    result = _service().analyze(samples["CTR-2034"])
    check = _checks(result, "severance_arithmetic")
    assert len(check) == 1
    assert check[0].status == "consistent"
    assert check[0].promised == "severance of 3 months of base salary"
    assert "$46,500" in check[0].found
    assert "severance_arithmetic" not in result.facts.exhibit_mismatches


def test_ctr2035_schedule_drafted_against_an_old_salary_mismatches(samples):
    result = _service().analyze(samples["CTR-2035"])
    check = _checks(result, "severance_arithmetic")
    assert len(check) == 1
    assert check[0].status == "mismatch"
    assert "$80,000" in check[0].found  # 4 months at $240,000 per annum
    assert "$60,000" in check[0].found  # what the schedule states
    assert "$20,000" in check[0].detail  # the disagreement, named
    assert "severance_arithmetic" in result.facts.exhibit_mismatches


# ---------------------------------------------------------------------------
# The family only fires when there is arithmetic to do
# ---------------------------------------------------------------------------


def test_formula_without_any_numbers_generates_no_check(samples):
    # CTR-2025/2033 promise three months of base salary but state
    # neither a salary basis nor a lump sum anywhere — a formula
    # with no numbers is not arithmetic yet, and their exhibit
    # counts stand as the prior rounds recorded them.
    for cid, count in (("CTR-2025", 1), ("CTR-2033", 2)):
        result = _service().analyze(samples[cid])
        assert _checks(result, "severance_arithmetic") == []
        assert len(result.exhibit_checks) == count


def test_msa_without_employment_terms_generates_nothing(samples):
    result = _service().analyze(samples["CTR-2001"])
    assert _checks(result, "severance_arithmetic") == []


# ---------------------------------------------------------------------------
# Unit level: the computation's branches
# ---------------------------------------------------------------------------


def _checks_for(text: str, documents=()):
    model = contract(text, doc_type="employment", documents=[
        DocumentInput(doc_type="agreement", document_id="AGR-1", raw_text=text),
        *documents,
    ])
    units = segment_contract(model)
    extractions = extract_clauses(units, model)
    return build_exhibit_checks(model, units, extractions)


_LETTER = (
    "1. Employment Terms\n"
    "The Employee's employment is at will and may be terminated by either "
    "party on thirty (30) days' written notice. If the Company terminates "
    "the employment without cause, the Employee will receive severance of "
    "two (2) months of base salary. The Employee's base salary is "
    "$120,000 per annum.\n"
)


def test_formula_and_basis_without_a_stated_figure_reports_the_computation():
    checks = _checks_for(_LETTER)
    by_name = {c.name: c for c in checks}
    check = by_name["severance_arithmetic"]
    assert check.status == "consistent"
    assert check.found == "computed $20,000"  # 2 months at $120,000 per annum
    assert "computed in code" in check.detail


def test_stated_figure_without_a_salary_basis_is_missing_in_exhibit():
    schedule = DocumentInput(
        doc_type="schedule",
        document_id="SCHED-C",
        raw_text=(
            "Compensation Schedule\n\n"
            "Severance on termination without cause: $30,000.\n"
        ),
    )
    letter = _LETTER.replace(
        " The Employee's base salary is $120,000 per annum.", ""
    )
    checks = _checks_for(letter, [schedule])
    by_name = {c.name: c for c in checks}
    check = by_name["severance_arithmetic"]
    assert check.status == "missing_in_exhibit"
    assert "$30,000" in check.found
    assert "stated nowhere" in check.detail


def test_weeks_formula_is_computed_against_the_basis():
    letter = (
        "1. Employment Terms\n"
        "The Employee's employment is at will and may be terminated by "
        "either party on thirty (30) days' written notice. If the Company "
        "terminates the employment without cause, the Employee will "
        "receive severance of eight (8) weeks of base salary. The "
        "Employee's base salary is $130,000 per annum.\n"
    )
    schedule = DocumentInput(
        doc_type="schedule",
        document_id="SCHED-C",
        raw_text=(
            "Compensation Schedule\n\n"
            "Severance on termination without cause: $20,000.\n"
        ),
    )
    checks = _checks_for(letter, [schedule])
    by_name = {c.name: c for c in checks}
    check = by_name["severance_arithmetic"]
    # 8 weeks at $130,000 per annum = 8 x $2,500 = $20,000.
    assert check.status == "consistent"
    assert "8 weeks of base salary" in check.promised


def test_salary_stated_in_a_severance_sentence_is_the_basis_not_the_amount():
    # One sentence carrying both the formula and the salary must
    # not be misread as stating a lump sum equal to the salary.
    letter = (
        "1. Employment Terms\n"
        "The Employee's employment is at will and may be terminated by "
        "either party on thirty (30) days' written notice. On "
        "termination without cause the Employee will receive severance "
        "of three (3) months of base salary, the Employee's base "
        "salary being $96,000 per annum.\n"
    )
    checks = _checks_for(letter)
    by_name = {c.name: c for c in checks}
    check = by_name["severance_arithmetic"]
    assert check.status == "consistent"
    assert check.found == "computed $24,000"  # 3 months at $96,000 per annum
