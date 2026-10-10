"""Drift findings as signed webhook events (round 13): the
approval webhook reports counsel's decisions, but the cross-round
findings — a sub-processor engaged inside its notice window, a
prior invention removed from the schedule it was protected by —
happen BETWEEN decisions, when nobody is looking at the packet.
When a chain round is analyzed and a drift check comes back a
finding (mismatch), the finding is pushed through the same signed
webhook seam: an event on the run's stream and a POST to
ACTION_WEBHOOK_URL whose payload carries the finding — check,
status, the movement the check computed — and never contract
text. Movement that breaks nothing stays on the result; it is
information for a reader, not an alarm for a system.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.request

import pytest

from contract_agent.events import CollectingSink
from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def _service() -> ContractService:
    return ContractService(store=InMemoryStore(), checkpointer=False)


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture(monkeypatch) -> list:
    captured = []

    def fake_urlopen(request, timeout=None):
        captured.append(request)
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("ACTION_WEBHOOK_URL", "https://clm.example.com/hook")
    monkeypatch.setenv("ACTION_WEBHOOK_SECRET", "hook-secret")
    return captured


# ---------------------------------------------------------------------------
# Chain builders (the shapes the drift rounds established)
# ---------------------------------------------------------------------------


def _dpa_round(samples, contract_id: str, received_at: str, annex: str,
               supersedes: str = "") -> ContractInput:
    data = samples["CTR-2031"].model_dump(mode="json")
    data["contract_id"] = contract_id
    data["received_at"] = received_at
    data["supersedes"] = supersedes
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

_ADDED_ANNEX = _CLEAN_ANNEX.replace(
    "- Postmark Systems Ltd (Ireland) — email delivery\n",
    "- Postmark Systems Ltd (Ireland) — email delivery\n"
    "- Cloudmetrics Inc (United States) — product analytics\n",
)


def _dpa_finding_chain(svc, samples) -> None:
    """CTR-2031's chain with an addition inside the notice window:
    the follow-up round's drift check is a mismatch."""
    svc.analyze(_dpa_round(samples, "CTR-DW-1", "2026-10-05T10:00:00", _CLEAN_ANNEX))
    return svc.analyze(
        _dpa_round(samples, "CTR-DW-2", "2026-10-15T10:00:00", _ADDED_ANNEX,
                   supersedes="CTR-DW-1")
    )


_SCHEDULE = (
    "Schedule of Prior Inventions\n\n"
    "The Employee records the following inventions, conceived before the "
    "employment, as the Employee's prior inventions:\n"
    "- RouteWeaver — a route-planning prototype for regional delivery fleets\n"
    "- Ledgerline — a bookkeeping reconciliation tool for small practices\n"
)


def _pi_round(samples, contract_id: str, schedule_text: str,
              supersedes: str = "CTR-2033") -> ContractInput:
    data = samples["CTR-2033"].model_dump(mode="json")
    data["contract_id"] = contract_id
    data["supersedes"] = supersedes
    for doc in data["documents"]:
        if doc["document_id"] == "SCHED-A":
            doc["raw_text"] = schedule_text
    return ContractInput.model_validate(data)


# ---------------------------------------------------------------------------
# The push: signed, finding-shaped, recorded
# ---------------------------------------------------------------------------


def test_subprocessor_finding_is_pushed_signed(samples, monkeypatch):
    captured = _capture(monkeypatch)
    svc = _service()
    result = _dpa_finding_chain(svc, samples)
    assert [c.status for c in result.subprocessor_drift] == ["mismatch"]
    assert len(captured) == 1
    request = captured[0]
    payload = json.loads(request.data)
    assert payload["event"] == "drift_finding"
    assert payload["check"] == "subprocessor_list_drift"
    assert payload["status"] == "mismatch"
    assert payload["contract_id"] == "CTR-DW-2"
    assert payload["prior_contract_id"] == "CTR-DW-1"
    assert "Cloudmetrics Inc" in payload["found"]
    signature = next(
        (v for k, v in request.headers.items() if k.lower() == "x-trida-signature"),
        None,
    )
    expected = hmac.new(b"hook-secret", request.data, hashlib.sha256).hexdigest()
    assert signature == f"sha256={expected}"
    # The push is recorded on the result — and on the saved record.
    assert len(result.drift_dispatches) == 1
    dispatch = result.drift_dispatches[0]
    assert dispatch.check == "subprocessor_list_drift"
    assert dispatch.prior_contract_id == "CTR-DW-1"
    assert dispatch.dispatch == "sent"
    stored = svc._get_store().get("CTR-DW-2")
    assert stored.result.drift_dispatches[0].dispatch == "sent"


