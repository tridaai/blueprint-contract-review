"""Proposed edits: the packet's tier language mapped onto the
counterparty's own sentences, in code (round 5)."""

from __future__ import annotations

import pytest

from contract_agent.proposed_edits import build_proposed_edits, split_sentences
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ClauseDiff
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore


@pytest.fixture(scope="module")
def samples() -> dict:
    return {c.contract_id: c for c in sample_contract_models()}


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


def _diff(**overrides) -> ClauseDiff:
    base = {
        "clause_id": "C-01",
        "clause_type": "liability_cap",
        "position": "review_required",
        "counterparty_text": (
            "The Provider's aggregate liability shall be unlimited. "
            "The Provider shall have no liability for indirect damages."
        ),
        "playbook_fallback": (
            "Each party's aggregate liability shall not exceed the fees "
            "paid in the twelve (12) months preceding the claim."
        ),
        "proposed_tier": 1,
        "tier_name": "first fallback",
        "tier_language": (
            "Each party's aggregate liability shall not exceed the fees "
            "paid in the twelve (12) months preceding the claim."
        ),
    }
    base.update(overrides)
    return ClauseDiff(**base)


def test_matching_sentence_is_replaced_not_the_whole_clause():
    edits = build_proposed_edits([_diff()])
    assert len(edits) == 1
    edit = edits[0]
    assert edit.kind == "replace_sentence"
    assert edit.alignment == "sentence"
    assert "unlimited" in edit.original_sentence
    assert "shall not exceed the fees" in edit.replacement_text
    assert edit.tier == 1
    assert "Draft for counsel review" in edit.note


def test_unpaired_language_becomes_an_insert_after_the_paired_sentence():
    diff = _diff(
        tier_language=(
            "Each party's aggregate liability shall not exceed the fees "
            "paid in the twelve (12) months preceding the claim. "
            "The cap does not apply to a breach of confidentiality obligations."
        )
    )
    edits = build_proposed_edits([diff])
    kinds = [e.kind for e in edits]
    assert kinds == ["replace_sentence", "insert_after"]
    insert = edits[1]
    assert insert.anchor_sentence == edits[0].original_sentence
    assert "confidentiality" in insert.replacement_text


def test_weak_alignment_degrades_to_a_clause_level_replacement():
    diff = _diff(
        counterparty_text=(
            "This Agreement is governed by the laws of Delaware. "
            "The courts of Delaware have exclusive jurisdiction."
        ),
        tier_language=(
            "Each party's aggregate liability shall not exceed the fees "
            "paid in the twelve (12) months preceding the claim."
        ),
        playbook_fallback="",
    )
    edits = build_proposed_edits([diff])
    assert len(edits) == 1
    assert edits[0].kind == "replace_clause"
    assert edits[0].alignment == "clause_fallback"
    assert "Delaware" in edits[0].original_sentence


def test_no_language_means_counsel_drafts():
    diff = _diff(tier_language="", playbook_fallback="")
    edits = build_proposed_edits([diff])
    assert len(edits) == 1
    assert edits[0].kind == "counsel_draft"
    assert edits[0].replacement_text == ""


def test_tier_language_wins_over_the_plain_fallback():
    diff = _diff(
        proposed_tier=2,
        tier_name="walk-away line",
        tier_language=(
            "Final position on liability: aggregate liability must be "
            "capped at the fees paid in the twelve (12) months "
            "preceding the claim."
        ),
    )
    edits = build_proposed_edits([diff])
    assert edits
    assert all(e.tier == 2 for e in edits)
    assert any("Final position" in e.replacement_text for e in edits)


def test_headings_and_fragments_are_not_alignment_targets():
    sentences = split_sentences(
        "1. Limitation of Liability\n"
        "The Provider's aggregate liability shall be unlimited in all cases."
    )
    assert sentences == [
        "The Provider's aggregate liability shall be unlimited in all cases."
    ]


def test_pipeline_attaches_edits_for_flagged_clauses_only(samples):
    result = _service().analyze(samples["CTR-2010"])
    diff_ids = {d.clause_id for d in result.clause_diffs}
    assert result.proposed_edits, "a flagged contract should carry edits"
    assert {e.clause_id for e in result.proposed_edits} <= diff_ids
    tiers = {d.clause_id: d.proposed_tier for d in result.clause_diffs}
    for edit in result.proposed_edits:
        assert edit.tier == tiers[edit.clause_id]
        assert edit.position != "preferred"
        assert "Draft for counsel review" in edit.note


def test_chain_refinement_rebuilds_the_edits(samples):
    service = _service()
    service.analyze(samples["CTR-2014"])
    service.analyze(samples["CTR-2015"])
    result = service.analyze(samples["CTR-2016"])
    assert result.round_chain is not None
    diff_ids = {d.clause_id for d in result.clause_diffs}
    assert {e.clause_id for e in result.proposed_edits} <= diff_ids
    # Re-traded clause types propose tier 2 — and their edits carry it.
    retraded = {
        d.clause_id
        for d in result.clause_diffs
        if d.clause_type in result.round_chain.re_traded_clause_types
    }
    for edit in result.proposed_edits:
        if edit.clause_id in retraded and edit.kind != "counsel_draft":
            assert edit.tier == 2
