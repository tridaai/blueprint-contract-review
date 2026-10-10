"""Deadline dispatch sweep: stored contracts are walked for due
obligations, one event per deadline, report-only (round 4)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from contract_agent.api import app
from contract_agent.events import CollectingSink
from contract_agent.service import ContractService
from contract_agent.store import InMemoryStore
from helpers import contract

# Renewal 2027-03-01 with ninety days' notice -> the non-renewal
# notice deadline computes to 2026-12-01.
_TEXT = (
    "1. Term and Termination\n"
    "This Agreement renews automatically on March 1, 2027 for successive "
    "one (1) year terms unless either party gives at least ninety (90) "
    "days' written notice of non-renewal. Either party may terminate "
    "for convenience on written notice.\n\n"
    "2. Limitation of Liability\n"
    "Each party's aggregate liability shall not exceed the fees paid "
    "in the twelve (12) months preceding the claim."
)


def _service_with_contract() -> ContractService:
    service = ContractService(store=InMemoryStore(), checkpointer=False)
    service.analyze(contract(_TEXT, contract_id="CTR-SWEEP-1"))
    return service


def test_sweep_reports_and_emits_per_deadline_frozen_clock():
    service = _service_with_contract()
    sink = CollectingSink()
    report = service.dispatch_obligation_sweep(
        days=30, today=date(2026, 11, 20), event_sink=sink
    )
    assert report["report_only"] is True
    assert report["contracts_swept"] == 1
    assert report["as_of"] == "2026-11-20"
    # Only the computed notice deadline (2026-12-01, 11 days out) is
    # inside the window; the renewal itself (2027-03-01) is not.
    assert report["due_count"] == 1
    assert report["events_emitted"] == 1
    row = report["obligations"][0]
    assert row["kind"] == "notice_period"
    assert row["date"] == "2026-12-01"
    assert row["days_until"] == 11
    assert row["overdue"] is False
    events = [e for e in sink.events if e.type == "obligation_due"]
    assert len(events) == 1
    assert events[0].contract_id == "CTR-SWEEP-1"
    assert events[0].detail["via"] == "deadline_sweep"
    assert events[0].detail["kind"] == "notice_period"
    assert events[0].detail["computed"] is True


def test_sweep_marks_overdue_and_respects_the_window():
    service = _service_with_contract()
    sink = CollectingSink()
    report = service.dispatch_obligation_sweep(
        days=30, today=date(2026, 12, 15), event_sink=sink
    )
    assert report["due_count"] == 1
    assert report["obligations"][0]["overdue"] is True
    assert report["obligations"][0]["days_until"] == -14
    narrow = service.dispatch_obligation_sweep(
        days=5, today=date(2026, 11, 20), event_sink=sink
    )
    assert narrow["due_count"] == 0
    assert narrow["events_emitted"] == 0


def test_sweep_on_empty_store_is_a_clean_noop():
    service = ContractService(store=InMemoryStore(), checkpointer=False)
    sink = CollectingSink()
    report = service.dispatch_obligation_sweep(
        days=90, today=date(2026, 11, 20), event_sink=sink
    )
    assert report["contracts_swept"] == 0
    assert report["due_count"] == 0
    assert sink.events == []


def test_sweep_endpoint_returns_the_report():
    client = TestClient(app)
    payload = contract(_TEXT, contract_id="CTR-SWEEP-API").model_dump(mode="json")
    assert client.post("/contracts/analyze", json=payload).status_code == 200
    res = client.post("/obligations/sweep", params={"days": 400})
    assert res.status_code == 200
    body = res.json()
    assert body["report_only"] is True
    assert body["contracts_swept"] >= 1
    assert body["due_count"] >= 1
    bad = client.post("/obligations/sweep", params={"days": -1})
    assert bad.status_code == 422


def test_sweep_cli_reports_only(tmp_path: Path):
    fixture = tmp_path / "contracts.json"
    fixture.write_text(
        json.dumps(
            [contract(_TEXT, contract_id="CTR-SWEEP-CLI").model_dump(mode="json")]
        ),
        encoding="utf-8",
    )
    env = {**os.environ, "CHECKPOINTS": "off"}
    proc = subprocess.run(
        [
            sys.executable, "-m", "contract_agent.cli",
            "--file", str(fixture), "--sweep-obligations", "400",
        ],
        capture_output=True, text=True, env=env, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "Obligation sweep:" in proc.stdout
    assert "CTR-SWEEP-CLI" in proc.stdout
    assert "Report-only" in proc.stdout
