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


# ---------------------------------------------------------------------------
# The matter-scope widening: lineage, cross-chain patterns, shared
# obligations, the roll-up, and the JSON rendering of the assembly
# ---------------------------------------------------------------------------

from contract_agent.schemas import EditOutcomeInput  # noqa: E402

_PATTERN_CP = "Matter Pattern Co"
_PATTERN_MATTER = "Cross-chain pattern matter"

_NET90_A = """1. Limitation of Liability
Each party's aggregate liability shall not exceed the fees paid in the
twelve (12) months preceding the claim.

2. Payment Terms
The Customer shall pay all invoices within ninety (90) days of the invoice date.
"""

_NET90_B = """1. Limitation of Liability
Each party's aggregate liability shall not exceed the fees paid in the
six (6) months preceding the claim.

2. Payment Terms
The Customer shall pay all invoices within ninety (90) days of the invoice date.
"""


def _agree(svc: ContractService, result, clause_type: str) -> None:
    index = next(
        i for i, e in enumerate(result.proposed_edits) if e.clause_type == clause_type
    )
    svc.record_edit_outcomes(
        result.contract_id,
        [EditOutcomeInput(edit_index=index, outcome="accepted", note="agreed on the call")],
        actor="C. Ounsel",
    )


def _pattern_service() -> ContractService:
    """Two 2-round chains, one counterparty, one matter. In each
    chain counsel agrees the payment edit in round 1 and round 2's
    draft still does not show it — ONE miss per chain, so neither
    chain forms a pattern of its own."""
    svc = _service()
    a1 = svc.analyze(
        contract(_NET90_A, contract_id="CTR-MP-A1", counterparty_name=_PATTERN_CP, matter=_PATTERN_MATTER)
    )
    _agree(svc, a1, "payment_terms")
    svc.analyze(
        contract(_NET90_A, contract_id="CTR-MP-A2", counterparty_name=_PATTERN_CP, matter=_PATTERN_MATTER, supersedes="CTR-MP-A1")
    )
    b1 = svc.analyze(
        contract(_NET90_B, contract_id="CTR-MP-B1", counterparty_name=_PATTERN_CP, matter=_PATTERN_MATTER)
    )
    _agree(svc, b1, "payment_terms")
    svc.analyze(
        contract(_NET90_B, contract_id="CTR-MP-B2", counterparty_name=_PATTERN_CP, matter=_PATTERN_MATTER, supersedes="CTR-MP-B1")
    )
    return svc


def test_matter_brief_cross_chain_pattern_neither_chain_shows_alone():
    svc = _pattern_service()
    brief = svc.matter_brief(_PATTERN_CP, _PATTERN_MATTER)
    data = brief["data"]
    # Neither chain's current round carries a pattern of its own…
    assert data["patterns"] == []
    # …but read together, payment_terms is a matter-level pattern.
    assert [p["clause_type"] for p in data["matter_patterns"]] == ["payment_terms"]
    pattern = data["matter_patterns"][0]
    assert pattern["total_flags"] == 2
    assert pattern["chains_with_active_pattern"] == 0
    assert {m["latest"] for m in pattern["chains"]} == {"CTR-MP-A2", "CTR-MP-B2"}
    md = brief["markdown"]
    assert "## Patterns visible only across the matter" in md
    assert "**payment_terms** — missed in 2 chains" in md


_OBLIGATION_MSA = """1. Term and Termination
This Agreement renews automatically on March 1, 2027 for successive
one (1) year terms unless either party gives at least one hundred
twenty (120) days' written notice of non-renewal.

2. Payment Terms
The Customer shall pay all invoices within thirty (30) days of the invoice date.
"""

_OBLIGATION_SOW = _OBLIGATION_MSA.replace(
    "one hundred\ntwenty (120)", "ninety (90)"
).replace("one hundred twenty (120)", "ninety (90)")


def test_matter_brief_shared_obligations_conflict_and_consistent():
    svc = _service()
    svc.analyze(
        contract(_OBLIGATION_MSA, contract_id="CTR-MO-MSA", counterparty_name="Obligation Co", matter="Shared duties")
    )
    svc.analyze(
        contract(_OBLIGATION_SOW, contract_id="CTR-MO-SOW", counterparty_name="Obligation Co", matter="Shared duties", doc_type="sow")
    )
    brief = svc.matter_brief("Obligation Co", "Shared duties")
    groups = {g["kind"]: g for g in brief["data"]["shared_obligations"]}
    # The MSA states 120 days' non-renewal notice, the SOW 90 —
    # the same duty, different terms: a conflict, in code.
    notice = groups["notice_period"]
    assert notice["status"] == "conflict"
    assert sorted(m["days"] for m in notice["members"]) == [90, 120]
    assert {m["contract_id"] for m in notice["members"]} == {"CTR-MO-MSA", "CTR-MO-SOW"}
    # Both documents state net 30 — the same duty, the same terms.
    assert groups["payment"]["status"] == "consistent"
    md = brief["markdown"]
    assert "## Shared obligations across the matter" in md
    assert "notice_period — days' written notice — CONFLICT" in md
    assert "The chains do not state the same terms for this duty" in md
    assert "CTR-MO-MSA states 120 days' written notice" in md
    assert "CTR-MO-SOW states 90 days' written notice" in md


