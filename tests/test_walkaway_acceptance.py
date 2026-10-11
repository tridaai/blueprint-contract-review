"""Walk-away line acceptance in the briefs (this round): the
capture/resolution machinery (``walkaway.py``, rounds 9–10) has
been on the record for rounds, and the concession profile counts
it — but the documents counsel actually reads into the call did
not tally it. Now both briefs carry an acceptance section: of the
walk-away lines counsel APPROVED, how many held, were softened,
or were dropped, with each line's lineage beside the count. The
population is approved lines only — a rejected packet's lines
never went out, so they are not acceptance lines."""

from __future__ import annotations

import pytest

from contract_agent.samples import sample_contract_models
from contract_agent.schemas import AgentResult, WalkAwayOutcome
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore
from contract_agent.walkaway import acceptance_report
from helpers import contract


@pytest.fixture(scope="module")
def samples() -> dict:
    return {c.contract_id: c for c in sample_contract_models()}


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


def _outcome(clause_type: str, status: str, **overrides) -> WalkAwayOutcome:
    base = {
        "clause_id": "C-01",
        "clause_type": clause_type,
        "line_language": "Final position: the playbook's line.",
        "position_at_decision": "walk_away",
        "decision": "approved",
        "decided_by": "C. Ounsel",
        "captured_at": "2026-10-10T12:00:00+00:00",
        "status": status,
        "final_position": "preferred" if status == "held" else "",
        "resolved_in": "CTR-F2" if status != "pending" else "",
    }
    base.update(overrides)
    return WalkAwayOutcome(**base)


# ---------------------------------------------------------------------------
# The tally: approved lines only, every outcome counted once
# ---------------------------------------------------------------------------


def test_acceptance_report_counts_approved_lines_only():
    decided = AgentResult(
        contract_id="CTR-F1",
        decision="approved",
        walk_away_outcomes=[
            _outcome("liability_cap", "held"),
            _outcome("ip_ownership", "softened", final_position="review_required"),
            _outcome("payment_terms", "pending"),
        ],
    )
    refused = AgentResult(
        contract_id="CTR-F9",
        decision="rejected",
        walk_away_outcomes=[
            _outcome(
                "liability_cap",
                "dropped",
                decision="rejected",
                final_position="walk_away",
            )
        ],
    )
    report = acceptance_report([decided, refused])
    # The rejected packet's dropped line is not an acceptance line:
    # counsel never sent it, so it cannot have been dropped by the
    # counterparty.
    assert report["approved_lines"] == 3
    assert report["held"] == 1
    assert report["softened"] == 1
    assert report["dropped"] == 0
    assert report["clause_removed"] == 0
    assert report["still_open"] == 1  # the record's pending, in brief words
    line = report["lines"][0]
    assert line["decided_in"] == "CTR-F1"
    assert line["resolved_in"] == "CTR-F2"
    assert line["final_position"] == "preferred"
    assert line["position_at_decision"] == "walk_away"


def test_acceptance_report_empty_without_decisions():
    report = acceptance_report([AgentResult(contract_id="CTR-F0")])
    assert report["approved_lines"] == 0
    assert report["lines"] == []


# ---------------------------------------------------------------------------
# The chain brief: headline tally + per-line lineage
# ---------------------------------------------------------------------------


def test_chain_brief_acceptance_section_tallies_the_held_lines(samples):
    svc = _service()
    svc.analyze(samples["CTR-2014"])
    svc.approve("CTR-2014", approver="C. Ounsel", reason="send the packet")
    svc.analyze(samples["CTR-2015"])
    md = svc.negotiation_brief("CTR-2015")["markdown"]
    assert "## Walk-away line acceptance" in md
    assert (
        "Of the 2 walk-away lines counsel approved in this chain, "
        "2 held, 0 softened, 0 dropped." in md
    )
    # Per-line lineage: the deciding round, the follow-up round,
    # and where the clause landed.
    assert "**liability_cap**" in md and "**ip_ownership**" in md
    assert "approved on CTR-2014 at walk_away" in md
    assert "the follow-up round (CTR-2015) placed it at preferred — **held**" in md


def test_chain_brief_acceptance_section_before_any_approval():
    svc = _service()
    result = svc.analyze(contract())
    md = svc.negotiation_brief(result.contract_id)["markdown"]
    assert "## Walk-away line acceptance" in md
    assert "No walk-away lines have been approved in this chain yet" in md


# ---------------------------------------------------------------------------
# The matter brief: the same tally summed across chains, as data
# ---------------------------------------------------------------------------


def test_matter_brief_acceptance_across_chains(samples):
    svc = _service()
    matter = "Acceptance matter"
    svc.analyze(samples["CTR-2014"].model_copy(update={"matter": matter}))
    svc.approve("CTR-2014", approver="C. Ounsel")
    svc.analyze(samples["CTR-2015"].model_copy(update={"matter": matter}))
    brief = svc.matter_brief("Vertex Analytics Ltd", matter)
    acceptance = brief["data"]["acceptance"]
    assert acceptance["approved_lines"] == 2
    assert acceptance["held"] == 2
    assert acceptance["softened"] == 0
    assert acceptance["still_open"] == 0
    assert {line["decided_in"] for line in acceptance["lines"]} == {"CTR-2014"}
    md = brief["markdown"]
    assert "## Walk-away line acceptance across the matter" in md
    assert (
        "Of the 2 walk-away lines counsel approved across this "
        "matter, 2 held, 0 softened, 0 dropped." in md
    )
    assert "| CTR-2014 | liability_cap" in md
    assert "| CTR-2015 | preferred | held |" in md
