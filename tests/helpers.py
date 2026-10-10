"""Shared sample builders for the pipeline/service tests."""

from __future__ import annotations

from contract_agent.schemas import ContractInput, DocumentInput


CLEAN = """1. Limitation of Liability
Each party's aggregate liability shall not exceed the fees paid in the
twelve (12) months preceding the claim.

2. Indemnification
Each party shall indemnify the other against third-party claims arising
from its breach of this Agreement.

3. Confidentiality
Each party shall hold the other's Confidential Information in confidence
for three (3) years.

4. Term and Termination
Either party may terminate this Agreement for convenience on thirty
(30) days' written notice.

5. Data Protection
Each party shall protect personal data in accordance with the Data
Processing Addendum. A party shall notify the other within 48 hours of
a personal data breach.

6. Governing Law
This Agreement is governed by the laws of the State of Delaware.
"""


def contract(text=CLEAN, **overrides) -> ContractInput:
    base = {
        "contract_id": "CTR-1001",
        "doc_type": "msa",
        "title": "Master Services Agreement",
        "counterparty_name": "Northwind Traders Ltd",
        "our_role": "vendor",
        "documents": [DocumentInput(doc_type="agreement", document_id="AGR-1", raw_text=text)],
    }
    base.update(overrides)
    return ContractInput(**base)


