"""Prior-invention schedule exhibits (round 11): an employment
agreement's invention assignment that excludes "inventions listed
on the schedule of prior inventions" makes the schedule
load-bearing. The checks: the schedule must be in the package and
list inventions (``prior_invention_schedule`` — promised but
absent is exhibit_missing), and when it is attached and lists
inventions, the assignment must actually exclude them
(``prior_invention_exclusion``) — an assignment that never
excludes the scheduled inventions would swallow them.
"""

from __future__ import annotations

import pytest

from contract_agent.exhibit_review import build_exhibit_checks
from contract_agent.extractor import extract_clauses
from contract_agent.samples import sample_contract_models
from contract_agent.segmentation import segment_contract
from contract_agent.schemas import ContractInput, DocumentInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore
from helpers import contract


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


def _checks(result, name: str):
    return [c for c in result.exhibit_checks if c.name == name]


def _by_name(result) -> dict:
    return {c.name: c for c in result.exhibit_checks}


# ---------------------------------------------------------------------------
# The samples: promised-but-absent, swallowed, and clean
# ---------------------------------------------------------------------------


def test_ctr2025_promised_schedule_absent_is_exhibit_missing(samples):
    # CTR-2025's assignment excludes inventions "listed on the
    # schedule of prior inventions signed by the Employee" — and no
    # schedule is in the package. The exclusion points at a document
    # nobody attached.
    result = _service().analyze(samples["CTR-2025"])
    check = _checks(result, "prior_invention_schedule")
    assert len(check) == 1
    assert check[0].status == "exhibit_missing"
    assert check[0].promised == "schedule of prior inventions"
    # No schedule attached, so there is nothing for the exclusion
    # check to read the clause against — it is not generated.
    assert _checks(result, "prior_invention_exclusion") == []
    assert "prior_invention_schedule" in result.facts.exhibit_missing_checks
    assert result.facts.package_complete is False


def test_ctr2032_schedule_attached_but_assignment_swallows_it(samples):
    result = _service().analyze(samples["CTR-2032"])
    checks = _by_name(result)
    schedule = checks["prior_invention_schedule"]
    assert schedule.status == "consistent"
    assert "2 prior invention(s) listed" in schedule.found
    exclusion = checks["prior_invention_exclusion"]
    assert exclusion.status == "mismatch"
    assert "RouteWeaver" in exclusion.found
    assert "Ledgerline" in exclusion.found
    assert "swallow" in exclusion.detail
    assert "prior_invention_exclusion" in result.facts.exhibit_mismatches


def test_ctr2033_schedule_attached_and_excluded_is_consistent(samples):
    result = _service().analyze(samples["CTR-2033"])
    checks = _by_name(result)
    assert checks["prior_invention_schedule"].status == "consistent"
    assert checks["prior_invention_exclusion"].status == "consistent"
    failed = [c for c in result.exhibit_checks if c.status != "consistent"]
    assert failed == []


# ---------------------------------------------------------------------------
# The family only fires on its own ground
# ---------------------------------------------------------------------------


def test_no_schedule_language_no_checks(samples):
    # CTR-2026's assignment swallows prior inventions wholesale but
    # never relies on a schedule, and none is attached — the
    # verdict (walk_away) is the finding; this family stays silent.
    result = _service().analyze(samples["CTR-2026"])
    assert _checks(result, "prior_invention_schedule") == []
    assert _checks(result, "prior_invention_exclusion") == []


def test_msa_without_assignment_clause_generates_nothing(samples):
    result = _service().analyze(samples["CTR-2001"])
    assert _checks(result, "prior_invention_schedule") == []
    assert _checks(result, "prior_invention_exclusion") == []


# ---------------------------------------------------------------------------
# Unit level: the schedule attached but empty, and the clause shapes
# ---------------------------------------------------------------------------


_ASSIGNMENT = (
    "3. Invention Assignment\n"
    "The Employee hereby assigns to the Company all inventions conceived in the "
    "course of the employment. Inventions listed on the schedule of prior "
    "inventions are excluded from this assignment."
)


def _checks_for(text: str, documents=()):
    model = contract(text, doc_type="employment", documents=[
        DocumentInput(doc_type="agreement", document_id="AGR-1", raw_text=text),
        *documents,
    ])
    units = segment_contract(model)
    extractions = extract_clauses(units, model)
    return build_exhibit_checks(model, units, extractions)


def test_attached_schedule_listing_nothing_is_missing_in_exhibit():
    schedule = DocumentInput(
        doc_type="schedule",
        document_id="SCHED-A",
        raw_text="Schedule of Prior Inventions\n\nThe Employee has no prior inventions to record at this time.\n",
    )
    checks = _checks_for(_ASSIGNMENT, [schedule])
    by_name = {c.name: c for c in checks}
    assert by_name["prior_invention_schedule"].status == "missing_in_exhibit"
    # Nothing listed, so nothing for the exclusion to swallow.
    assert "prior_invention_exclusion" not in by_name


def test_clause_referencing_schedule_without_exclusion_language_mismatches():
    text = _ASSIGNMENT.replace(
        "Inventions listed on the schedule of prior inventions are excluded from this assignment.",
        "The Employee's prior inventions are recorded on the schedule of prior inventions.",
    )
    schedule = DocumentInput(
        doc_type="schedule",
        document_id="SCHED-A",
        raw_text=(
            "Schedule of Prior Inventions\n\n"
            "- RouteWeaver — a route-planning prototype\n"
        ),
    )
    checks = _checks_for(text, [schedule])
    by_name = {c.name: c for c in checks}
    assert by_name["prior_invention_schedule"].status == "consistent"
    assert by_name["prior_invention_exclusion"].status == "mismatch"