def test_prior_invention_removal_is_pushed(samples, monkeypatch):
    captured = _capture(monkeypatch)
    svc = _service()
    svc.analyze(samples["CTR-2033"])
    shrunk = _SCHEDULE.replace(
        "- Ledgerline — a bookkeeping reconciliation tool for small practices\n", ""
    )
    result = svc.analyze(_pi_round(samples, "CTR-DW-PI", shrunk))
    assert [c.status for c in result.prior_invention_drift] == ["mismatch"]
    assert len(captured) == 1
    payload = json.loads(captured[0].data)
    assert payload["event"] == "drift_finding"
    assert payload["check"] == "prior_invention_schedule_drift"
    assert payload["prior_contract_id"] == "CTR-2033"
    assert "Ledgerline" in payload["found"]
    assert result.drift_dispatches[0].dispatch == "sent"


def test_payload_carries_the_finding_never_contract_text(samples, monkeypatch):
    captured = _capture(monkeypatch)
    svc = _service()
    _dpa_finding_chain(svc, samples)
    body = captured[0].data.decode("utf-8")
    # A distinctive sentence of the DPA's clause text — the finding
    # names what moved; the agreement's own words never ride along.
    assert "technical and organisational security measures" not in body
    assert "raw_text" not in body
    payload = json.loads(body)
    assert set(payload) == {
        "event", "check", "status", "contract_id", "prior_contract_id",
        "counterparty_name", "exhibit_document_id", "promised", "found",
        "detail",
    }


def test_finding_is_announced_on_the_event_stream(samples, monkeypatch):
    _capture(monkeypatch)
    svc = _service()
    svc.analyze(_dpa_round(samples, "CTR-DW-1", "2026-10-05T10:00:00", _CLEAN_ANNEX))
    sink = CollectingSink()
    svc.analyze(
        _dpa_round(samples, "CTR-DW-2", "2026-10-15T10:00:00", _ADDED_ANNEX,
                   supersedes="CTR-DW-1"),
        event_sink=sink,
    )
    events = [e for e in sink.events if e.type == "drift_finding"]
    assert len(events) == 1
    assert events[0].contract_id == "CTR-DW-2"
    assert events[0].detail["check"] == "subprocessor_list_drift"
    assert events[0].detail["prior_contract_id"] == "CTR-DW-1"


# ---------------------------------------------------------------------------
# The discipline: findings only, silence by default, failure recorded
# ---------------------------------------------------------------------------


def test_consistent_movement_is_not_pushed(samples, monkeypatch):
    captured = _capture(monkeypatch)
    svc = _service()
    svc.analyze(_dpa_round(samples, "CTR-DW-1", "2026-10-05T10:00:00", _CLEAN_ANNEX))
    # An addition OUTSIDE the notice window: reported, consistent.
    result = svc.analyze(
        _dpa_round(samples, "CTR-DW-2", "2026-11-20T10:00:00", _ADDED_ANNEX,
                   supersedes="CTR-DW-1")
    )
    assert [c.status for c in result.subprocessor_drift] == ["consistent"]
    assert captured == []
    assert result.drift_dispatches == []


def test_no_webhook_configured_records_no_dispatch(samples):
    svc = _service()
    result = _dpa_finding_chain(svc, samples)
    assert [c.status for c in result.subprocessor_drift] == ["mismatch"]
    assert result.drift_dispatches == []


def test_failed_push_is_recorded_and_the_finding_stands(samples, monkeypatch):
    def boom(request, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setenv("ACTION_WEBHOOK_URL", "https://clm.example.com/hook")
    svc = _service()
    result = _dpa_finding_chain(svc, samples)
    assert [c.status for c in result.subprocessor_drift] == ["mismatch"]
    assert len(result.drift_dispatches) == 1
    assert result.drift_dispatches[0].dispatch == "failed"
