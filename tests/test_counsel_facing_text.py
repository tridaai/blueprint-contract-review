"""What counsel and the counterparty actually read: rationales state the
clause's own figures, the counterparty letter never reveals our
internal position, and the packet separates the citations its verdicts
rest on from the playbook context retrieval merely surfaced."""

from __future__ import annotations

import pytest

from contract_agent.graph import run_contract
from contract_agent.guardrails import find_internal_position_hit, validate_drafts
from contract_agent.model_backends import MockModelBackend
from contract_agent.retriever import KeywordRetriever
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput, Drafts

from helpers import CLEAN, contract


def run(c: ContractInput):
    return run_contract(c, backend=MockModelBackend(), retriever=KeywordRetriever())


def _rationale(result, clause_type: str) -> str:
    return next(v.rationale for v in result.clause_verdicts if v.clause_type == clause_type)


def _line(packet: str, prefix: str) -> str:
    return next((line for line in packet.splitlines() if line.startswith(prefix)), "")


# --- rationales state the clause's figures, not the playbook's -----------

def test_rationales_state_the_clauses_own_figures():
    result = run(contract())
    breach = _rationale(result, "data_protection")
    assert "48 hours" in breach and "within 72 hours" not in breach
    assert "12 months of fees" in _rationale(result, "liability_cap")
    assert "30-day notice window" in _rationale(result, "term_termination")


def test_below_preferred_cap_states_the_cap_and_claims_no_mutuality():
    text = CLEAN.replace(
        "Each party's aggregate liability shall not exceed the fees paid in the\n"
        "twelve (12) months preceding the claim.",
        "The Provider's aggregate liability shall not exceed the fees paid in the\n"
        "three (3) months preceding the claim.",
    )
    rationale = _rationale(run(contract(text)), "liability_cap")
    assert "3 months of fees" in rationale
    assert "mutual" not in rationale.lower()


# --- the counterparty letter never reveals our internal position ---------

CONTRACT = ContractInput(contract_id="CTR-9003", doc_type="msa", counterparty_name="Northwind")


def _drafts(response: str) -> Drafts:
    return Drafts(
        subject="Contract CTR-9003 (draft for counsel review)",
        counterparty_response=response + "\nNext step: please send a revised draft.",
        review_packet="REVIEW PACKET — contract CTR-9003 [PB-LIA-02]",
    )


@pytest.mark.parametrize("leak", [
    "Clause 1: requested position per our playbook.",
    "Clause 1: we cite [PB-LIA-02].",
    "Clause 1 is one of our walk-away items.",
    "We propose our fallback wording.",
    "Clause 2 already matches our preferred position.",
    "The items are listed in the review packet.",
])
def test_internal_position_disclosure_blocks_the_draft(leak):
    validation = validate_drafts(CONTRACT, _drafts(leak), ["PB-LIA-02"])
    failed = {c.name for c in validation.checks if not c.passed}
    assert "no_internal_position_disclosure" in failed
    assert not validation.passed


def test_requested_change_and_wording_pass_the_disclosure_check():
    response = "Clause 1 (Limitation of Liability): we propose this wording: Each party's liability is capped."
    assert find_internal_position_hit(response) is None


@pytest.mark.parametrize("sample", sample_contract_models(), ids=lambda c: c.contract_id)
def test_no_bundled_sample_letter_reveals_our_position(sample):
    result = run(sample)
    hit = find_internal_position_hit(result.drafts.counterparty_response)
    assert hit is None, f"{sample.contract_id}: counterparty letter says '{hit}'"


# --- packet citations: findings vs. retrieved context --------------------

def test_clean_contract_does_not_cite_the_uncapped_walk_away():
    result = run(contract())
    cited = _line(result.drafts.review_packet, "Playbook citations:")
    assert "[PB-LIA-01]" in cited  # what the liability verdict rests on
    assert "[PB-LIA-03]" not in cited  # retrieval context, not a finding
    consulted = _line(result.drafts.review_packet, "Also consulted")
    assert "[PB-LIA-03]" not in cited and ("[PB-LIA-03]" in consulted or not consulted)
    assert result.review.verdict == "pass"


def test_uncapped_liability_cites_the_walk_away():
    text = CLEAN.replace(
        "Each party's aggregate liability shall not exceed the fees paid in the\n"
        "twelve (12) months preceding the claim.",
        "The Provider's liability under this Agreement shall be unlimited.",
    )
    cited = _line(run(contract(text)).drafts.review_packet, "Playbook citations:")
    assert "[PB-LIA-03]" in cited


def test_counterparty_heading_echo_does_not_block_approval():
    # The counterparty's own clause heading, echoed back, is not our
    # disclosure — a "Fallback Rate" clause must stay approvable.
    text = CLEAN.replace(
        "6. Governing Law\n",
        "6. Fallback Rate\nIf the reference rate is unavailable the fallback rate applies.\n\n7. Governing Law\n",
    )
    result = run(contract(text))
    assert result.validation.passed, [c for c in result.validation.checks if not c.passed]
    assert find_internal_position_hit("Clause 6 (Fallback Rate): we request a revision.") is None
    assert find_internal_position_hit("Clause 6 (Fallback Rate): our fallback is attached.") == "fallback"


def test_signature_block_keeps_a_clean_contract_aligned():
    text = CLEAN + (
        "\nIN WITNESS WHEREOF the parties have executed this Agreement.\n\n"
        "Northwind Traders Ltd\nBy: ____________\nName: Jane Doe\n\n"
        "Acme Corporation\nBy: ____________\nName: John Roe\n"
    )
    assert run(contract(text)).facts.posture == "aligned"
