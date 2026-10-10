"""SOW-vs-MSA covenant conflicts (round 19): a statement of
work lives under a master agreement, and both can carry
restrictive covenants — the conflict exists only BETWEEN the
documents. When the SOW's own text names its master and the
master is in the store, the SOW's covenant register is read
against the master's: a duration or scope conflict is flagged
with both sources named, the stated order of precedence read
in code where a document states one; an aligned, incorporated,
or SOW-only covenant is a consistent reading, on record
because it was checked. An SOW naming no master — or one the
store has never seen — draws no checks: there is no second
document to conflict with, and inventing one would be worse
than silence.
"""

from __future__ import annotations

import pytest

from contract_agent.objectstore import InMemoryObjectStore
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput, DocumentInput
from contract_agent.service import ContractService
from contract_agent.sow_covenants import (
    master_agreement_id,
    order_of_precedence,
)
from contract_agent.store import InMemoryStore
from helpers import contract


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def _service() -> ContractService:
    return ContractService(
        store=InMemoryStore(),
        checkpointer=False,
        object_store=InMemoryObjectStore(),
    )


def _by_expectation(result) -> dict[str, object]:
    return {
        check.expectation: check for check in result.sow_covenant_conflicts
    }


EXP_EXCLUSIVITY = "the exclusivity covenant under master agreement CTR-2040"
EXP_COMPETE = "the non-compete covenant under master agreement CTR-2040"
EXP_CIRC = "the non-circumvention covenant under master agreement CTR-2040"


# ---------------------------------------------------------------------------
# CTR-2041: the conflicting SOW
# ---------------------------------------------------------------------------


def test_the_conflicting_sow_flags_both_conflicts(samples):
    svc = _service()
    svc.analyze(samples["CTR-2040"])
    result = svc.analyze(samples["CTR-2041"])
    checks = _by_expectation(result)
    assert set(checks) == {EXP_EXCLUSIVITY, EXP_COMPETE}
    assert all(c.name == "sow_covenant_conflict" for c in checks.values())
    assert all(c.status == "mismatch" for c in checks.values())


def test_the_exclusivity_conflict_names_duration_scope_and_both_sources(samples):
    svc = _service()
    svc.analyze(samples["CTR-2040"])
    result = svc.analyze(samples["CTR-2041"])
    check = _by_expectation(result)[EXP_EXCLUSIVITY]
    # Both durations, both sources (contract + clause ref).
    assert check.promised.startswith(
        "12 months following termination of the agreement"
    )
    assert "CTR-2040 7" in check.promised
    assert "24 months following termination of the agreement" in check.found
    assert "CTR-2041 2" in check.found
    assert "duration" in check.detail and "scope" in check.detail
    assert "managed warehouse automation and facilities monitoring" in check.detail
    assert "managed warehouse automation services" in check.detail


def test_the_non_compete_conflict_is_a_duration_conflict(samples):
    svc = _service()
    svc.analyze(samples["CTR-2040"])
    result = svc.analyze(samples["CTR-2041"])
    check = _by_expectation(result)[EXP_COMPETE]
    assert check.status == "mismatch"
    assert "6 months" in check.detail and "12 months" in check.detail
    assert "CTR-2040 6" in check.detail and "CTR-2041 3" in check.detail


def test_the_masters_stated_precedence_is_read_in_code(samples):
    svc = _service()
    svc.analyze(samples["CTR-2040"])
    result = svc.analyze(samples["CTR-2041"])
    for check in result.sow_covenant_conflicts:
        # CTR-2040 §8: "this Agreement shall control" — stated in
        # the master, naming the master as governing.
        assert "Order of precedence: CTR-2040 states" in check.detail
        assert "the master agreement controls" in check.detail


def test_the_master_itself_draws_no_checks(samples):
    svc = _service()
    result = svc.analyze(samples["CTR-2040"])
    assert result.sow_covenant_conflicts == []


# ---------------------------------------------------------------------------
# CTR-2042: the aligned SOW — consistent readings, gap included
# ---------------------------------------------------------------------------


def test_the_aligned_sow_reads_consistent(samples):
    svc = _service()
    svc.analyze(samples["CTR-2040"])
    result = svc.analyze(samples["CTR-2042"])
    checks = _by_expectation(result)
    assert set(checks) == {EXP_EXCLUSIVITY, EXP_COMPETE, EXP_CIRC}
    assert all(c.status == "consistent" for c in checks.values())
    # The master-silent kind is information, not a conflict:
    # the SOW's covenant stands on its own terms.
    circ = checks[EXP_CIRC]
    assert "states no non-circumvention covenant" in circ.promised
    assert "18 months" in circ.found
    assert "does not contradict" in circ.detail


# ---------------------------------------------------------------------------
# Linkage discipline: stated, stored — or silence
# ---------------------------------------------------------------------------


