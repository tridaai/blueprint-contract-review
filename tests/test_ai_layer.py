"""AI-layer tests: retrieval, assessment, options, tools, resilience,
and the mock drafting templates — all offline and deterministic."""

from __future__ import annotations

from contract_agent.assessment import build_assessment, citations_for
from contract_agent.extractor import extract_clauses
from contract_agent.facts import compute_facts
from contract_agent.clause_review import review_clause
from contract_agent.model_backends import MockModelBackend
from contract_agent.options import build_options
from contract_agent.retriever import KeywordRetriever, rerank_fused
from contract_agent.schemas import ContractInput, DocumentInput
from contract_agent.segmentation import segment_contract
from contract_agent.tools_agent import AssessmentToolBox

TEXT = """1. Limitation of Liability
Each party's aggregate liability shall not exceed the fees paid in the
twelve (12) months preceding the claim.

2. Data Protection
Each party shall protect personal data in accordance with the Data
Processing Addendum. A party shall notify the other within 48 hours of
a personal data breach.
"""


def make(text=TEXT):
    contract = ContractInput(
        contract_id="CTR-9001",
        doc_type="msa",
        counterparty_name="Northwind Traders Ltd",
        our_role="vendor",
        documents=[DocumentInput(doc_type="agreement", document_id="AGR-1", raw_text=text)],
    )
    units = segment_contract(contract)
    extractions = extract_clauses(units, backend=object())
    verdicts = [review_clause(u, e) for u, e in zip(units, extractions)]
    facts = compute_facts(contract, units, extractions, verdicts)
    return contract, units, extractions, verdicts, facts


def rows(units, verdicts):
    by_id = {u.clause_id: u for u in units}
    return [
        {
            "clause_id": v.clause_id,
            "ref": by_id[v.clause_id].ref,
            "heading": by_id[v.clause_id].heading,
            "clause_type": v.clause_type,
            "position": v.position,
            "refs": v.playbook_refs,
            "rationale": v.rationale,
            "fallback": v.fallback_language,
        }
        for v in verdicts
    ]


def test_keyword_retriever_finds_cap_entry():
    hits = KeywordRetriever().retrieve("liability cap months of fees", top_k=2)
    assert hits and hits[0].playbook_id == "PB-LIA-01"


def test_rrf_rerank_prefers_entries_found_by_both():
    kw = KeywordRetriever().retrieve("mutual confidentiality obligations years", top_k=3)
    fused = rerank_fused(kw, kw, top_k=2)
    assert fused[0].sources == ["keyword", "semantic"] or set(fused[0].sources) == {"keyword", "semantic"}


def test_template_assessment_mentions_posture_and_cites():
    contract, units, extractions, verdicts, facts = make()
    retriever = KeywordRetriever()
    playbook = [h.model_dump() for h in retriever.retrieve("liability data protection", top_k=4)]
    assessment = build_assessment(
        contract=contract, facts=facts, verdicts=verdicts,
        clause_rows=rows(units, verdicts), extractions=extractions,
        playbook=playbook, backend=MockModelBackend(),
    )
    assert assessment.source == "template"
    assert facts.posture in assessment.summary
    assert citations_for(verdicts, playbook)


def test_options_recommend_escalation_for_walk_away():
    text = "1. Liability\nThe Provider's liability shall be unlimited and shall not be limited."
    contract, units, extractions, verdicts, facts = make(text)
    options, note = build_options(
        contract=contract, verdicts=verdicts, clause_rows=rows(units, verdicts),
        facts=facts, assessment_summary="x", playbook=[], backend=MockModelBackend(),
    )
    assert options and options[0].recommended
    assert options[0].kind == "escalate_to_senior_counsel"


def test_mock_draft_packet_and_response():
    contract, units, extractions, verdicts, facts = make()
    backend = MockModelBackend()
    options, _ = build_options(
        contract=contract, verdicts=verdicts, clause_rows=rows(units, verdicts),
        facts=facts, assessment_summary="Posture negotiable.", playbook=[], backend=backend,
    )
    recommended = next((o for o in options if o.recommended), None)
    context = {
        "contract_id": contract.contract_id,
        "counterparty_name": contract.counterparty_name,
        "our_role": contract.our_role,
        "doc_type": contract.doc_type,
        "title": contract.title,
        "posture": facts.posture,
        "positions_summary": "preferred: 2",
        "clause_rows": rows(units, verdicts),
        "missing_clauses": facts.missing_standard_clauses,
        "cover_text": "",
        "assessment_summary": "Posture negotiable.",
        "assessment_risk_factors": ["missing standard clause: indemnity"],
        "options": [o.model_dump() for o in options],
        "citations": citations_for(verdicts, []),
        "recommended_option_kind": recommended.kind if recommended else "",
    }
    subject, response, packet = backend.draft_documents(context)
    assert "CTR-9001" in subject
    assert "DRAFT FOR COUNSEL REVIEW" in packet
    assert "C-01" in packet and "[PB-LIA-01]" in packet
    assert "Next step:" in response
    assert "we agree" not in response.lower()


def test_toolbox_dispatch():
    contract, units, extractions, verdicts, facts = make()
    toolbox = AssessmentToolBox(
        contract=contract,
        facts={
            "posture": facts.posture,
            "worst_position": facts.worst_position,
            "positions_count": facts.positions_count,
            "clause_count": facts.clause_count,
            "missing_standard_clauses": facts.missing_standard_clauses,
            "critical_absences": facts.critical_absences,
            "clause_rows": rows(units, verdicts),
        },
        retriever=KeywordRetriever(),
        priors=[{"counterparty_name": "Northwind Traders Ltd", "posture": "negotiable", "decision": ""}],
    )
    text, summary = toolbox.dispatch("clause_facts", {})
    assert "posture" in text and summary.startswith("clause_facts")
    text, _ = toolbox.dispatch("counterparty_history", {"counterparty_name": "Northwind Traders Ltd"})
    assert "1 prior contract(s)" in text
    text, _ = toolbox.dispatch("search_playbook", {"query": "indemnity mutual"})
    assert "PB-IND" in text
