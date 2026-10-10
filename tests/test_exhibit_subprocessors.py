"""Sub-processor list exhibit checks (round 10): a DPA's own
annex — the list of sub-processors the processor engages — is
checked against the data-processing terms that govern it. The
clause promises a notice period for changes; the annex that
maintains the list must carry it. The clause authorises named
sub-processors; a list engaging anyone else has an undeclared
sub-processor in it. The transfer clause confines processing to a
region; no listed processor may sit outside it."""

from __future__ import annotations

import pytest

from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput, DocumentInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore
from helpers import contract


@pytest.fixture(scope="module")
def samples() -> dict:
    return {c.contract_id: c for c in sample_contract_models()}


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


def _checks(result) -> dict:
    return {c.name: c for c in result.exhibit_checks}


_DPA = """1. Data Protection and Security
The Processor shall implement technical and organisational security measures appropriate to the risk of the processing, and shall protect personal data in accordance with this Data Processing Addendum. The Processor shall notify the Controller within 72 hours of becoming aware of a personal data breach.

2. Sub-processors
The sub-processors authorised by the Controller under this Addendum are Hostline GmbH and Postmark Systems Ltd. The Processor shall give the Controller at least thirty (30) days' prior written notice of any intended addition or replacement of a sub-processor, and the Controller may object in writing within thirty (30) days of that notice.

3. International Transfers
The Processor shall not transfer personal data outside the European Economic Area. All processing under this Addendum takes place within the European Economic Area.
"""


def _dpa_with_annex(annex_text: str, agreement: str = _DPA) -> ContractInput:
    return contract(
        agreement,
        contract_id="CTR-SUB-X",
        doc_type="dpa",
        title="Data Processing Addendum",
        documents=[
            DocumentInput(doc_type="agreement", document_id="AGR-1", raw_text=agreement),
            DocumentInput(doc_type="exhibit", document_id="ANNEX-1", raw_text=annex_text),
        ],
    )


# ---------------------------------------------------------------------------
# The clean sample (CTR-2031): the annex honours the DPA's own terms
# ---------------------------------------------------------------------------


def test_clean_annex_is_consistent_on_all_three_checks(samples):
    result = _service().analyze(samples["CTR-2031"])
    checks = _checks(result)
    assert set(checks) == {
        "subprocessor_notice_period",
        "subprocessor_authorised_list",
        "subprocessor_transfer_terms",
    }
    assert all(c.status == "consistent" for c in checks.values())
    assert result.facts.posture == "aligned"
    assert checks["subprocessor_notice_period"].found == "30 days"
    assert checks["subprocessor_notice_period"].exhibit_document_id == "ANNEX-1"


# ---------------------------------------------------------------------------
# The adversarial sample (CTR-2030): the annex breaks the DPA's terms
# ---------------------------------------------------------------------------


def test_annex_with_undeclared_processor_outside_the_region_fails(samples):
    result = _service().analyze(samples["CTR-2030"])
    checks = _checks(result)
    assert len(checks) == 3
    # No notice period anywhere in the annex: the clause's promised
    # thirty days has no machinery in the document that changes.
    notice = checks["subprocessor_notice_period"]
    assert notice.status == "missing_in_exhibit"
    assert "30" in notice.detail
    # Cloudmetrics Inc is engaged by the list but named nowhere in
    # the clause's authorisation — undeclared.
    authorised = checks["subprocessor_authorised_list"]
    assert authorised.status == "mismatch"
    assert authorised.found == "Cloudmetrics Inc"
    assert "Hostline GmbH" in authorised.promised
    # …and it sits in the United States, outside the EEA the
    # transfer clause confines processing to.
    transfer = checks["subprocessor_transfer_terms"]
    assert transfer.status == "mismatch"
    assert "Cloudmetrics Inc (United States)" in transfer.found
    assert result.facts.posture == "negotiable"


# ---------------------------------------------------------------------------
# The individual rules, on hand-built packages
# ---------------------------------------------------------------------------


def test_annex_notice_shorter_than_promised_is_a_mismatch():
    annex = (
        "Annex 1 — Sub-processor List\n\n"
        "The Processor engages the following sub-processors in the processing:\n"
        "- Hostline GmbH (Germany) — data hosting\n"
        "- Postmark Systems Ltd (Ireland) — email delivery\n"
        "The Processor shall give at least ten (10) days' written notice "
        "before any change to this list takes effect.\n"
    )
    checks = _checks(_service().analyze(_dpa_with_annex(annex)))
    notice = checks["subprocessor_notice_period"]
    assert notice.status == "mismatch"
    assert notice.found == "10 days"
    assert notice.promised.startswith("30 days")


def test_no_authorised_set_in_clause_means_no_authorised_check():
    # The clause promises notice machinery but authorises no named
    # set — there is no set for the list to be checked against, and
    # the family says so by generating no check, not by inventing one.
    agreement = _DPA.replace(
        "The sub-processors authorised by the Controller under this "
        "Addendum are Hostline GmbH and Postmark Systems Ltd. ", ""
    )
    annex = (
        "Annex 1 — Sub-processor List\n\n"
        "The Processor engages the following sub-processors in the processing:\n"
        "- Hostline GmbH (Germany) — data hosting\n"
        "- Postmark Systems Ltd (Ireland) — email delivery\n"
        "The Processor shall give at least thirty (30) days' written "
        "notice before any change to this list takes effect.\n"
    )
    checks = _checks(_service().analyze(_dpa_with_annex(annex, agreement)))
    assert "subprocessor_authorised_list" not in checks
    assert checks["subprocessor_notice_period"].status == "consistent"


def test_list_without_locations_cannot_be_checked_against_the_region():
    annex = (
        "Annex 1 — Sub-processor List\n\n"
        "The Processor engages the following sub-processors in the processing:\n"
        "- Hostline GmbH — data hosting\n"
        "- Postmark Systems Ltd — email delivery\n"
        "The Processor shall give at least thirty (30) days' written "
        "notice before any change to this list takes effect.\n"
    )
    checks = _checks(_service().analyze(_dpa_with_annex(annex)))
    transfer = checks["subprocessor_transfer_terms"]
    assert transfer.status == "missing_in_exhibit"
    assert "location" in transfer.detail


def test_no_list_annex_no_family(samples):
    # A DPA with no sub-processor list attached generates none of
    # the family — there is no list to hold against the terms.
    result = _service().analyze(samples["CTR-2023"])
    assert not any(
        name.startswith("subprocessor_") and name != "subprocessor_list"
        for name in _checks(result)
    )
