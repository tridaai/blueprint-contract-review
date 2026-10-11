"""The sub-processor exhibit (this round): the single-round
checks read one package's list against its own terms, but no
surface assembled the matter's lists — every sub-processor the
matter's documents engage, as each document states it, with
its lineage. The exhibit assembles them in code, carries
mentions that do not structure as UNPARSED, and flags the same
processor stated two ways across documents — a computed
comparison with both sources cited, and no conclusion drawn."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from contract_agent.api import app
from contract_agent.schemas import DocumentInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore
from contract_agent.subprocessor_exhibit import (
    build_subprocessor_exhibit,
    render_subprocessor_exhibit_text,
)
from helpers import contract

_DPA = """1. Data Protection and Security
The Processor shall implement technical and organisational security measures appropriate to the risk of the processing, and shall protect personal data in accordance with this Data Processing Addendum. The Processor shall notify the Controller within 72 hours of becoming aware of a personal data breach.

2. Sub-processors
The sub-processors authorised by the Controller under this Addendum are Hostline GmbH and Postmark Systems Ltd. The Processor shall give the Controller at least thirty (30) days' prior written notice of any intended addition or replacement of a sub-processor, and the Controller may object in writing within thirty (30) days of that notice.

3. International Transfers
The Processor shall not transfer personal data outside the European Economic Area. All processing under this Addendum takes place within the European Economic Area.
"""

_COUNTERPARTY = "Exhibit Matter Co"
_MATTER = "Platform DPA matter"

_ANNEX_A = (
    "Annex 1 — Sub-processor List\n\n"
    "The Processor engages the following sub-processors in the processing:\n"
    "- Hostline GmbH (Germany) — data hosting\n"
    "- Postmark Systems Ltd (Ireland) — email delivery\n"
    "- Cloudmetrics Inc (United States) — product analytics\n"
    "The Processor shall give at least thirty (30) days' written notice "
    "before any change to this list takes effect.\n"
)

_ANNEX_B = (
    "Annex 1 — Sub-processor List\n\n"
    "The Processor engages the following sub-processors in the processing:\n"
    "- Hostline GmbH (Germany) — data hosting\n"
    "- Postmark Systems Ltd (Ireland) — email delivery\n"
    "- Cloudmetrics Inc (United States) — usage analytics\n"
    "The Processor also engages Brightdata Analytics Inc for support telemetry.\n"
    "The Processor shall give at least thirty (30) days' written notice "
    "before any change to this list takes effect.\n"
)


def _dpa(contract_id: str, annex: str, **overrides):
    base = dict(
        contract_id=contract_id,
        doc_type="dpa",
        title="Data Processing Addendum",
        counterparty_name=_COUNTERPARTY,
        matter=_MATTER,
        documents=[
            DocumentInput(doc_type="agreement", document_id="AGR-1", raw_text=_DPA),
            DocumentInput(doc_type="exhibit", document_id="ANNEX-1", raw_text=annex),
        ],
    )
    base.update(overrides)
    return contract(_DPA, **base)


def _service_with_matter() -> ContractService:
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(_dpa("CTR-SP-A", _ANNEX_A))
    svc.analyze(_dpa("CTR-SP-B", _ANNEX_B))
    return svc


def _exhibit(svc: ContractService) -> dict:
    return svc.subprocessor_exhibit(_COUNTERPARTY, _MATTER)


def _processor(data: dict, name: str) -> dict:
    return next(p for p in data["processors"] if p["name"] == name)


# ---------------------------------------------------------------------------
# Assembly across two documents
# ---------------------------------------------------------------------------


def test_exhibit_assembles_both_documents_lists_with_lineage():
    data = _exhibit(_service_with_matter())
    assert data["counts"] == {
        "documents": 2,
        "entries": 6,
        "processors": 3,
        "conflicts": 1,
        "unparsed": 1,
    }
    hostline = _processor(data, "Hostline GmbH")
    assert len(hostline["appearances"]) == 2
    appearance = next(
        a for a in hostline["appearances"] if a["contract_id"] == "CTR-SP-A"
    )
    assert appearance["document_id"] == "ANNEX-1"
    assert appearance["section"] == "Annex 1 — Sub-processor List"
    assert appearance["purpose"] == "data hosting"
    assert appearance["location"] == "Germany"
    assert appearance["line"] == "Hostline GmbH (Germany) — data hosting"
    assert appearance["chain_root"] == "CTR-SP-A"


def test_conflicting_purpose_across_documents_is_flagged_with_both_sources():
    data = _exhibit(_service_with_matter())
    assert len(data["conflicts"]) == 1
    conflict = data["conflicts"][0]
    assert conflict["name"] == "Cloudmetrics Inc"
    assert conflict["field"] == "purpose"
    cited = {(v["value"], v["contract_id"]) for v in conflict["values"]}
    assert cited == {
        ("product analytics", "CTR-SP-A"),
        ("usage analytics", "CTR-SP-B"),
    }


def test_prose_mention_is_carried_as_unparsed_never_dropped():
    data = _exhibit(_service_with_matter())
    assert len(data["unparsed"]) == 1
    item = data["unparsed"][0]
    assert item["contract_id"] == "CTR-SP-B"
    assert item["document_id"] == "ANNEX-1"
    assert "Brightdata Analytics Inc" in item["line"]
    # And it was not guessed into structure either.
    assert all(p["name"] != "Brightdata Analytics Inc" for p in data["processors"])


def test_text_rendering_carries_lineage_conflicts_and_unparsed():
    data = _exhibit(_service_with_matter())
    text = render_subprocessor_exhibit_text(data)
    assert "Sub-processor exhibit — Exhibit Matter Co / Platform DPA matter" in text
    assert "draws no conclusion" in text
    assert '"product analytics" (CTR-SP-A, ANNEX-1)' in text
    assert '"usage analytics" (CTR-SP-B, ANNEX-1)' in text
    assert "Brightdata Analytics Inc" in text
    assert '"Hostline GmbH (Germany) — data hosting"' in text


# ---------------------------------------------------------------------------
# The comparison rules
# ---------------------------------------------------------------------------


def test_conflicting_location_across_documents_is_flagged():
    annex_b = _ANNEX_A.replace("(Ireland)", "(Netherlands)")
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(_dpa("CTR-SP-A", _ANNEX_A))
    svc.analyze(_dpa("CTR-SP-B", annex_b))
    data = _exhibit(svc)
    locations = [c for c in data["conflicts"] if c["field"] == "location"]
    assert len(locations) == 1
    assert locations[0]["name"] == "Postmark Systems Ltd"
    assert {v["value"] for v in locations[0]["values"]} == {
        "Ireland",
        "Netherlands",
    }


def test_silence_in_one_document_is_not_a_conflict():
    # B lists Hostline with no location: one stated value across
    # the matter, so nothing is stated two ways.
    annex_b = _ANNEX_A.replace("Hostline GmbH (Germany)", "Hostline GmbH")
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(_dpa("CTR-SP-A", _ANNEX_A))
    svc.analyze(_dpa("CTR-SP-B", annex_b))
    data = _exhibit(svc)
    assert data["conflicts"] == []


def test_restatement_inside_one_document_is_not_a_conflict():
    annex = _ANNEX_A + "- Hostline GmbH (Germany) — backup hosting\n"
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(_dpa("CTR-SP-A", annex))
    data = _exhibit(svc)
    assert data["conflicts"] == []
    assert len(_processor(data, "Hostline GmbH")["appearances"]) == 2


def test_exhibit_reads_each_chains_latest_round():
    annex_old = _ANNEX_A + "- Oldrelay GmbH (Germany) — legacy relay\n"
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(_dpa("CTR-SP-A1", annex_old))
    svc.analyze(_dpa("CTR-SP-A2", _ANNEX_A, supersedes="CTR-SP-A1"))
    data = _exhibit(svc)
    assert data["counts"]["documents"] == 1
    assert all(p["name"] != "Oldrelay GmbH" for p in data["processors"])
    document = data["documents"][0]
    assert document["contract_id"] == "CTR-SP-A2"
    assert document["chain_root"] == "CTR-SP-A1"


def test_matter_without_list_annexes_assembles_empty():
    svc = ContractService(store=InMemoryStore(), checkpointer=False)
    svc.analyze(
        contract(counterparty_name=_COUNTERPARTY, matter=_MATTER)
    )
    data = _exhibit(svc)
    assert data["counts"] == {
        "documents": 0,
        "entries": 0,
        "processors": 0,
        "conflicts": 0,
        "unparsed": 0,
    }
    assert "no list annex" in render_subprocessor_exhibit_text(data)


def test_unknown_matter_and_empty_tag_are_refused():
    svc = _service_with_matter()
    with pytest.raises(KeyError):
        svc.subprocessor_exhibit(_COUNTERPARTY, "No such matter")
    with pytest.raises(ValueError):
        svc.subprocessor_exhibit(_COUNTERPARTY, "")


# ---------------------------------------------------------------------------
# The endpoint: two renderings of one assembly
# ---------------------------------------------------------------------------


def test_exhibit_endpoint_serves_json_and_text():
    svc_records = _service_with_matter()
    # The API module serves its own service; analyse through it.
    client = TestClient(app)
    for payload in (
        _dpa("CTR-SP-A", _ANNEX_A).model_dump(mode="json"),
        _dpa("CTR-SP-B", _ANNEX_B).model_dump(mode="json"),
    ):
        assert client.post("/contracts/analyze", json=payload).status_code == 200
    params = {"counterparty": _COUNTERPARTY, "matter": _MATTER}
    res = client.get("/matters/subprocessor-exhibit", params=params)
    assert res.status_code == 200
    assert res.json()["counts"]["processors"] == 3
    text = client.get(
        "/matters/subprocessor-exhibit", params={**params, "format": "text"}
    )
    assert text.status_code == 200
    assert "Cloudmetrics Inc — purpose differs" in text.text
    missing = client.get(
        "/matters/subprocessor-exhibit",
        params={"counterparty": _COUNTERPARTY, "matter": "Nope"},
    )
    assert missing.status_code == 404
    bad = client.get(
        "/matters/subprocessor-exhibit", params={**params, "format": "yaml"}
    )
    assert bad.status_code == 422
    # The service-level assembly the endpoint renders is the same one.
    assert svc_records.subprocessor_exhibit(_COUNTERPARTY, _MATTER)["counts"] == (
        res.json()["counts"]
    )


def test_builder_is_pure_over_records():
    svc = _service_with_matter()
    from contract_agent.matter_brief import matter_records

    chains = matter_records(
        svc._get_store().all_records(), _COUNTERPARTY, _MATTER
    )
    data = build_subprocessor_exhibit(_COUNTERPARTY, _MATTER, chains)
    assert data["counts"]["entries"] == 6
