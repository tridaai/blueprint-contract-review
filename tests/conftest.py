"""Suite-wide hermeticity: tests must not depend on the developer's shell.

The suite configures the agent through environment variables, so an
ambient variable exported in the shell that runs pytest (a live
``MODEL_BACKEND=openai`` + API key, a stray ``RUN_TOKEN_BUDGET``, a
repo-root ``.env`` from trying the demo) would otherwise leak into the
tests and change what they exercise.

This autouse fixture runs before every test and:

* scrubs every provider/agent configuration variable from the
  environment (tests that need one set it explicitly with
  ``monkeypatch.setenv``, which runs after this fixture), and
* neutralises ``.env`` loading in every already-imported
  ``contract_agent`` module, so a repo-root ``.env`` cannot
  re-inject values mid-test. ``contract_agent.config.load_dotenv``
  itself is NOT patched — ``test_config.py`` exercises the real loader
  directly.
"""

from __future__ import annotations

import os
import sys

import pytest

# Exact variable names whose ambient values must never leak into a test.
_SCRUB_EXACT = {
    "MODEL_BACKEND",
    "RETRIEVER",
    "RUN_TOKEN_BUDGET",
    "STATE_DB_PATH",
    "CHECKPOINTS",
    "CHECKPOINT_DB_PATH",
    "NODE_TIMEOUT_SECONDS",
    "ACTION_WEBHOOK_URL",
    "ACTION_WEBHOOK_TIMEOUT_SECONDS",
    "API_KEY",
    "GUARDRAIL_REPAIR",
    "GUARDRAIL_REPAIR_MAX_ATTEMPTS",
    "DIAGNOSIS_MAX_TOOL_CALLS",
    "LLM_TIMEOUT_SECONDS",
}

# Variable families scrubbed by prefix / suffix.
_SCRUB_PREFIXES = ("OPENAI_", "ANTHROPIC_", "OLLAMA_", "LLM_", "REVIEWER", "CHROMA_")
_SCRUB_SUFFIXES = ("_API_KEY", "_MODEL", "_BASE_URL")


def _is_agent_config(name: str) -> bool:
    return (
        name in _SCRUB_EXACT
        or name.startswith(_SCRUB_PREFIXES)
        or name.endswith(_SCRUB_SUFFIXES)
    )


@pytest.fixture(autouse=True)
def hermetic_environment(monkeypatch):
    """Scrub ambient agent configuration + disable .env loading."""
    for name in list(os.environ):
        if _is_agent_config(name):
            monkeypatch.delenv(name, raising=False)

    import contract_agent.config as config_module

    real_load_dotenv = config_module.load_dotenv
    for module in list(sys.modules.values()):
        if module is None or module is config_module:
            continue
        if not getattr(module, "__name__", "").startswith("contract_agent"):
            continue
        if getattr(module, "load_dotenv", None) is real_load_dotenv:
            monkeypatch.setattr(module, "load_dotenv", lambda *a, **k: None)
    yield
