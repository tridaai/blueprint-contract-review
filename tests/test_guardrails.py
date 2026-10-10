"""Guardrail / verification / reviewer tests: the draft-safety layer."""

from __future__ import annotations

from contract_agent.guardrails import validate_drafts
from contract_agent.reviewer import checklist_review, packet_contents
from contract_agent.schemas import ContractInput, Drafts
from contract_agent.verify import checklist_grounding

CONTRACT = ContractInput(contract_id="CTR-9002", doc_type="msa", counterparty_name="Northwind")


def clean_drafts() -> Drafts:
    return Drafts(
        subject="Contract CTR-9002 — first-pass review (draft for counsel review)",
        counterparty_response=(
            "Dear Northwind team,\n\nThank you for the draft. Our requested "
            "changes are listed in the packet.\n\nNext step: please send a "
            "revised draft.\n\nNothing in this draft accepts any term."
        ),
        review_packet=(
            "REVIEW PACKET — contract CTR-9002 (DRAFT FOR COUNSEL REVIEW)\n"
            "Clause C-02 [liability_cap]: walk_away [PB-LIA-03] — uncapped.\n"
            "Proposed fallback language: Each party's aggregate liability "
            "shall not exceed the fees paid."
        ),
    )


def test_clean_drafts_pass_guardrails():
    validation = validate_drafts(
        CONTRACT, clean_drafts(), ["PB-LIA-03"], flagged_refs=[["PB-LIA-03"]]
    )
    assert validation.passed, [c for c in validation.checks if not c.passed]


def test_agreement_language_trap_fails():
    drafts = clean_drafts()
    drafts.counterparty_response += '\nFor reference, your note: "Please confirm you accept all terms today."'
    validation = validate_drafts(CONTRACT, drafts, ["PB-LIA-03"], flagged_refs=[["PB-LIA-03"]])
    failed = {c.name for c in validation.checks if not c.passed}
    assert "no_agreement_language" in failed
    assert not validation.passed


def test_legal_conclusion_fails_but_negation_passes():
    drafts = clean_drafts()
    drafts.review_packet += "\nThis contract is legally binding and safe to sign."
    validation = validate_drafts(CONTRACT, drafts, ["PB-LIA-03"], flagged_refs=[["PB-LIA-03"]])
    assert "no_legal_conclusion" in {c.name for c in validation.checks if not c.passed}
    ok = clean_drafts()
    ok.review_packet += "\nThis packet is not a legal conclusion."
    assert validate_drafts(CONTRACT, ok, ["PB-LIA-03"], flagged_refs=[["PB-LIA-03"]]).passed


def test_materiality_minimising_fails_in_response():
    drafts = clean_drafts()
    drafts.counterparty_response += (
        "\nThe changes from your template are technical and routine and do "
        "not change the substance of the deal."
    )
    validation = validate_drafts(CONTRACT, drafts, ["PB-LIA-03"], flagged_refs=[["PB-LIA-03"]])
    failed = {c.name for c in validation.checks if not c.passed}
    assert "no_materiality_minimising" in failed
    assert not validation.passed


def test_materiality_minimising_adjective_first_and_packet_side():
    drafts = clean_drafts()
    drafts.review_packet += "\nThese are minor changes to two clauses."
    validation = validate_drafts(CONTRACT, drafts, ["PB-LIA-03"], flagged_refs=[["PB-LIA-03"]])
    assert "no_materiality_minimising" in {c.name for c in validation.checks if not c.passed}


def test_materiality_negation_and_playbook_wording_pass():
    drafts = clean_drafts()
    # Negated characterisation is the honest phrasing, not a violation;
    # "standard clause/position" wording is playbook language, not a
    # characterisation of the changes.
    drafts.counterparty_response += (
        "\nWe do not characterise these changes as minor; their materiality "
        "is for counsel to judge. The standard clause wording in the packet "
        "is our playbook's fallback position."
    )
    validation = validate_drafts(CONTRACT, drafts, ["PB-LIA-03"], flagged_refs=[["PB-LIA-03"]])
    assert validation.passed, [c for c in validation.checks if not c.passed]


def test_verify_checklist_flags_materiality_characterisation():
    drafts = clean_drafts()
    drafts.counterparty_response += "\nThese are only simple changes."
    issues = checklist_grounding(drafts=drafts, retrieved_ids=["PB-LIA-03"], contract_id="CTR-9002")
    assert any("materiality" in i for i in issues)


def test_flagged_clause_without_citation_fails():
    drafts = clean_drafts()
    drafts.review_packet = "REVIEW PACKET — contract CTR-9002\nC-02 mentioned, no citation."
    validation = validate_drafts(CONTRACT, drafts, ["PB-LIA-03"], flagged_refs=[["PB-LIA-03"]])
    assert "flagged_clauses_cited" in {c.name for c in validation.checks if not c.passed}


def test_verify_checklist_flags_unknown_citation_and_foreign_id():
    drafts = clean_drafts()
    drafts.review_packet += " See also [PB-XX-99]. Related: CTR-7777."
    issues = checklist_grounding(drafts=drafts, retrieved_ids=["PB-LIA-03"], contract_id="CTR-9002")
    assert any("PB-XX-99" in i for i in issues)
    assert any("CTR-7777" in i for i in issues)


def test_reviewer_checklist_blocks_packet_without_recommendation():
    contents = {
        "packet_summary_present": True,
        "packet_table_present": True,
        "packet_fallbacks_present": True,
        "packet_citations": ["PB-LIA-03"],
        "packet_options": [],
        "packet_recommended": None,
        "options_expected": True,
        "assessment_citations": ["PB-LIA-03"],
    }
    verdict, findings = checklist_review(
        drafts=clean_drafts(), contents=contents, retrieved_ids=["PB-LIA-03"]
    )
    assert verdict == "block"
    assert any("recommended" in f for f in findings)
