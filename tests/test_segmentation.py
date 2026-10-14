"""Segmentation: real counterparty paper is formatted every which way,
and a missed heading folds a clause into its neighbour — which the
absence check then reports as missing. These pin the variants."""

from __future__ import annotations

import pytest

from contract_agent.schemas import ContractInput
from contract_agent.segmentation import segment_contract, segment_text


def headings(text: str) -> list[tuple[str, str]]:
    return [(u.ref, u.heading) for u in segment_text(text) if u.heading]


@pytest.mark.parametrize("line, expected", [
    ("1. Limitation of Liability", ("1", "Limitation of Liability")),
    ("1. Limitation of Liability.", ("1", "Limitation of Liability")),
    ("3. Term; Termination", ("3", "Term; Termination")),
    ("8. Governing Law: Jurisdiction", ("8", "Governing Law: Jurisdiction")),
    ("6. Fees & Payment", ("6", "Fees & Payment")),
    ("1. Parties’ Limitation of Liability", ("1", "Parties' Limitation of Liability")),
    ("3. TERM; AUTOMATIC RENEWAL", ("3", "TERM; AUTOMATIC RENEWAL")),
    ("§ 4 Confidentiality", ("4", "Confidentiality")),
    ("**7. Data Protection**", ("7", "Data Protection")),
    ("3. Breach notification", ("3", "Breach notification")),
    ("ARTICLE 2 – INDEMNIFICATION", ("2", "– INDEMNIFICATION")),
])
def test_heading_variants_start_a_clause(line, expected):
    assert headings(f"{line}\nThe clause body.") == [expected]


def test_inline_heading_splits_heading_from_body():
    units = segment_text("1. Fees. Customer will pay invoices within thirty (30) days.")
    assert [(u.ref, u.heading, u.text) for u in units] == [
        ("1", "Fees", "Customer will pay invoices within thirty (30) days."),
    ]


def test_unnumbered_title_case_heading_at_a_paragraph_start():
    text = "Preamble text.\n\nLimitation of Liability\nLiability is capped at fees paid."
    assert headings(text) == [("", "Limitation of Liability")]


@pytest.mark.parametrize("sentence", [
    "1. Each party shall pay all invoices.",
    "2. The Customer may terminate on notice.",
    "3. Provider must notify Customer of any breach.",
])
def test_numbered_sentence_stays_body_text(sentence):
    assert headings(f"The parties agree as follows.\n{sentence}\nMore text.") == []


def test_headingless_agreement_falls_back_to_paragraphs():
    text = "\n\n".join([
        "MASTER SERVICES AGREEMENT",
        "1. Each party's aggregate liability shall not exceed the fees paid in the twelve (12) months preceding the claim.",
        "2. Each party shall indemnify the other against third-party claims arising from its breach.",
        "3. This Agreement is governed by the laws of the State of Delaware.",
    ])
    units = segment_contract(ContractInput(contract_id="CTR-T1", contract_text=text))
    assert [u.ref for u in units] == ["", "1", "2", "3"]
    assert units[1].text.startswith("Each party's aggregate liability")


def test_fallback_does_not_touch_a_well_headed_agreement():
    text = "1. Fees\nNet 30.\n\n2. Governing Law\nDelaware.\n\nSigned.\n\nDated."
    units = segment_contract(ContractInput(contract_id="CTR-T2", contract_text=text))
    assert [u.heading for u in units if u.heading] == ["Fees", "Governing Law"]


def test_signature_block_is_not_read_as_clauses():
    text = (
        "1. Governing Law\nThis Agreement is governed by Delaware law.\n\n"
        "IN WITNESS WHEREOF the parties have executed this Agreement.\n\n"
        "Northwind Traders Ltd\nBy: ____________\nName: Jane Doe\n\n"
        "Acme Corporation\nBy: ____________\nName: John Roe\n"
    )
    assert [h for _, h in headings(text)] == ["Governing Law"]


@pytest.mark.parametrize("text, expected", [
    # A numbered list inside a clause follows its lead-in line: not headings.
    ("6. Fees and Payment\nThe Customer shall pay as follows:\n1. Monthly invoices.\n2. Net 30.\n",
     ["Fees and Payment"]),
    ("RECITALS\nWHEREAS:\nA. The Customer wishes to buy services.\n", ["Recitals"]),
    ("9. Assignment\nNo assignment without consent.\n\nIN WITNESS WHEREOF.\nThe parties have signed.\n",
     ["Assignment"]),
    ("5. Confidentiality\nEach party keeps secrets.\n\nPage 2 of 10\nThe obligations survive.\n",
     ["Confidentiality"]),
])
def test_body_lines_that_look_like_headings_stay_body(text, expected):
    assert [h for _, h in headings(text)] == expected


def test_abbreviation_is_not_an_inline_heading():
    assert headings("1. Mr. Smith shall act as the Provider's representative.") == []
