"""Multi-contract matter brief (round 9): several chains sharing a
counterparty + matter tag briefed as one matter — combined open
positions, exhibit findings across the documents, and all
obligations sorted by deadline — exported through the object-store
port with the chain brief's lineage discipline."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from contract_agent.api import app
from contract_agent.objectstore import InMemoryObjectStore
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import DocumentInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore
from helpers import contract

MATTER = "Vertex platform renewal"
COUNTERPARTY = "Vertex Analytics Ltd"

# Chain B: a single-round SOW-style contract in the same matter —
# a walk-away liability clause (an open line for the combined
# view), a DPA exhibit whose breach window contradicts the clause
# (a cross-contract exhibit finding), and dated obligations.
_CHAIN_B_TEXT = """1. Term and Termination
This Agreement renews automatically on March 1, 2027 for successive
one (1) year terms unless either party gives at least ninety (90)
days' written notice of non-renewal. Either party may terminate
for convenience on written notice.

2. Limitation of Liability
The Provider's total liability arising under or in connection with
this Agreement shall be unlimited, and no cap on liability shall
apply to any claim.

3. Data Protection
Each party shall protect personal data in accordance with the Data
Processing Addendum attached hereto as Exhibit B. A party shall
notify the other within 48 hours of a personal data breach.
"""

_CHAIN_B_EXHIBIT = """Exhibit B — Data Processing Addendum

This Data Processing Addendum forms part of the Agreement.

1. Breach notification. The Processor shall notify the Controller
of a personal data breach within 72 hours of discovery.
"""


@pytest.fixture(scope="module")
def samples() -> dict:
    return {c.contract_id: c for c in sample_contract_models()}


def _service(objects: InMemoryObjectStore | None = None) -> ContractService:
    return ContractService(
        store=InMemoryStore(), checkpointer=False, object_store=objects
    )


def _matter_service(samples, objects=None) -> ContractService:
    svc = _service(objects)
    for cid in ("CTR-2014", "CTR-2015"):
        svc.analyze(samples[cid].model_copy(update={"matter": MATTER}))
    svc.analyze(
        contract(
            _CHAIN_B_TEXT,
            contract_id="CTR-MB-SOW",
            counterparty_name=COUNTERPARTY,
            matter=MATTER,
            documents=[
                DocumentInput(
                    doc_type="agreement", document_id="AGR-B", raw_text=_CHAIN_B_TEXT
                ),
                DocumentInput(
                    doc_type="exhibit", document_id="EX-B", raw_text=_CHAIN_B_EXHIBIT
                ),
            ],
        )
    )
    # Noise that must NOT leak into the matter: another matter of
    # the same counterparty, and the same tag on someone else's paper.
    svc.analyze(
        contract(contract_id="CTR-MB-OTHER", counterparty_name=COUNTERPARTY, matter="Other matter")
    )
    svc.analyze(samples["CTR-2027"].model_copy(update={"matter": MATTER}))
    return svc


def test_matter_brief_combines_chains_positions_exhibits_obligations(samples):
    svc = _matter_service(samples)
    brief = svc.matter_brief(COUNTERPARTY, MATTER)
    assert brief["chains"] == 2
    assert brief["contracts"] == 3
    md = brief["markdown"]
    assert f"# Matter brief — {MATTER}" in md
    assert "CTR-2014 → CTR-2015" in md  # chain A, both rounds
    assert "CTR-MB-SOW" in md
    # Combined open positions: chain B's unlimited liability is a
    # tier-2 line open in the matter, named with its contract.
    assert "Tier 2 — walk-away lines open anywhere in the matter" in md
    assert "CTR-MB-SOW" in md.split("Tier 2")[1].split("###")[0]
    # The cross-contract exhibit finding: chain B's DPA promises 48h
    # in the clause and states 72h in the exhibit.
    assert "breach_notice_window" in md
    assert "mismatch" in md
    # Obligations across the matter, dated first by deadline:
    # chain B's computed notice deadline (2026-12-01) precedes its
    # renewal date (2027-03-01).
    obligations_section = md.split("## Obligations across the matter")[1]
    assert obligations_section.index("2026-12-01") < obligations_section.index("2027-03-01")
    assert "CTR-MB-SOW" in obligations_section


def test_matter_brief_excludes_other_matters_and_counterparties(samples):
    svc = _matter_service(samples)
    md = svc.matter_brief(COUNTERPARTY, MATTER)["markdown"]
    assert "CTR-MB-OTHER" not in md
    assert "CTR-2027" not in md  # same tag, different counterparty
    with pytest.raises(KeyError):
        svc.matter_brief(COUNTERPARTY, "No such matter")
    with pytest.raises(KeyError):
        svc.matter_brief("Nobody", MATTER)
    with pytest.raises(ValueError):
        svc.matter_brief(COUNTERPARTY, "")


def test_matter_brief_export_lineage_and_delta(samples):
    objects = InMemoryObjectStore()
    svc = _matter_service(samples, objects)
    first = svc.matter_brief(COUNTERPARTY, MATTER)
    assert first["export_seq"] == 1
    assert first["previous_export"] is None
    assert "first brief exported for this matter" in first["markdown"]
    stored = objects.get(first["key"])
    assert stored is not None and stored.decode("utf-8") == first["markdown"]
    assert first["key"].startswith("matters/vertex-analytics-ltd--vertex-platform-renewal/")
    # The negotiation moves: round 3 of chain A arrives, a decision
    # lands on the SOW — the next export leads with the delta.
    svc.analyze(samples["CTR-2016"].model_copy(update={"matter": MATTER}))
    svc.approve("CTR-MB-SOW", approver="C. Ounsel")
    second = svc.matter_brief(COUNTERPARTY, MATTER)
    assert second["export_seq"] == 2
    assert second["previous_export"] == {"key": first["versioned_key"]}
    delta = "\n".join(second["delta_lines"])
    assert "Chain advanced: CTR-2015 → CTR-2016" in delta
    assert "Decision recorded since the last brief: approved on CTR-MB-SOW" in delta
    assert "Since the last matter brief" in second["markdown"]
    # A third export with nothing changed says exactly that.
    third = svc.matter_brief(COUNTERPARTY, MATTER)
    assert third["delta_lines"] == [
        "Nothing material changed since the last matter brief — "
        "the facts below are the same facts, re-exported."
    ]


def test_matter_brief_endpoint():
    client = TestClient(app)
    payload = contract(
        contract_id="CTR-MB-API",
        counterparty_name="Matter API Co",
        matter="API matter",
    ).model_dump(mode="json")
    assert client.post("/contracts/analyze", json=payload).status_code == 200
    res = client.get(
        "/matters/brief",
        params={"counterparty": "Matter API Co", "matter": "API matter"},
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/markdown")
    assert "attachment" in res.headers["content-disposition"]
    assert res.headers["x-object-key"].startswith("matters/matter-api-co--api-matter/")
    assert "# Matter brief — API matter" in res.text
    missing = client.get(
        "/matters/brief",
        params={"counterparty": "Matter API Co", "matter": "Nope"},
    )
    assert missing.status_code == 404
    empty = client.get(
        "/matters/brief", params={"counterparty": "Matter API Co", "matter": ""}
    )
    assert empty.status_code == 422
