"""Brief deltas between exports (round 6): every export is
retained in object storage under a versioned key, its source
digest is stored in the chain's brief lineage index, and the next
export leads with a "Since the last brief" section diffed against
the previous export's digest."""

from __future__ import annotations

import json

import pytest

from contract_agent.brief import brief_delta_lines, brief_source_digest
from contract_agent.objectstore import InMemoryObjectStore
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import EditOutcomeInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore
from helpers import contract

# A term clause carrying dated obligations (renewal + computed
# notice deadline), and the same agreement with the clause gone.
_TERM_TEXT = """1. Term and Termination
This Agreement renews automatically on March 1, 2027 for successive one (1) year terms unless either party gives at least ninety (90) days' written notice of non-renewal.

2. Limitation of Liability
Each party's aggregate liability shall not exceed the fees paid in the twelve (12) months preceding the claim.
"""

_NO_TERM_TEXT = """1. Limitation of Liability
Each party's aggregate liability shall not exceed the fees paid in the twelve (12) months preceding the claim.
"""


@pytest.fixture(scope="module")
def samples() -> dict:
    return {c.contract_id: c for c in sample_contract_models()}


def _service(objects: InMemoryObjectStore | None = None) -> ContractService:
    return ContractService(
        store=InMemoryStore(),
        checkpointer=False,
        object_store=objects if objects is not None else InMemoryObjectStore(),
    )


def test_first_export_says_it_is_the_first(samples):
    objects = InMemoryObjectStore()
    svc = _service(objects)
    svc.analyze(samples["CTR-2014"])
    brief = svc.negotiation_brief("CTR-2014")
    assert brief["export_seq"] == 1
    assert brief["previous_export"] is None
    assert brief["delta_lines"] == []
    assert "## Since the last brief" in brief["markdown"]
    assert "first brief exported for this negotiation" in brief["markdown"]
    # The export is retained under its versioned key, the canonical
    # key holds the same document.
    assert objects.get("contracts/CTR-2014/negotiation-brief-v1.md") is not None
    assert (
        objects.get("contracts/CTR-2014/negotiation-brief.md").decode("utf-8")
        == brief["markdown"]
    )


def test_chain_growth_delta_names_the_new_round_and_the_moves(samples):
    svc = _service()
    svc.analyze(samples["CTR-2014"])
    svc.negotiation_brief("CTR-2014")
    svc.analyze(samples["CTR-2015"])
    brief = svc.negotiation_brief("CTR-2015")
    assert brief["export_seq"] == 2
    assert brief["previous_export"] == {
        "contract_id": "CTR-2014",
        "key": "contracts/CTR-2014/negotiation-brief-v1.md",
    }
    delta = brief["delta_lines"]
    assert any(
        line.startswith("New round since the last brief: CTR-2015") for line in delta
    )
    assert "Clause moved: liability_cap (C-02): walk_away → preferred." in delta
    assert any(line.startswith("New proposed edit: C-06 term_termination") for line in delta)
    # And the lines lead the document itself.
    md = brief["markdown"]
    assert "## Since the last brief" in md
    assert "New round since the last brief: CTR-2015" in md
    assert md.index("## Since the last brief") < md.index("## Negotiation chain")


def test_outcome_and_reflection_changes_appear_in_the_next_delta(samples):
    svc = _service()
    svc.analyze(samples["CTR-2014"])
    svc.analyze(samples["CTR-2015"])
    svc.negotiation_brief("CTR-2015")
    gov = next(
        i
        for i, e in enumerate(svc.get("CTR-2014").proposed_edits)
        if e.clause_type == "governing_law"
    )
    svc.record_edit_outcomes(
        "CTR-2014",
        [EditOutcomeInput(edit_index=gov, outcome="accepted", note="NY law agreed")],
        actor="C. Ounsel",
    )
    brief = svc.negotiation_brief("CTR-2015")
    assert f"Edit outcome recorded: CTR-2014 edit {gov} — accepted." in brief["delta_lines"]
    assert any(
        "agreed-but-not-reflected" in line for line in brief["delta_lines"]
    )


def test_decision_and_unchanged_reexport_deltas(samples):
    svc = _service()
    svc.analyze(samples["CTR-2015"])  # chain walk ends the lineage at CTR-2014's absence
    svc.negotiation_brief("CTR-2015")
    again = svc.negotiation_brief("CTR-2015")
    assert again["delta_lines"] == [
        "Nothing material changed since the last brief — the "
        "facts below are the same facts, re-exported."
    ]
    svc.approve("CTR-2015", approver="C. Ounsel")
    decided = svc.negotiation_brief("CTR-2015")
    assert decided["delta_lines"] == [
        "Decision recorded since the last brief: approved by "
        "C. Ounsel on CTR-2015."
    ]


def test_closed_obligations_appear_in_the_delta():
    svc = _service()
    first = svc.analyze(contract(_TERM_TEXT, contract_id="CTR-OB-1"))
    assert first.obligations  # the term clause produced register entries
    svc.negotiation_brief("CTR-OB-1")
    svc.analyze(contract(_NO_TERM_TEXT, contract_id="CTR-OB-2", supersedes="CTR-OB-1"))
    brief = svc.negotiation_brief("CTR-OB-2")
    assert any(
        line.startswith(
            "Obligation closed or no longer on the register: notice_period"
        )
        for line in brief["delta_lines"]
    )
    assert any(
        line.startswith("Obligation closed or no longer on the register: renewal")
        for line in brief["delta_lines"]
    )


def test_both_exports_are_retained_and_indexed(samples):
    objects = InMemoryObjectStore()
    svc = _service(objects)
    svc.analyze(samples["CTR-2014"])
    first = svc.negotiation_brief("CTR-2014")
    svc.analyze(samples["CTR-2015"])
    second = svc.negotiation_brief("CTR-2015")
    third = svc.negotiation_brief("CTR-2015")
    # Every version survives; the canonical key for CTR-2015 holds
    # the LATEST export, and CTR-2014's holds its own.
    assert objects.get("contracts/CTR-2014/negotiation-brief-v1.md").decode() == (
        first["markdown"]
    )
    assert objects.get("contracts/CTR-2015/negotiation-brief-v2.md").decode() == (
        second["markdown"]
    )
    assert objects.get("contracts/CTR-2015/negotiation-brief-v3.md").decode() == (
        third["markdown"]
    )
    assert objects.get("contracts/CTR-2015/negotiation-brief.md").decode() == (
        third["markdown"]
    )
    # The lineage index is keyed by the chain root and carries the
    # digests the deltas were computed from.
    index = json.loads(objects.get("contracts/CTR-2014/negotiation-brief-index.json"))
    assert [entry["seq"] for entry in index] == [1, 2, 3]
    assert [entry["contract_id"] for entry in index] == [
        "CTR-2014",
        "CTR-2015",
        "CTR-2015",
    ]
    assert all("digest" in entry and "exported_at" in entry for entry in index)


def test_digest_and_delta_units(samples):
    svc = _service()
    r14 = svc.analyze(samples["CTR-2014"])
    digest = brief_source_digest([r14], r14)
    assert digest["contract_id"] == "CTR-2014"
    assert digest["clause_positions"]["liability_cap:C-02"] == "walk_away"
    assert digest["reflection_flags"] == 0
    # A digest diffed against itself is the honest no-news line.
    assert brief_delta_lines(digest, digest) == [
        "Nothing material changed since the last brief — the "
        "facts below are the same facts, re-exported."
    ]
