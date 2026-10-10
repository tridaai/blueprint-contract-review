"""Exhibit consistency beyond the DPA: order forms and statements
of work checked against the agreement's own statements (round 5)."""

from __future__ import annotations

import pytest

from contract_agent.exhibit_review import (
    _acceptance_days,
    _months_near,
    _order_form_total,
)
from contract_agent.samples import sample_contract_models
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore


@pytest.fixture(scope="module")
def samples() -> dict:
    return {c.contract_id: c for c in sample_contract_models()}


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


def _by_name(result) -> dict:
    return {c.name: c for c in result.exhibit_checks}


# ---------------------------------------------------------------------------
# Order forms
# ---------------------------------------------------------------------------


def test_consistent_order_form_passes_all_three_checks(samples):
    result = _service().analyze(samples["CTR-2019"])
    checks = _by_name(result)
    assert set(checks) == {
        "order_form_fees_total",
        "order_form_payment_terms",
        "order_form_term",
    }
    assert all(c.status == "consistent" for c in checks.values())
    assert checks["order_form_fees_total"].found == "USD 96,000"
    assert checks["order_form_fees_total"].exhibit_document_id == "Schedule 1"
    assert result.facts.exhibit_mismatches == []
    assert result.facts.posture == "aligned"


def test_order_form_that_moves_terms_and_term_is_a_mismatch(samples):
    result = _service().analyze(samples["CTR-2020"])
    checks = _by_name(result)
    terms = checks["order_form_payment_terms"]
    assert terms.status == "mismatch"
    assert terms.promised == "Net 30"
    assert terms.found == "Net 60"
    term = checks["order_form_term"]
    assert term.status == "mismatch"
    assert term.promised == "12 months"
    assert term.found == "24 months"
    # The total is honestly stated — that check passes; the failure
    # set names exactly the two contradictions.
    assert checks["order_form_fees_total"].status == "consistent"
    assert result.facts.exhibit_mismatches == [
        "order_form_payment_terms",
        "order_form_term",
    ]
    assert result.facts.posture == "negotiable"


def test_promised_order_form_absent_is_reported_and_requested_by_name(samples):
    contract = samples["CTR-2019"].model_copy(deep=True)
    contract.documents = [d for d in contract.documents if d.doc_type == "agreement"]
    result = _service().analyze(contract)
    checks = _by_name(result)
    assert checks["order_form_fees_total"].status == "exhibit_missing"
    assert result.facts.exhibit_absent is True
    assert result.facts.exhibit_missing_checks == ["order_form_fees_total"]
    assert result.facts.package_complete is False
    assert result.needs_information is True
    items = result.information_request.missing_items
    assert any("order form" in item for item in items)
    # The request names the document that is actually missing — not
    # the DPA, which this package never promised.
    assert not any("Data Processing Addendum" in item for item in items)


def test_order_form_without_a_total_is_missing_the_total(samples):
    contract = samples["CTR-2019"].model_copy(deep=True)
    schedule = next(d for d in contract.documents if d.doc_type == "schedule")
    schedule.raw_text = schedule.raw_text.replace(
        "Total fees for the initial term: USD 96,000.\n", ""
    )
    result = _service().analyze(contract)
    check = _by_name(result)["order_form_fees_total"]
    assert check.status == "missing_in_exhibit"
    assert check.found == "amounts itemised, no total"


def test_silent_order_form_asserts_nothing_and_is_not_checked(samples):
    contract = samples["CTR-2019"].model_copy(deep=True)
    schedule = next(d for d in contract.documents if d.doc_type == "schedule")
    schedule.raw_text = (
        "Schedule 1 — Order Form\n\n"
        "Order Form under the Master Services Agreement between the parties.\n\n"
        "Total fees for the initial term: USD 96,000.\n"
    )
    result = _service().analyze(contract)
    checks = _by_name(result)
    # The total is there; the form restates neither terms nor term,
    # so there is nothing cross-document to contradict.
    assert set(checks) == {"order_form_fees_total"}
    assert checks["order_form_fees_total"].status == "consistent"


# ---------------------------------------------------------------------------
# Statements of work
# ---------------------------------------------------------------------------


def test_consistent_sow_passes_all_three_checks(samples):
    result = _service().analyze(samples["CTR-2021"])
    checks = _by_name(result)
    assert set(checks) == {
        "sow_deliverables",
        "sow_delivery_dates",
        "sow_acceptance_period",
    }
    assert all(c.status == "consistent" for c in checks.values())
    assert checks["sow_delivery_dates"].found == "2026-11-30"
    assert checks["sow_acceptance_period"].promised == "15 days"


def test_sow_without_dates_and_with_a_doubled_acceptance_window(samples):
    result = _service().analyze(samples["CTR-2022"])
    checks = _by_name(result)
    assert checks["sow_deliverables"].status == "consistent"
    assert checks["sow_delivery_dates"].status == "missing_in_exhibit"
    acceptance = checks["sow_acceptance_period"]
    assert acceptance.status == "mismatch"
    assert acceptance.promised == "15 days"
    assert acceptance.found == "30 days"
    assert result.facts.exhibit_mismatches == [
        "sow_acceptance_period",
        "sow_delivery_dates",
    ]


def test_sow_that_mentions_deliverables_without_listing_them(samples):
    contract = samples["CTR-2021"].model_copy(deep=True)
    sow = next(d for d in contract.documents if d.doc_type == "exhibit")
    sow.raw_text = (
        "Exhibit C — Statement of Work\n\n"
        "Statement of Work under the Master Services Agreement between "
        "the parties. The Provider will deliver the deliverables for the "
        "onboarding engagement as discussed. Delivery dates: the "
        "workspace completes by 2026-11-30.\n"
    )
    result = _service().analyze(contract)
    check = _by_name(result)["sow_deliverables"]
    assert check.status == "missing_in_exhibit"
    assert check.found == "deliverables mentioned, no list"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_months_near_reads_years_and_parenthesised_digits():
    text = "The initial term of two (2) years applies. Fees are Net 30."
    assert _months_near(text, "initial term") == 24
    assert _months_near(text, "duration") is None


def test_acceptance_days_reads_only_acceptance_sentences():
    text = (
        "Invoices are payable within 30 days. The Customer shall have "
        "fifteen (15) business days to accept or reject a Deliverable."
    )
    assert _acceptance_days(text) == 15
    assert _acceptance_days("Invoices are payable within 30 days.") is None


def test_order_form_total_needs_a_total_line():
    assert _order_form_total("Fees: USD 100.\nTotal: USD 240.\n") == "USD 240"
    assert _order_form_total("Fees: USD 100 per month.\n") == ""
