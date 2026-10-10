"""Configuration: the .env loader and backend selection errors."""

from __future__ import annotations

import pytest

from contract_agent.config import load_dotenv
from contract_agent.model_backends import get_backend


def test_load_dotenv_reads_values_without_overriding_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("MODEL_BACKEND=ollama\n# comment\nOPENAI_API_KEY=from-file\n")
    monkeypatch.setenv("OPENAI_API_KEY", "from-shell")
    monkeypatch.chdir(tmp_path)
    load_dotenv()
    import os

    assert os.environ["MODEL_BACKEND"] == "ollama"  # file value applied
    assert os.environ["OPENAI_API_KEY"] == "from-shell"  # real env wins


def test_default_backend_is_the_offline_mock():
    assert get_backend().name == "mock"


def test_openai_without_key_fails_loudly(monkeypatch):
    monkeypatch.setenv("MODEL_BACKEND", "openai")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is not set"):
        get_backend()


def test_anthropic_without_key_fails_loudly(monkeypatch):
    monkeypatch.setenv("MODEL_BACKEND", "anthropic")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY is not set"):
        get_backend()


def test_unknown_backend_fails_loudly(monkeypatch):
    monkeypatch.setenv("MODEL_BACKEND", "bogus")
    with pytest.raises(ValueError, match="Unknown MODEL_BACKEND"):
        get_backend()


def test_samples_are_valid_and_complete():
    from contract_agent.samples import load_sample_contracts, sample_contract_models

    raw = load_sample_contracts()
    assert len(raw) == 42
    models = sample_contract_models()  # every sample validates against the schema
    assert {m.doc_type for m in models} == {"msa", "sow", "nda", "amendment", "order_form", "dpa", "employment"}
