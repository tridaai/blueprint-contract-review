"""Obligations register tests: extraction in code, date validation."""

from __future__ import annotations

from contract_agent.graph import run_contract
from contract_agent.model_backends import MockModelBackend
from contract_agent.obligations import build_obligations
from contract_agent.retriever import KeywordRetriever
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ClauseExtraction, ClauseUnit


def unit(clause_id: str, text: str, ref: str = "1") -> ClauseUnit:
    return ClauseUnit(clause_id=clause_id, ref=ref, text=text)


def extraction(clause_id: str, clause_type: str, signals: dict) -> ClauseExtraction:
    return ClauseExtraction(clause_id=clause_id, clause_type=clause_type, signals=signals)


def test_renewal_and_notice_deadline_computed_from_explicit_date():
    text = (
        "This Order Form renews automatically on January 1, 2027 for "
        "successive one (1) year terms unless either party gives at "
        "least ninety (90) days' written notice of non-renewal."
    )
    obligations = build_obligations(
        [unit("C-01", text)],
        [extraction("C-01", "term_termination",
                    {"auto_renewal": True, "notice_days": 90})],
    )
    by_kind = {o.kind: o for o in obligations}
    assert by_kind["renewal"].date == "2027-01-01"
    assert by_kind["renewal"].computed is False  # quoted, not derived
    notice = by_kind["notice_period"]
    assert notice.days == 90
    # 2027-01-01 minus 90 days, computed in code and flagged as such.
    assert notice.date == "2026-10-03"
    assert notice.computed is True
    assert notice.source_clause_id == "C-01"


def test_payment_breach_and_confidentiality_entries():
    units = [
        unit("C-01", "Invoices are payable Net 45. Late amounts bear interest."),
        unit("C-02", "Notify the other party of a breach within 72 hours."),
        unit("C-03", "Confidentiality survives for three (3) years."),
    ]
    extractions = [
        extraction("C-01", "payment_terms", {"net_days": 45, "late_fee_pct_monthly": 1.5}),
        extraction("C-02", "data_protection", {"breach_notice_hours": 72}),
        extraction("C-03", "confidentiality", {"duration_years": 3}),
    ]
    obligations = build_obligations(units, extractions)
    kinds = [o.kind for o in obligations]
    assert kinds.count("payment") == 2  # net days + late interest
    payment = next(o for o in obligations if o.detail == "Net 45")
    assert payment.days == 45
    breach = next(o for o in obligations if o.kind == "breach_notice")
    assert breach.hours == 72
    conf = next(o for o in obligations if o.kind == "confidentiality_term")
    assert "3 years" in conf.description


def test_garbled_signals_yield_no_entries():
    # Validation, not trust: zero/negative/wrong-typed counts are not
    # obligations, and a clause with no dated duty registers nothing.
    obligations = build_obligations(
        [unit("C-01", "Pay soon."), unit("C-02", "Terminate somehow.")],
        [
            extraction("C-01", "payment_terms", {"net_days": 0}),
            extraction("C-02", "term_termination", {"notice_days": -5}),
        ],
    )
    assert obligations == []


def test_sample_order_form_register_end_to_end():
    contract = next(
        c for c in sample_contract_models() if c.contract_id == "CTR-2007"
    )
    result = run_contract(
        contract, backend=MockModelBackend(), retriever=KeywordRetriever()
    )
    by_kind = {o.kind: o for o in result.obligations}
    assert "renewal" in by_kind
    assert "successive" in by_kind["renewal"].detail
    assert by_kind["notice_period"].days == 120
    assert by_kind["payment"].days == 45
    assert all(o.source_clause_id for o in result.obligations)


def test_sample_msa_register_end_to_end():
    contract = next(
        c for c in sample_contract_models() if c.contract_id == "CTR-2001"
    )
    result = run_contract(
        contract, backend=MockModelBackend(), retriever=KeywordRetriever()
    )
    by_kind = {o.kind: o for o in result.obligations}
    net = next(o for o in result.obligations if o.detail == "Net 30")
    assert net.days == 30
    assert by_kind["breach_notice"].hours == 48
    assert by_kind["notice_period"].days == 30
    assert by_kind["confidentiality_term"].detail == "3 years following disclosure"
