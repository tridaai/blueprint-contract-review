"""Covenant movements in the brief delta (round 19): the
negotiation brief's "Since the last brief" section already
narrated clause and verdict drift, but a renegotiated covenant
— its tail shortened on an amendment, a covenant dropped —
lived only in the annex history's covenant biography. The
brief digest now carries the biography's step readings
verbatim, and the delta names the movements of the steps that
arrived since the previous export, in the biography's own
summary sentences: the two surfaces share one computation,
not two tellings of it.
"""

from __future__ import annotations

import pytest

from contract_agent.brief import brief_delta_lines, brief_source_digest
from contract_agent.objectstore import InMemoryObjectStore
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def _service() -> ContractService:
    return ContractService(
        store=InMemoryStore(),
        checkpointer=False,
        object_store=InMemoryObjectStore(),
    )


def _covenant_lines(brief: dict) -> list[str]:
    return [
        line
        for line in brief["delta_lines"]
        if line.startswith("Covenant movement since the last brief")
    ]


def test_delta_names_the_amendments_covenant_movements(samples):
    svc = _service()
    svc.analyze(samples["CTR-2038"])  # exclusivity 12mo, non-circumvention 24mo
    svc.negotiation_brief("CTR-2038")
    svc.analyze(samples["CTR-2039"])  # tail shortened, non-circumvention dropped
    brief = svc.negotiation_brief("CTR-2039")
    (line,) = _covenant_lines(brief)
    assert line.startswith(
        "Covenant movement since the last brief (CTR-2038 → CTR-2039): "
    )
    # The biography's own sentences, verbatim: the tail change
    # and the drop are both named, with both lengths.
    assert (
        "the exclusivity covenant's tail changes from 12 months "
        "following termination of the agreement to 6 months "
        "following termination of the agreement" in line
    )
    assert (
        "the non-circumvention covenant is dropped (it ran 24 "
        "months following termination of the agreement)" in line
    )
    # And the line leads the document, in the delta section.
    md = brief["markdown"]
    assert "Covenant movement since the last brief" in md
    assert md.index("## Since the last brief") < md.index(
        "Covenant movement since the last brief"
    )


def test_the_delta_line_is_the_biographys_own_sentence(samples):
    svc = _service()
    svc.analyze(samples["CTR-2038"])
    svc.negotiation_brief("CTR-2038")
    svc.analyze(samples["CTR-2039"])
    brief = svc.negotiation_brief("CTR-2039")
    biography = svc.annex_history("CTR-2039")["covenants"]
    (step,) = biography["steps"]
    (line,) = _covenant_lines(brief)
    # One computation, two surfaces: the delta line ends with
    # the biography step's summary, character for character.
    assert line.endswith(step["summary"])


def test_a_reexport_names_no_covenant_movement(samples):
    svc = _service()
    svc.analyze(samples["CTR-2038"])
    svc.analyze(samples["CTR-2039"])
    svc.negotiation_brief("CTR-2039")
    again = svc.negotiation_brief("CTR-2039")
    assert _covenant_lines(again) == []


def test_a_first_brief_has_no_delta_to_narrate(samples):
    svc = _service()
    svc.analyze(samples["CTR-2038"])
    svc.analyze(samples["CTR-2039"])
    brief = svc.negotiation_brief("CTR-2039")
    assert brief["export_seq"] == 1
    assert brief["delta_lines"] == []


def test_a_chain_without_covenants_carries_no_steps(samples):
    svc = _service()
    svc.analyze(samples["CTR-2014"])
    svc.negotiation_brief("CTR-2014")
    svc.analyze(samples["CTR-2015"])
    brief = svc.negotiation_brief("CTR-2015")
    assert _covenant_lines(brief) == []


# ---------------------------------------------------------------------------
# Digest + delta units: the arrival rule, without the pipeline
# ---------------------------------------------------------------------------

_STEP = {
    "from_contract_id": "CTR-A",
    "to_contract_id": "CTR-B",
    "movements": [
        {"kind": "exclusivity", "change": "duration_changed",
         "detail": "6 months following termination of the agreement"},
    ],
    "summary": "the exclusivity covenant's tail changes from "
               "12 months to 6 months.",
}

_CARRIED_STEP = {
    "from_contract_id": "CTR-B",
    "to_contract_id": "CTR-C",
    "movements": [
        {"kind": "exclusivity", "change": "carried",
         "detail": "6 months following termination of the agreement"},
    ],
    "summary": "the exclusivity covenant is carried unchanged "
               "(6 months following termination of the agreement).",
}


def _digest(contract_id: str, chain: list[str], steps: list[dict]) -> dict:
    # The full key set brief_delta_lines reads (brief_source_
    # digest's shape), with only the covenant steps varying.
    return {
        "contract_id": contract_id,
        "chain": chain,
        "status": "awaiting_counsel",
        "decision": "",
        "decided_by": "",
        "posture": "aligned",
        "worst_position": "preferred",
        "clause_positions": {},
        "proposed_edits": [],
        "edit_outcomes": {},
        "obligations": [],
        "reflection_flags": 0,
        "re_trades": 0,
        "covenant_steps": steps,
    }


def test_delta_units_name_only_steps_that_arrived():
    previous = _digest("CTR-A", ["CTR-A"], [])
    current = _digest("CTR-B", ["CTR-A", "CTR-B"], [_STEP])
    lines = brief_delta_lines(previous, current)
    assert lines == [
        "New round since the last brief: CTR-B — the last brief "
        "covered CTR-A; the chain now runs CTR-A → CTR-B.",
        "Covenant movement since the last brief (CTR-A → CTR-B): "
        "the exclusivity covenant's tail changes from 12 months "
        "to 6 months.",
    ]
    # Diffed against itself, the step has already been narrated.
    assert brief_delta_lines(current, current) == [
        "Nothing material changed since the last brief — the "
        "facts below are the same facts, re-exported."
    ]


def test_delta_units_skip_a_step_that_only_carried():
    previous = _digest("CTR-B", ["CTR-A", "CTR-B"], [_STEP])
    current = _digest(
        "CTR-C", ["CTR-A", "CTR-B", "CTR-C"], [_STEP, _CARRIED_STEP]
    )
    lines = brief_delta_lines(previous, current)
    assert not any("Covenant movement" in line for line in lines)


def test_delta_units_tolerate_a_legacy_digest_without_steps():
    # An export retained before round 19 has every other digest
    # key but no covenant_steps; the delta reads the missing key
    # as no steps narrated yet, never a crash.
    previous = _digest("CTR-A", ["CTR-A"], [])
    del previous["covenant_steps"]
    current = _digest("CTR-B", ["CTR-A", "CTR-B"], [_STEP])
    lines = brief_delta_lines(previous, current)
    assert any("Covenant movement" in line for line in lines)


def test_digest_carries_the_steps_it_is_given(samples):
    svc = _service()
    result = svc.analyze(samples["CTR-2038"])
    digest = brief_source_digest([result], result, [_STEP])
    assert digest["covenant_steps"] == [
        {
            "from_contract_id": "CTR-A",
            "to_contract_id": "CTR-B",
            "movements": [
                {"kind": "exclusivity", "change": "duration_changed",
                 "detail": "6 months following termination of the agreement"}
            ],
            "summary": _STEP["summary"],
        }
    ]
    # And without steps, the key is present and empty.
    assert brief_source_digest([result], result)["covenant_steps"] == []
