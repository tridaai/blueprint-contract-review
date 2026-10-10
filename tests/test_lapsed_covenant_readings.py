"""Lapsed-covenant readings on later letters (round 17): the
register knows a prior letter's covenant lapsed, and since
round 16 whether the lapse was closed. A LATER letter in the
chain carrying a fresh covenant of the same kind is read
against that history at analysis time: the prior round and
its expiry are named, the lapse's evidence is named
(confirmed lifted, notified only, still open), and the new
covenant is read as the NEW bargain it is — a fresh promise
priced on its own terms — not a continuation of the
restriction that lapsed. A prior covenant still running draws
no reading, and neither does a letter carrying no fresh
covenant of the kind.
"""

from __future__ import annotations

from datetime import date

import pytest

from contract_agent.covenant_history import build_lapsed_covenant_readings
from contract_agent.obligations import Obligation
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore

AFTER_LAPSE = date(2025, 7, 15)  # after the 2025-06-30 expiry


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def _letter(
    samples,
    contract_id: str,
    *,
    termination: str | None = None,
    supersedes: str = "",
) -> ContractInput:
    """CTR-2025 (both covenants at 12 months) renamed, with an
    optional stated termination date and chain link."""
    data = samples["CTR-2025"].model_dump(mode="json")
    data["contract_id"] = contract_id
    data["supersedes"] = supersedes
    if termination:
        for doc in data["documents"]:
            if doc["doc_type"] == "agreement":
                doc["raw_text"] += (
                    "\n\n5. Fixed Term\n"
                    "The parties record that the employment terminates "
                    f"on {termination}, when the programme of work "
                    "concludes.\n"
                )
    return ContractInput.model_validate(data)


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


def _lapsed_chain(samples) -> ContractService:
    """Letter A terminated 2024-06-30 — its covenants expired
    2025-06-30, lapsed by any reading today — in the store."""
    svc = _service()
    svc.analyze(_letter(samples, "CTR-LAPSE-A", termination="June 30, 2024"))
    return svc


# ---------------------------------------------------------------------------
# The reading on the later letter
# ---------------------------------------------------------------------------


def test_a_later_letter_reads_the_lapsed_covenants(samples):
    svc = _lapsed_chain(samples)
    result = svc.analyze(_letter(samples, "CTR-LAPSE-B", supersedes="CTR-LAPSE-A"))
    readings = result.lapsed_covenant_readings
    assert len(readings) == 2
    assert {r.found for r in readings} == {
        "lapse state on the record: open"
    }
    for reading in readings:
        assert reading.name == "prior_covenant_lapsed"
        assert reading.status == "consistent"
        assert "CTR-LAPSE-A" in reading.detail
        assert "2025-06-30" in reading.detail
        assert "NEW bargain" in reading.detail
        assert "stands open on the record" in reading.detail
    steps = [s for s in result.trace if s.name == "prior_covenant_lapsed"]
    assert len(steps) == 2


def test_the_reading_names_a_confirmed_lapse(samples):
    svc = _lapsed_chain(samples)
    for kind in ("non_compete", "non_solicit"):
        svc.record_covenant_follow_through(
            "CTR-LAPSE-A", kind, "counterparty_notified",
            actor="A. Counsel", today=AFTER_LAPSE,
        )
        svc.record_covenant_follow_through(
            "CTR-LAPSE-A", kind, "restriction_confirmed_lifted",
            actor="A. Counsel", today=AFTER_LAPSE,
        )
    result = svc.analyze(_letter(samples, "CTR-LAPSE-B", supersedes="CTR-LAPSE-A"))
    assert len(result.lapsed_covenant_readings) == 2
    for reading in result.lapsed_covenant_readings:
        assert reading.found == (
            "lapse state on the record: restriction_confirmed_lifted"
        )
        assert "confirmed lifted on the record" in reading.detail
        assert "A. Counsel" in reading.detail