def test_an_sow_naming_no_master_draws_no_checks(samples):
    svc = _service()
    svc.analyze(samples["CTR-2040"])
    unnamed = contract(
        doc_type="sow",
        contract_id="CTR-2090",
        documents=[DocumentInput(
            doc_type="agreement", document_id="AGR-1",
            raw_text=(
                "1. Exclusivity\nThe Provider shall be the "
                "exclusive provider of managed warehouse "
                "automation services to the Customer. This "
                "exclusivity shall continue for twenty-four (24) "
                "months following termination."
            ),
        )],
    )
    result = svc.analyze(unnamed)
    assert result.sow_covenant_conflicts == []


def test_an_sow_naming_an_unseen_master_draws_no_checks(samples):
    svc = _service()  # CTR-2040 never analyzed: not in the store
    result = svc.analyze(samples["CTR-2041"])
    assert result.sow_covenant_conflicts == []


def test_a_non_sow_draws_no_checks(samples):
    svc = _service()
    svc.analyze(samples["CTR-2040"])
    amendment = contract(
        doc_type="amendment",
        contract_id="CTR-2091",
        documents=[DocumentInput(
            doc_type="agreement", document_id="AGR-1",
            raw_text=(
                "1. Recitals\nThis Amendment amends the Master "
                "Services Agreement (CTR-2040) between the parties."
            ),
        )],
    )
    result = svc.analyze(amendment)
    assert result.sow_covenant_conflicts == []


# ---------------------------------------------------------------------------
# The readers: linkage and precedence, as units
# ---------------------------------------------------------------------------


def _doc(contract_id: str, text: str, doc_type: str = "sow") -> ContractInput:
    return contract(
        contract_id=contract_id,
        doc_type=doc_type,
        documents=[DocumentInput(
            doc_type="agreement", document_id="AGR-1", raw_text=text
        )],
    )


def test_master_reference_must_be_stated_with_an_id():
    assert master_agreement_id(
        "under that certain Master Services Agreement (CTR-2040) "
        "between the parties"
    ) == "CTR-2040"
    assert master_agreement_id(
        "under the Master Services Agreement between the parties"
    ) == ""
    assert master_agreement_id(
        "The fee schedule (CTR-2040) is attached."
    ) == ""


def test_precedence_reads_the_control_clause_not_any_mention():
    master = _doc(
        "CTR-M1",
        "In the event of any conflict between this Agreement and "
        "any statement of work entered into under it, this "
        "Agreement shall control.",
        doc_type="msa",
    )
    sow = _doc("CTR-S1", "Services under the Master Agreement (CTR-M1).")
    assert order_of_precedence(sow, master) == ("master", "CTR-M1")
    sow_first = _doc(
        "CTR-S2",
        "Services under the Master Agreement (CTR-M1). In the "
        "event of a conflict, this Statement of Work shall "
        "prevail.",
    )
    assert order_of_precedence(sow_first, master) == ("sow", "CTR-S2")
    silent = _doc("CTR-M2", "Fees are Net 30.", doc_type="msa")
    assert order_of_precedence(sow, silent) == ("", "")


def test_no_stated_precedence_is_itself_the_reading(samples):
    # Neither CTR-2040's precedence (master-first) nor a conflict
    # is universal: an SOW-stated precedence for itself flips the
    # sentence, and silence leaves the call to counsel.
    svc = _service()
    svc.analyze(samples["CTR-2040"])
    sow = _doc(
        "CTR-2092",
        "1. Services\nThis Statement of Work is entered into "
        "under that certain Master Services Agreement (CTR-2040) "
        "between the parties. In the event of a conflict, this "
        "Statement of Work shall prevail.\n\n2. Exclusivity\n"
        "The Provider shall be the exclusive provider of "
        "managed warehouse automation services to the Customer. "
        "This exclusivity shall continue for thirty-six (36) "
        "months following the termination of the Master Agreement.",
    )
    result = svc.analyze(sow)
    check = _by_expectation(result)[EXP_EXCLUSIVITY]
    assert check.status == "mismatch"
    assert "Order of precedence: CTR-2092 states" in check.detail
    assert "the statement of work controls" in check.detail


# ---------------------------------------------------------------------------
# The brief carries the readings
# ---------------------------------------------------------------------------


def test_the_brief_names_the_conflicts(samples):
    svc = _service()
    svc.analyze(samples["CTR-2040"])
    svc.analyze(samples["CTR-2041"])
    brief = svc.negotiation_brief("CTR-2041")
    assert "sow_covenant_conflict" in brief["markdown"]
    assert "Covenant conflict (duration" in brief["markdown"]
    assert "Order of precedence: CTR-2040 states" in brief["markdown"]


def test_the_brief_delta_counts_the_conflicts(samples):
    svc = _service()
    svc.analyze(samples["CTR-2040"])
    svc.analyze(samples["CTR-2041"])
    svc.negotiation_brief("CTR-2041")
    # The SOW is reissued aligned to the master: the conflicts
    # close, and the next brief's delta says the readings moved.
    aligned = samples["CTR-2042"].model_copy(
        update={"contract_id": "CTR-2043", "supersedes": "CTR-2041"}
    )
    svc.analyze(aligned)
    brief = svc.negotiation_brief("CTR-2043")
    assert any(
        "SOW covenants read against the master agreement: "
        "0 conflict(s) across the two documents" in line
        for line in brief["delta_lines"]
    )
