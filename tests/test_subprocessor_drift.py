"""Sub-processor list drift (round 11): the DPA annex is a living
list, and a chain's rounds are its history. Consecutive rounds'
annexes are diffed — processors added, removed, or moved — and an
addition is checked against the notice clock the clause promises:
an addition that first appears in a round received inside the
promised notice window cannot have had the notice run, and is a
finding (mismatch), not just a change. Changes that break nothing
are still reported, as consistent, with the movement spelled out.
"""

from __future__ import annotations

import pytest

from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore
from contract_agent.subprocessor_drift import (
    build_subprocessor_drift,
    subprocessor_list_entries,
)


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def _round(samples, contract_id: str, received_at: str, annex: str | None,
           supersedes: str = "") -> ContractInput:
    """A round of the CTR-2031 DPA chain: same clauses, a chosen
    annex text (None keeps the sample's clean annex)."""
    data = samples["CTR-2031"].model_dump(mode="json")
    data["contract_id"] = contract_id
    data["received_at"] = received_at
    data["supersedes"] = supersedes
    if annex is not None:
        for doc in data["documents"]:
            if doc["document_id"] == "ANNEX-1":
                doc["raw_text"] = annex
    return ContractInput.model_validate(data)


_CLEAN_ANNEX = (
    "Annex 1 — Sub-processor List\n\n"
    "The Processor engages the following sub-processors in the processing under this Addendum:\n"
    "- Hostline GmbH (Germany) — data hosting\n"
    "- Postmark Systems Ltd (Ireland) — email delivery\n"
    "The Processor shall give the Controller at least thirty (30) days' written notice before any change to this list takes effect.\n"
)

_ADDED_ANNEX = (
    "Annex 1 — Sub-processor List\n\n"
    "The Processor engages the following sub-processors in the processing under this Addendum:\n"
    "- Hostline GmbH (Germany) — data hosting\n"
    "- Postmark Systems Ltd (Ireland) — email delivery\n"
    "- Cloudmetrics Inc (United States) — product analytics\n"
    "The Processor shall give the Controller at least thirty (30) days' written notice before any change to this list takes effect.\n"
)

_THREE_ANNEX = (
    "Annex 1 — Sub-processor List\n\n"
    "The Processor engages the following sub-processors in the processing under this Addendum:\n"
    "- Hostline GmbH (Germany) — data hosting\n"
    "- Postmark Systems Ltd (Ireland) — email delivery\n"
    "- Ledgerhost SAS (France) — bookkeeping hosting\n"
    "The Processor shall give the Controller at least thirty (30) days' written notice before any change to this list takes effect.\n"
)

_MOVED_ANNEX = (
    "Annex 1 — Sub-processor List\n\n"
    "The Processor engages the following sub-processors in the processing under this Addendum:\n"
    "- Hostline GmbH (Netherlands) — data hosting\n"
    "- Postmark Systems Ltd (Ireland) — email delivery\n"
    "The Processor shall give the Controller at least thirty (30) days' written notice before any change to this list takes effect.\n"
)


# ---------------------------------------------------------------------------
# The diff itself
# ---------------------------------------------------------------------------


def test_unchanged_list_reports_unchanged(samples):
    prior = _round(samples, "CTR-DRIFT-1", "2026-10-05T10:00:00", None)
    current = _round(samples, "CTR-DRIFT-2", "2026-10-20T10:00:00", _CLEAN_ANNEX,
                     supersedes="CTR-DRIFT-1")
    drift = build_subprocessor_drift(prior, current)
    assert len(drift) == 1
    assert drift[0].name == "subprocessor_list_drift"
    assert drift[0].status == "consistent"
    assert drift[0].found == "list unchanged"


def test_addition_inside_the_notice_window_is_a_finding(samples):
    prior = _round(samples, "CTR-DRIFT-1", "2026-10-05T10:00:00", None)
    current = _round(samples, "CTR-DRIFT-2", "2026-10-15T10:00:00", _ADDED_ANNEX,
                     supersedes="CTR-DRIFT-1")
    drift = build_subprocessor_drift(prior, current)
    assert len(drift) == 1
    check = drift[0]
    assert check.status == "mismatch"
    assert "Cloudmetrics Inc" in check.found
    assert "10 day(s)" in check.detail
    assert "cannot have run" in check.detail


def test_addition_after_the_notice_window_is_reported_not_flagged(samples):
    prior = _round(samples, "CTR-DRIFT-1", "2026-10-05T10:00:00", None)
    current = _round(samples, "CTR-DRIFT-2", "2026-11-20T10:00:00", _ADDED_ANNEX,
                     supersedes="CTR-DRIFT-1")
    check = build_subprocessor_drift(prior, current)[0]
    assert check.status == "consistent"
    assert "added: Cloudmetrics Inc (United States)" in check.found
    assert "outside the notice window" in check.detail


def test_removal_and_move_are_reported_without_a_finding(samples):
    prior = _round(samples, "CTR-DRIFT-1", "2026-10-05T10:00:00", _THREE_ANNEX)
    current = _round(samples, "CTR-DRIFT-2", "2026-10-20T10:00:00", _MOVED_ANNEX,
                     supersedes="CTR-DRIFT-1")
    check = build_subprocessor_drift(prior, current)[0]
    assert check.status == "consistent"
    assert "removed: Ledgerhost SAS" in check.found
    assert "moved: Hostline GmbH (Germany → Netherlands)" in check.found


def test_no_list_on_either_side_means_no_drift(samples):
    # CTR-2001 (an MSA with no annex at all) against itself: there
    # is no list to diff, and the single-round checks own absence.
    msa = samples["CTR-2001"]
    assert subprocessor_list_entries(msa) == []
    assert build_subprocessor_drift(msa, msa) == []


# ---------------------------------------------------------------------------
# Through the service: attached to the follow-up round's result
# ---------------------------------------------------------------------------


def test_service_attaches_drift_to_the_follow_up_round(samples):
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    first = svc.analyze(_round(samples, "CTR-DRIFT-1", "2026-10-05T10:00:00", None))
    assert first.subprocessor_drift == []  # no prior round, no drift
    second = svc.analyze(_round(
        samples, "CTR-DRIFT-2", "2026-10-15T10:00:00", _ADDED_ANNEX,
        supersedes="CTR-DRIFT-1",
    ))
    assert len(second.subprocessor_drift) == 1
    assert second.subprocessor_drift[0].status == "mismatch"
    assert any(
        step.name == "subprocessor_drift" for step in second.trace
    )
    # The single-round checks are untouched by the cross-round one.
    assert all(
        c.name != "subprocessor_list_drift" for c in second.exhibit_checks
    )


def test_service_no_drift_when_the_prior_round_is_not_stored(samples):
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    result = svc.analyze(_round(
        samples, "CTR-DRIFT-2", "2026-10-15T10:00:00", _ADDED_ANNEX,
        supersedes="CTR-DRIFT-404",
    ))
    assert result.subprocessor_drift == []


def test_brief_carries_the_drift_row(samples):
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(_round(samples, "CTR-DRIFT-1", "2026-10-05T10:00:00", None))
    svc.analyze(_round(
        samples, "CTR-DRIFT-2", "2026-10-15T10:00:00", _ADDED_ANNEX,
        supersedes="CTR-DRIFT-1",
    ))
    md = svc.negotiation_brief("CTR-DRIFT-2")["markdown"]
    assert "subprocessor_list_drift" in md
    assert "Cloudmetrics Inc" in md
    assert "cross-round" in md