def test_the_reading_names_a_notified_but_unconfirmed_lapse(samples):
    svc = _lapsed_chain(samples)
    svc.record_covenant_follow_through(
        "CTR-LAPSE-A", "non_compete", "counterparty_notified",
        actor="A. Counsel", today=AFTER_LAPSE,
    )
    result = svc.analyze(_letter(samples, "CTR-LAPSE-B", supersedes="CTR-LAPSE-A"))
    by_expectation = {r.expectation: r for r in result.lapsed_covenant_readings}
    compete = next(
        r for r in result.lapsed_covenant_readings
        if "non-compete" in r.expectation
    )
    assert compete.found == "lapse state on the record: counterparty_notified"
    assert "not confirmed on the record" in compete.detail
    solicit = next(
        r for r in result.lapsed_covenant_readings
        if "non-solicitation" in r.expectation
    )
    assert solicit.found == "lapse state on the record: open"
    assert by_expectation  # both kinds read


def test_the_reading_rides_the_negotiation_brief(samples):
    svc = _lapsed_chain(samples)
    svc.analyze(_letter(samples, "CTR-LAPSE-B", supersedes="CTR-LAPSE-A"))
    brief = svc.negotiation_brief("CTR-LAPSE-B")
    assert "prior_covenant_lapsed" in brief["markdown"]
    assert "NEW bargain" in brief["markdown"]


# ---------------------------------------------------------------------------
# The gates: no lapse, no fresh covenant, no chain — no reading
# ---------------------------------------------------------------------------


def test_no_chain_no_reading(samples):
    svc = _service()
    result = svc.analyze(_letter(samples, "CTR-LAPSE-SOLO"))
    assert result.lapsed_covenant_readings == []


def test_a_running_prior_covenant_draws_no_reading(samples):
    svc = _service()
    # Termination 2027-06-30: the covenants run to 2028-06-30,
    # still live restrictions today — a continuation question,
    # not a lapse.
    svc.analyze(_letter(samples, "CTR-LIVE-A", termination="June 30, 2027"))
    result = svc.analyze(_letter(samples, "CTR-LIVE-B", supersedes="CTR-LIVE-A"))
    assert result.lapsed_covenant_readings == []


def test_a_letter_without_fresh_covenants_draws_no_reading(samples):
    svc = _lapsed_chain(samples)
    data = samples["CTR-2029"].model_dump(mode="json")
    data["supersedes"] = "CTR-LAPSE-A"
    result = svc.analyze(ContractInput.model_validate(data))
    assert result.lapsed_covenant_readings == []


# ---------------------------------------------------------------------------
# The pure reading: the nearest dated prior governs
# ---------------------------------------------------------------------------


def _obligation(kind: str, date_: str | None) -> Obligation:
    return Obligation(
        kind=kind, description="", detail="12 months following termination",
        date=date_, source_clause_id="c1", source_ref="§1",
    )


def test_the_nearest_dated_prior_governs():
    fresh = [_obligation("non_compete", None)]
    older = ("CTR-OLD", [_obligation("non_compete", "2020-06-30")], [])
    nearer_running = ("CTR-NEAR", [_obligation("non_compete", "2999-06-30")], [])
    # The nearest dated prior covenant is still running: the
    # older lapse behind it is not the operative history.
    assert build_lapsed_covenant_readings(
        [older, nearer_running], fresh, date(2026, 1, 1)
    ) == []
    nearer_lapsed = ("CTR-NEAR", [_obligation("non_compete", "2025-06-30")], [])
    readings = build_lapsed_covenant_readings(
        [older, nearer_lapsed], fresh, date(2026, 1, 1)
    )
    assert len(readings) == 1
    assert "CTR-NEAR" in readings[0].detail
    assert "2025-06-30" in readings[0].detail


def test_an_undated_prior_is_no_baseline():
    fresh = [_obligation("non_compete", None)]
    undated = ("CTR-U", [_obligation("non_compete", None)], [])
    assert build_lapsed_covenant_readings(
        [undated], fresh, date(2026, 1, 1)
    ) == []