def test_matter_brief_delta_names_a_new_shared_obligation_conflict():
    objects = InMemoryObjectStore()
    svc = _service(objects)
    svc.analyze(
        contract(_OBLIGATION_MSA, contract_id="CTR-MO-MSA", counterparty_name="Obligation Co", matter="Shared duties")
    )
    first = svc.matter_brief("Obligation Co", "Shared duties")
    assert first["data"]["shared_obligations"] == []  # one chain: nothing shared yet
    svc.analyze(
        contract(_OBLIGATION_SOW, contract_id="CTR-MO-SOW", counterparty_name="Obligation Co", matter="Shared duties", doc_type="sow")
    )
    second = svc.matter_brief("Obligation Co", "Shared duties")
    delta = "\n".join(second["delta_lines"])
    assert "Shared-obligation conflict in the matter: notice_period" in delta


_LINEAGE_R1 = """1. Limitation of Liability
Each party's aggregate liability shall not exceed the fees paid in the
three (3) months preceding the claim.
"""

_LINEAGE_R2 = """1. Limitation of Liability
The Provider's total liability arising under or in connection with
this Agreement shall be unlimited, and no cap on liability shall apply.
"""


def test_matter_brief_walk_away_lineage_moved_across_rounds():
    svc = _service()
    svc.analyze(
        contract(_LINEAGE_R1, contract_id="CTR-ML-1", counterparty_name="Lineage Co", matter="Lineage matter")
    )
    svc.analyze(
        contract(_LINEAGE_R2, contract_id="CTR-ML-2", counterparty_name="Lineage Co", matter="Lineage matter", supersedes="CTR-ML-1")
    )
    brief = svc.matter_brief("Lineage Co", "Lineage matter")
    lines = [p for p in brief["data"]["open_positions"] if p["tier"] == 2]
    assert len(lines) == 1
    lineage = lines[0]["lineage"]
    assert lineage["first_seen_in"] == "CTR-ML-1"
    assert lineage["walk_away_since"] == "CTR-ML-2"
    assert lineage["rounds_at_walk_away"] == 1
    assert lineage["moved"] is True
    assert lineage["from_position"] == "acceptable_fallback"
    assert lineage["to_position"] == "walk_away"
    md = brief["markdown"]
    assert "Lineage: clause first seen in CTR-ML-1 at acceptable_fallback" in md
    assert "walk-away line first proposed in CTR-ML-2" in md
    assert "position moved acceptable_fallback → walk_away" in md


def test_matter_brief_rollup_counts_positions_tiers_postures(samples):
    svc = _matter_service(samples)
    brief = svc.matter_brief(COUNTERPARTY, MATTER)
    data = brief["data"]
    rollup = data["rollup"]
    assert rollup["chains"] == 2 and rollup["contracts"] == 3
    assert rollup["open_walk_aways"] == sum(
        row["open_walk_aways"] for row in data["chains"]
    )
    # Positions counted across both chains' current rounds
    # (twelve verdicts in all), by ladder position, and the
    # chains split one negotiable / one escalation-required.
    assert rollup["positions"] == {
        "preferred": 10,
        "acceptable_fallback": 1,
        "walk_away": 1,
    }
    assert sum(rollup["positions"].values()) == 12
    assert rollup["tiers"] == {"2": 2}
    assert rollup["chains_by_posture"] == {
        "negotiable": 1,
        "escalation_required": 1,
    }
    assert all(row["posture"] for row in data["chains"])
    md = brief["markdown"]
    assert "## Matter roll-up" in md
    assert "Chains by posture:" in md
    assert "| Posture |" in md


def test_matter_brief_json_export_matches_the_markdown_assembly(samples):
    objects = InMemoryObjectStore()
    svc = _matter_service(samples, objects)
    brief = svc.matter_brief(COUNTERPARTY, MATTER)
    assert brief["json_key"].endswith("matter-brief.json")
    stored = objects.get(brief["json_key"])
    assert stored is not None
    import json as _json

    data = _json.loads(stored.decode("utf-8"))
    assert data == brief["data"]
    assert data["kind"] == "matter_brief"
    assert data["counterparty"] == COUNTERPARTY and data["matter"] == MATTER
    assert objects.get(brief["versioned_json_key"]) == stored
    # Same facts, both renderings: the JSON's chains are the
    # markdown's chain table.
    for row in data["chains"]:
        assert row["latest"] in brief["markdown"]


def test_matter_brief_json_endpoint():
    client = TestClient(app)
    payload = contract(
        contract_id="CTR-MB-JSON",
        counterparty_name="Matter JSON Co",
        matter="JSON matter",
    ).model_dump(mode="json")
    assert client.post("/contracts/analyze", json=payload).status_code == 200
    res = client.get(
        "/matters/brief",
        params={"counterparty": "Matter JSON Co", "matter": "JSON matter", "format": "json"},
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    assert res.headers["x-object-key"].endswith("matter-brief.json")
    body = res.json()
    assert body["kind"] == "matter_brief"
    assert body["rollup"]["chains"] == 1
    bogus = client.get(
        "/matters/brief",
        params={"counterparty": "Matter JSON Co", "matter": "JSON matter", "format": "yaml"},
    )
    assert bogus.status_code == 422
