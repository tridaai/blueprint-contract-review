"""Prior-invention schedule drift (round 12): the sub-processor
list got the cross-round treatment in round 10; the schedule of
prior inventions gets it here. The direction of movement is what
matters: an addition only widens the record of what predates the
employment, but a REMOVAL is a transfer — the assignment excludes
what the schedule lists, so an invention that leaves the schedule
between rounds falls into the assignment. The drift check names
the movement either way and reads a removal as a mismatch where
the assignment leans on the schedule.
"""

from __future__ import annotations

import pytest

from contract_agent.prior_invention_drift import (
    build_prior_invention_drift,
    prior_invention_entries,
)
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore

SCHEDULE = (
    "Schedule of Prior Inventions\n\n"
    "The Employee records the following inventions, conceived before the "
    "employment, as the Employee's prior inventions:\n"
    "- RouteWeaver — a route-planning prototype for regional delivery fleets\n"
    "- Ledgerline — a bookkeeping reconciliation tool for small practices\n"
)

_SCHEDULE_SENTENCE = (
    "Inventions conceived before the employment are excluded from this "
    "assignment, including any inventions listed on the schedule of prior "
    "inventions signed by the Employee and attached as Exhibit A."
)


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def _round(samples, contract_id: str, schedule_text: str | None,
           supersedes: str = "CTR-2033",
           assignment: str | None = None) -> ContractInput:
    """A round of the CTR-2033 chain carrying a chosen schedule
    (or none at all), and optionally a rewritten exclusion
    sentence in the assignment section."""
    data = samples["CTR-2033"].model_dump(mode="json")
    data["contract_id"] = contract_id
    data["supersedes"] = supersedes
    docs = []
    for doc in data["documents"]:
        if doc["document_id"] == "SCHED-A":
            if schedule_text is None:
                continue  # the schedule document is gone entirely
            doc = dict(doc, raw_text=schedule_text)
        if doc["doc_type"] == "agreement" and assignment is not None:
            doc = dict(doc, raw_text=doc["raw_text"].replace(
                _SCHEDULE_SENTENCE, assignment))
        docs.append(doc)
    data["documents"] = docs
    return ContractInput.model_validate(data)


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


# ---------------------------------------------------------------------------
# The diff itself
# ---------------------------------------------------------------------------


def test_entries_read_from_the_schedule(samples):
    entries = prior_invention_entries(samples["CTR-2033"])
    assert [e["name"] for e in entries] == ["RouteWeaver", "Ledgerline"]


def test_unchanged_schedule_is_consistent_and_named(samples):
    drift = build_prior_invention_drift(
        samples["CTR-2033"], _round(samples, "CTR-PI-1", SCHEDULE)
    )
    assert len(drift) == 1
    assert drift[0].name == "prior_invention_schedule_drift"
    assert drift[0].status == "consistent"
    assert drift[0].found == "schedule unchanged"


def test_an_addition_is_movement_not_a_finding(samples):
    grown = SCHEDULE + "- OrchardLens — an orchard survey annotation tool\n"
    drift = build_prior_invention_drift(
        samples["CTR-2033"], _round(samples, "CTR-PI-2", grown)
    )
    assert drift[0].status == "consistent"
    assert "added: OrchardLens" in drift[0].found


def test_a_removal_is_a_finding_because_the_assignment_leans_on_the_schedule(samples):
    shrunk = SCHEDULE.replace(
        "- Ledgerline — a bookkeeping reconciliation tool for small practices\n", "")
    drift = build_prior_invention_drift(
        samples["CTR-2033"], _round(samples, "CTR-PI-3", shrunk)
    )
    assert drift[0].status == "mismatch"
    assert "removed: Ledgerline" in drift[0].found
    assert "no longer excluded" in drift[0].detail
    assert "assigns it away" in drift[0].detail


def test_a_removal_is_information_when_the_assignment_does_not_lean_on_the_schedule(samples):
    shrunk = SCHEDULE.replace(
        "- RouteWeaver — a route-planning prototype for regional delivery fleets\n", "")
    general = (
        "Inventions conceived before the employment are excluded "
        "from this assignment."
    )
    drift = build_prior_invention_drift(
        samples["CTR-2033"],
        _round(samples, "CTR-PI-4", shrunk, assignment=general),
    )
    assert drift[0].status == "consistent"
    assert "does not tie its exclusion to the schedule" in drift[0].detail


def test_a_schedule_that_vanishes_entirely_is_the_loudest_removal(samples):
    drift = build_prior_invention_drift(
        samples["CTR-2033"], _round(samples, "CTR-PI-5", None)
    )
    assert drift[0].status == "mismatch"
    assert "RouteWeaver" in drift[0].found
    assert "Ledgerline" in drift[0].found


def test_no_baseline_no_drift(samples):
    # CTR-2025's package carries no schedule of prior inventions
    # at all: nothing listed in the prior round, nothing to
    # drift from — the single-round checks own that absence.
    round_ = _round(samples, "CTR-PI-6", SCHEDULE, supersedes="CTR-2025")
    assert build_prior_invention_drift(samples["CTR-2025"], round_) == []


# ---------------------------------------------------------------------------
# Through the service
# ---------------------------------------------------------------------------


def test_service_attaches_the_drift_and_the_brief_carries_it(samples):
    svc = _service()
    svc.analyze(samples["CTR-2033"])
    shrunk = SCHEDULE.replace(
        "- Ledgerline — a bookkeeping reconciliation tool for small practices\n", "")
    result = svc.analyze(_round(samples, "CTR-PI-7", shrunk))
    assert len(result.prior_invention_drift) == 1
    assert result.prior_invention_drift[0].status == "mismatch"
    brief = svc.negotiation_brief("CTR-PI-7")["markdown"]
    assert "prior_invention_schedule_drift" in brief
    assert "Ledgerline" in brief


def test_service_attaches_nothing_without_a_stored_prior(samples):
    svc = _service()
    result = svc.analyze(samples["CTR-2033"])
    assert result.prior_invention_drift == []
