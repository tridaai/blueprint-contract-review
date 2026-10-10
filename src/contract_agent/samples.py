"""Bundled synthetic sample contracts.

The samples ship inside the package (``data/sample_contracts.json``)
so the CLI, demo, API, and console work identically from a source
checkout and a pip install. Every contract is synthetic — invented
counterparties and invented terms, written for this blueprint. No
real company or real contract text appears anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

from .schemas import ContractInput

_DATA = Path(__file__).parent / "data" / "sample_contracts.json"


def load_sample_contracts() -> list[dict]:
    return json.loads(_DATA.read_text(encoding="utf-8"))


def sample_contract_models() -> list[ContractInput]:
    return [ContractInput.model_validate(item) for item in load_sample_contracts()]
