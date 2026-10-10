"""Commercial restrictive covenants (round 18): the covenant
clocks were scoped to employment letters — a services
agreement's restrictions never became register entries, so
nothing dated them, announced their lapse, or closed their
follow-through. Now a commercial agreement's covenants join
the same register: an exclusivity appointment, a
non-circumvention covenant, and a commercial non-compete /
non-solicit clause all register as covenant clocks running
from the agreement's own termination machinery — dated in
code from the end date the agreement states for itself,
undated with the anchor stated while it states none. The
word alone is not a covenant: a non-exclusive licence and an
exclusive-jurisdiction clause register nothing.
"""

from __future__ import annotations

from datetime import date

import pytest

from contract_agent.events import CollectingSink
from contract_agent.graph import run_contract
from contract_agent.model_backends import MockModelBackend
from contract_agent.obligations import build_obligations
from contract_agent.retriever import KeywordRetriever
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ClauseExtraction, ClauseUnit, ContractInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore

COVENANT_KINDS = ("non_compete", "non_solicit", "exclusivity", "non_circumvention")


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def unit(clause_id: str, text: str, ref: str = "1") -> ClauseUnit:
    return ClauseUnit(clause_id=clause_id, ref=ref, text=text)


def extraction(clause_id: str, clause_type: str, signals: dict) -> ClauseExtraction:
    return ClauseExtraction(clause_id=clause_id, clause_type=clause_type, signals=signals)


def _run(contract: ContractInput):
    return run_contract(
        contract, backend=MockModelBackend(), retriever=KeywordRetriever()
    )


def _covenants(obligations) -> dict:
    return {o.kind: o for o in obligations if o.kind in COVENANT_KINDS}


# ---------------------------------------------------------------------------
# The samples: both commercial covenants clocked off the stated anchor
# ---------------------------------------------------------------------------


def test_ctr2038_commercial_covenants_are_dated_from_the_stated_expiry(samples):
    result = _run(samples["CTR-2038"])
    covenants = _covenants(result.obligations)
    assert set(covenants) == {"exclusivity", "non_circumvention"}
    exclusivity = covenants["exclusivity"]
    assert exclusivity.detail == "12 months following termination of the agreement"
    # The agreement states its own expiry — 2026-12-31 — and the
    # covenant clock runs from that anchor, computed in code.
    assert exclusivity.date == "2027-12-31"
    assert exclusivity.computed is True
    assert "2026-12-31" in exclusivity.description
    assert "anchor" in exclusivity.description
    circumvention = covenants["non_circumvention"]
    assert circumvention.detail == "24 months following termination of the agreement"
    assert circumvention.date == "2028-12-31"
    assert circumvention.computed is True


def test_ctr2039_amendment_shortens_the_exclusivity_tail(samples):
    result = _run(samples["CTR-2039"])
    covenants = _covenants(result.obligations)
    assert set(covenants) == {"exclusivity"}
    assert covenants["exclusivity"].detail == (
        "6 months following termination of the agreement"
    )
    assert covenants["exclusivity"].date == "2027-06-30"


def test_no_existing_commercial_sample_gains_a_phantom_covenant(samples):
    # The boilerplate guard, over the whole commercial set: a
    # non-exclusive licence and an exclusive-jurisdiction
    # clause are not covenants, and no commercial sample in the
    # set states one — so none registers one.
    for contract in samples.values():
        if contract.doc_type == "employment":
            continue
        result = _run(contract)
        if contract.contract_id in ("CTR-2038", "CTR-2039"):
            continue
        assert _covenants(result.obligations) == {}, contract.contract_id


# ---------------------------------------------------------------------------
# Unit level: the anchor, stated and unstated
# ---------------------------------------------------------------------------


def _commercial_units(*, stated_end: bool) -> list[ClauseUnit]:
    units = [
        unit("C-01", (
            "During the Term, the Provider shall be the exclusive "
            "provider of hosting services to the Customer. This "
            "exclusivity shall continue for twelve (12) months "
            "following the termination or expiry of this Agreement."
        )),
        unit("C-02", (
            "Each party agrees not to circumvent the other in "
            "respect of introductions made under this Agreement. "
            "This non-circumvention covenant shall run for "
            "twenty-four (24) months following the expiry of "
            "this Agreement."
        ), ref="2"),
    ]
    if stated_end:
        units.append(unit("C-03", (
            "This Agreement takes effect on the Effective Date "
            "and expires on December 31, 2026, unless terminated "
            "earlier in accordance with this clause."
        ), ref="3"))
    return units


def test_commercial_covenants_date_from_the_agreements_own_expiry():
    obligations = build_obligations(_commercial_units(stated_end=True), [])
    covenants = _covenants(obligations)
    assert covenants["exclusivity"].date == "2027-12-31"
    assert covenants["exclusivity"].computed is True
    assert covenants["non_circumvention"].date == "2028-12-31"


def test_commercial_covenants_are_undated_with_the_anchor_stated():
    obligations = build_obligations(_commercial_units(stated_end=False), [])
    covenants = _covenants(obligations)
    assert set(covenants) == {"exclusivity", "non_circumvention"}
    for entry in covenants.values():
        # No stated end: the clock is anchored to the event,
        # and the entry says so rather than inventing a date.
        assert entry.date is None
        assert entry.computed is False
        assert "anchor" in entry.description
        assert "undated until" in entry.description


