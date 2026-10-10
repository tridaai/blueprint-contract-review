"""Webhook outbox with re-send (round 14): the three signed
families — approved packets, queue SLA breaches, drift findings
— used to keep their outcomes scattered (a status on the
decision record, a stamp on the breached record, a list on the
analysis result), and a failed delivery could only be recovered
by re-running whatever produced it. The outbox is the one
delivery ledger: every dispatch is recorded as it happens with
the exact payload posted, failed entries are visible in one
view, and an operator re-sends a failed entry deliberately —
the attempt recorded under their name, the payload re-posted
as stored, the event never regenerated.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import date

import pytest

from contract_agent.samples import sample_contract_models
from contract_agent.schemas import ContractInput
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore
from contract_agent.webhook_outbox import InMemoryOutboxStore


@pytest.fixture(scope="module")
def samples() -> dict[str, ContractInput]:
    return {c.contract_id: c for c in sample_contract_models()}


def _service(outbox: InMemoryOutboxStore | None = None) -> ContractService:
    return ContractService(
        store=InMemoryStore(),
        checkpointer=False,
        outbox_store=outbox or InMemoryOutboxStore(),
    )


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Endpoint:
    """A fake webhook endpoint whose health the test controls:
    it starts broken (500s) and can be repaired mid-test — the
    situation the outbox exists for."""

    def __init__(self, monkeypatch, status: int = 500) -> None:
        self.status = status
        self.requests: list = []

        def fake_urlopen(request, timeout=None):
            self.requests.append(request)
            return _Response(self.status)

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setenv("ACTION_WEBHOOK_URL", "https://clm.example.com/hook")
        monkeypatch.setenv("ACTION_WEBHOOK_SECRET", "hook-secret")


# ---------------------------------------------------------------------------
# Approval family: a failed packet delivery, repaired by an act
# ---------------------------------------------------------------------------


def test_failed_approval_lands_in_the_outbox_with_its_payload(samples, monkeypatch):
    endpoint = _Endpoint(monkeypatch)
    outbox = InMemoryOutboxStore()
    svc = _service(outbox)
    contract = samples["CTR-2001"]
    svc.analyze(contract)
    result = svc.approve(contract.contract_id, "C. Ounsel", "agreed as negotiated")
    assert result.dispatch_status == "failed"

    entries = outbox.all()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.family == "approval"
    assert entry.contract_id == contract.contract_id
    assert entry.status == "failed"
    assert len(entry.attempts) == 1
    assert entry.attempts[0].actor == ""  # the automatic dispatch
    # The stored payload is the packet that was posted.
    posted = json.loads(endpoint.requests[0].data)
    assert entry.payload == posted
    assert entry.payload["contract_id"] == contract.contract_id
    assert entry.payload["status"] == "approved"


def test_resend_repairs_a_failed_approval_without_regenerating_it(samples, monkeypatch):
    endpoint = _Endpoint(monkeypatch)
    outbox = InMemoryOutboxStore()
    svc = _service(outbox)
    contract = samples["CTR-2001"]
    svc.analyze(contract)
    svc.approve(contract.contract_id, "C. Ounsel", "agreed as negotiated")
    entry = outbox.all()[0]

    endpoint.status = 200  # the customer fixed their endpoint
    outcome = svc.resend_webhook(entry.outbox_id, "Ops Rivera", "endpoint restored")
    assert outcome["outcome"] == "sent"
    assert outcome["status"] == "sent"
    # The re-send posted the stored bytes — the same payload, not
    # a regenerated event.
    assert json.loads(endpoint.requests[-1].data) == entry.payload
    stored = outbox.get(entry.outbox_id)
    assert stored.status == "sent"
    assert len(stored.attempts) == 2
    assert stored.attempts[1].actor == "Ops Rivera"
    assert stored.attempts[1].note == "endpoint restored"
    # And a delivered entry cannot be re-sent again.
    with pytest.raises(ValueError, match="only a failed delivery"):
        svc.resend_webhook(entry.outbox_id, "Ops Rivera")


def test_failed_resend_is_recorded_and_the_entry_stays_failed(samples, monkeypatch):
    _Endpoint(monkeypatch)  # stays broken
    outbox = InMemoryOutboxStore()
    svc = _service(outbox)
    contract = samples["CTR-2001"]
    svc.analyze(contract)
    svc.approve(contract.contract_id, "C. Ounsel", "agreed")
    entry = outbox.all()[0]
    outcome = svc.resend_webhook(entry.outbox_id, "Ops Rivera")
    assert outcome["outcome"] == "failed"
    stored = outbox.get(entry.outbox_id)
    assert stored.status == "failed"
    assert len(stored.attempts) == 2
    assert stored.attempts[1].actor == "Ops Rivera"


def test_resend_guards_actor_unknown_and_unconfigured(samples, monkeypatch):
    _Endpoint(monkeypatch)
    outbox = InMemoryOutboxStore()
    svc = _service(outbox)
    contract = samples["CTR-2001"]
    svc.analyze(contract)
    svc.approve(contract.contract_id, "C. Ounsel", "agreed")
    entry = outbox.all()[0]
    with pytest.raises(ValueError, match="named actor"):
        svc.resend_webhook(entry.outbox_id, "  ")
    with pytest.raises(KeyError):
        svc.resend_webhook("no-such-entry", "Ops Rivera")
    monkeypatch.delenv("ACTION_WEBHOOK_URL")
    with pytest.raises(ValueError, match="ACTION_WEBHOOK_URL is not configured"):
        svc.resend_webhook(entry.outbox_id, "Ops Rivera")
    # None of the refused acts spent an attempt.
    assert len(outbox.get(entry.outbox_id).attempts) == 1


def test_no_webhook_configured_records_no_outbox_entry(samples):
    outbox = InMemoryOutboxStore()
    svc = _service(outbox)
    contract = samples["CTR-2001"]
    svc.analyze(contract)
    result = svc.approve(contract.contract_id, "C. Ounsel", "agreed")
    assert result.dispatch_status is None
    assert outbox.all() == []
    assert svc.webhook_outbox()["counts"]["total"] == 0


# ---------------------------------------------------------------------------
# Drift family: the finding's dispatch joins the same ledger
# ---------------------------------------------------------------------------

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


def _dpa_round(samples, contract_id: str, annex: str, supersedes: str = "") -> ContractInput:
    data = samples["CTR-2031"].model_dump(mode="json")
    data["contract_id"] = contract_id
    data["supersedes"] = supersedes
    for doc in data["documents"]:
        if doc["document_id"] == "ANNEX-1":
            doc["raw_text"] = annex
    return ContractInput.model_validate(data)


def test_drift_dispatch_is_recorded_and_resendable(samples, monkeypatch):
    endpoint = _Endpoint(monkeypatch)
    outbox = InMemoryOutboxStore()
    svc = _service(outbox)
    svc.analyze(_dpa_round(samples, "CTR-OB-1", _CLEAN_ANNEX))
    result = svc.analyze(
        _dpa_round(samples, "CTR-OB-2", _ADDED_ANNEX, supersedes="CTR-OB-1")
    )
    assert [d.dispatch for d in result.drift_dispatches] == ["failed"]
    entries = outbox.all()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.family == "drift_finding"
    assert entry.label == "subprocessor_list_drift"
    assert entry.payload["event"] == "drift_finding"
    assert entry.payload["contract_id"] == "CTR-OB-2"
    endpoint.status = 200
    assert svc.resend_webhook(entry.outbox_id, "Ops Rivera")["outcome"] == "sent"


# ---------------------------------------------------------------------------
# SLA breach family: the sweep's dispatch joins the same ledger
# ---------------------------------------------------------------------------


def test_sla_breach_dispatch_is_recorded_and_resendable(samples, monkeypatch):
    endpoint = _Endpoint(monkeypatch)
    outbox = InMemoryOutboxStore()
    svc = _service(outbox)
    contract = samples["CTR-2002"].model_copy(deep=True)  # walk-away tier
    contract.received_at = "2026-09-01T09:30:00"
    svc.analyze(contract)
    report = svc.dispatch_sla_breach_sweep(today=date(2026, 9, 10))
    assert report["webhooks"]["failed"] == 1
    entries = outbox.all()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.family == "sla_breach"
    assert entry.payload["event"] == "sla_breach"
    assert entry.status == "failed"
    endpoint.status = 200
    assert svc.resend_webhook(entry.outbox_id, "Ops Rivera")["outcome"] == "sent"
    # The breach itself is not re-announced by the repair: the
    # sweep's stamp stands, and a later sweep reports it as
    # already reported.
    again = svc.dispatch_sla_breach_sweep(today=date(2026, 9, 11))
    assert again["already_reported"] == 1
    assert len(outbox.all()) == 1


# ---------------------------------------------------------------------------
# The view and the metrics
# ---------------------------------------------------------------------------


def test_outbox_view_filters_and_counts(samples, monkeypatch):
    endpoint = _Endpoint(monkeypatch)
    outbox = InMemoryOutboxStore()
    svc = _service(outbox)
    contract = samples["CTR-2001"]
    svc.analyze(contract)
    svc.approve(contract.contract_id, "C. Ounsel", "agreed")
    entry = outbox.all()[0]
    endpoint.status = 200
    svc.resend_webhook(entry.outbox_id, "Ops Rivera")

    view = svc.webhook_outbox()
    assert view["counts"] == {
        "total": 1,
        "sent": 1,
        "failed": 0,
        "resends": 1,
        "by_family": {"approval": 1},
    }
    assert view["entries"][0]["outbox_id"] == entry.outbox_id
    assert "payload" not in view["entries"][0]  # the view carries no drafts
    assert svc.webhook_outbox(status="failed")["entries"] == []
    assert svc.metrics()["webhooks"]["total"] == 1
    assert svc.metrics()["webhooks"]["resends"] == 1


def test_outbox_ids_do_not_collide_on_a_repeated_dispatch(samples, monkeypatch):
    _Endpoint(monkeypatch)
    outbox = InMemoryOutboxStore()
    svc = _service(outbox)
    # The same drift finding pushed by two analyses of the same
    # round (a re-analysis) — both deliveries are kept.
    svc.analyze(_dpa_round(samples, "CTR-OB-1", _CLEAN_ANNEX))
    svc.analyze(_dpa_round(samples, "CTR-OB-2", _ADDED_ANNEX, supersedes="CTR-OB-1"))
    svc.analyze(_dpa_round(samples, "CTR-OB-2", _ADDED_ANNEX, supersedes="CTR-OB-1"))
    ids = [e.outbox_id for e in outbox.all()]
    assert len(ids) == 2
    assert len(set(ids)) == 2
