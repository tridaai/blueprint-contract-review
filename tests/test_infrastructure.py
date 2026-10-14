"""Infrastructure tests: stores, object storage, resilience — the
in-memory doubles hermetically; the real PostgreSQL path is gated on
DATABASE_URL in test_postgres_integration.py."""

from __future__ import annotations

import os

import pytest

from contract_agent.migrations import pending_files
from contract_agent.objectstore import InMemoryObjectStore, S3ObjectStore
from contract_agent.resilience import ResilientBackend
from contract_agent.schemas import AgentResult
from contract_agent.store import ApprovalRecord, InMemoryStore


def make_record(contract_id="CTR-1", status="awaiting_counsel", decision=None, reason=None):
    return ApprovalRecord(
        result=AgentResult(
            contract_id=contract_id,
            status=status,
            counterparty_name="Northwind Traders Ltd",
            doc_type="msa",
            decision=decision,
            decision_reason=reason,
        ),
        contract={"counterparty_name": "Northwind Traders Ltd", "doc_type": "msa"},
    )


def test_inmemory_store_round_trip_and_memory():
    store = InMemoryStore()
    store.save(make_record())
    store.save(make_record("CTR-2", status="approved", decision="approved", reason="Counsel cleared after fallback wording."))
    assert store.get("CTR-1") is not None
    priors = store.prior_contracts(exclude_contract_id="CTR-1")
    assert [p["contract_id"] for p in priors] == ["CTR-2"]
    assert priors[0]["counterparty_name"] == "Northwind Traders Ltd"
    feedback = store.decision_feedback(exclude_contract_id="CTR-1")
    assert feedback and feedback[0]["reason"].startswith("Counsel cleared")


def test_inmemory_object_store():
    store = InMemoryObjectStore()
    assert store.get("contracts/CTR-1/AGR-1.txt") is None
    store.put("contracts/CTR-1/AGR-1.txt", b"agreement text")
    assert store.exists("contracts/CTR-1/AGR-1.txt")
    assert store.get("contracts/CTR-1/AGR-1.txt") == b"agreement text"


def test_s3_addressing_path_and_virtual_host():
    minio = S3ObjectStore(bucket="contracts", endpoint_url="http://minio:9000",
                          access_key="a", secret_key="s")
    assert minio._url("contracts/CTR-1/a.txt") == "http://minio:9000/contracts/contracts/CTR-1/a.txt"
    aws = S3ObjectStore(bucket="contracts", access_key="a", secret_key="s", region="eu-west-1")
    assert aws._url("k.txt") == "https://contracts.s3.eu-west-1.amazonaws.com/k.txt"


def test_s3_keys_with_spaces_are_percent_encoded():
    # Document ids are written by people ("Exhibit B"); an unencoded
    # space made every such PUT fail before it left the client.
    store = S3ObjectStore(bucket="contracts", endpoint_url="http://minio:9000",
                          access_key="k", secret_key="s")
    assert store._url("contracts/CTR-1/Exhibit B.txt") == (
        "http://minio:9000/contracts/contracts/CTR-1/Exhibit%20B.txt"
    )


def test_migration_files_numbered_and_present():
    files = pending_files()
    names = [f.name for f in files]
    assert names == sorted(names)
    assert any(n.startswith("0001") for n in names)
    assert any(n.startswith("0002") for n in names)


class _FlakyBackend:
    name = "flaky"

    def __init__(self):
        self.calls = 0

    def extract_clause_fields(self, unit):
        from contract_agent.errors import ProviderError

        self.calls += 1
        if self.calls == 1:
            raise ProviderError("transient provider error")
        return {"clause_type": "other", "confidence": 0.5, "signals": {}, "key_facts": []}


def test_resilient_backend_retries_safe_methods():
    backend = ResilientBackend(_FlakyBackend(), timeout_seconds=5)
    result = backend.extract_clause_fields(object())
    assert result is not None and result["clause_type"] == "other"


@pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="Postgres integration needs DATABASE_URL")
def test_postgres_store_is_reachable_when_configured():
    from contract_agent.store import PostgresStore

    store = PostgresStore(os.environ["DATABASE_URL"])
    assert store.ping()