def test_the_word_alone_is_not_a_covenant():
    units = [
        unit("C-01", (
            "The Customer receives a non-exclusive licence to use "
            "the Deliverables for twelve (12) months following "
            "termination of this Agreement."
        )),
        unit("C-02", (
            "This Agreement is governed by the laws of the State "
            "of Delaware, and the parties consent to the exclusive "
            "jurisdiction of the courts located in Delaware for "
            "twenty-four (24) months following termination."
        ), ref="2"),
    ]
    assert _covenants(build_obligations(units, [])) == {}


def test_commercial_non_compete_clause_registers_on_the_commercial_anchor():
    units = [
        unit("C-01", (
            "For eighteen (18) months following the end of the "
            "Term, the Provider shall not compete with the "
            "Customer in the hosting market. The Provider also "
            "agrees not to solicit the Customer's staff for six "
            "(6) months following the end of the Term."
        )),
        unit("C-02", (
            "This Agreement expires on June 30, 2027, unless "
            "terminated earlier in accordance with this clause."
        ), ref="2"),
    ]
    extractions = [extraction("C-01", "non_compete", {"duration_months": 18})]
    covenants = _covenants(build_obligations(units, extractions))
    assert covenants["non_compete"].detail == (
        "18 months following termination of the agreement"
    )
    assert covenants["non_compete"].date == "2028-12-30"
    assert covenants["non_solicit"].detail == (
        "6 months following termination of the agreement"
    )
    assert covenants["non_solicit"].date == "2027-12-30"


def test_a_mistyped_clause_names_no_phantom_covenant():
    # A clause the classifier typed non_compete whose text
    # names no covenant: the durations helper's signals
    # fallback must not mint a covenant the words never made.
    units = [
        unit("C-01", (
            "The Provider shall maintain the hosting platform "
            "for twelve (12) months following the Effective Date."
        )),
    ]
    extractions = [extraction("C-01", "non_compete", {"duration_months": 12})]
    assert _covenants(build_obligations(units, extractions)) == {}


# ---------------------------------------------------------------------------
# The machinery: due view, follow-through, the sweep
# ---------------------------------------------------------------------------


def _service_with(contract: ContractInput) -> ContractService:
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(contract)
    return svc


def test_commercial_covenant_joins_the_due_view_with_its_follow_through(samples):
    svc = _service_with(samples["CTR-2038"])
    due = svc.obligations_due(days=400, today=date(2028, 1, 15))
    rows = {
        row["kind"]: row
        for row in due["obligations"]
        if row["contract_id"] == "CTR-2038" and row["kind"] in COVENANT_KINDS
    }
    assert set(rows) == {"exclusivity", "non_circumvention"}
    # The exclusivity lapsed on 2027-12-31 and nobody closed
    # the loop: it reads open, never as a live restriction.
    assert rows["exclusivity"]["lapsed"] is True
    assert rows["exclusivity"]["follow_through_state"] == "open"
    # The non-circumvention covenant still runs to 2028-12-31.
    assert rows["non_circumvention"]["lapsed"] is False
    assert rows["non_circumvention"]["follow_through_state"] == "pending_lapse"


def test_commercial_covenant_takes_the_follow_through_acts(samples):
    svc = _service_with(samples["CTR-2038"])
    view = svc.record_covenant_follow_through(
        "CTR-2038", "exclusivity", "counterparty_notified",
        "Counsel — A. Reyes", today=date(2028, 1, 15),
    )
    row = next(r for r in view["covenants"] if r["kind"] == "exclusivity")
    assert row["follow_through_state"] == "counterparty_notified"
    view = svc.record_covenant_follow_through(
        "CTR-2038", "exclusivity", "restriction_confirmed_lifted",
        "Counsel — A. Reyes", today=date(2028, 1, 16),
    )
    row = next(r for r in view["covenants"] if r["kind"] == "exclusivity")
    assert row["follow_through_state"] == "restriction_confirmed_lifted"
    assert view["open_lapses"] == 0


def test_commercial_covenant_is_refused_before_its_lapse(samples):
    svc = _service_with(samples["CTR-2038"])
    with pytest.raises(ValueError, match="does not lapse until 2028-12-31"):
        svc.record_covenant_follow_through(
            "CTR-2038", "non_circumvention", "counterparty_notified",
            "Counsel — A. Reyes", today=date(2028, 1, 15),
        )


def test_the_sweep_announces_a_commercial_covenant_lapse(samples):
    svc = _service_with(samples["CTR-2038"])
    sink = CollectingSink()
    report = svc.dispatch_obligation_sweep(
        days=400, today=date(2028, 1, 15), event_sink=sink
    )
    events = [
        e for e in sink.events
        if e.type == "covenant_expiring" and e.contract_id == "CTR-2038"
    ]
    assert {e.detail["kind"] for e in events} == {
        "exclusivity", "non_circumvention",
    }
    assert all(e.detail["effect"] == "restriction_lapses" for e in events)
    # Once the exclusivity's lapse stands confirmed, the sweep
    # counts it closed and stops announcing it — the same
    # discipline as the employment covenants.
    svc.record_covenant_follow_through(
        "CTR-2038", "exclusivity", "restriction_confirmed_lifted",
        "Counsel — A. Reyes", today=date(2028, 1, 15),
    )
    sink = CollectingSink()
    report = svc.dispatch_obligation_sweep(
        days=400, today=date(2028, 1, 20), event_sink=sink
    )
    assert report["covenant_closed_count"] == 1
    events = [
        e for e in sink.events
        if e.type == "covenant_expiring" and e.contract_id == "CTR-2038"
    ]
    assert {e.detail["kind"] for e in events} == {"non_circumvention"}
