"""Provider-mode tests with a fake LLM backend (no network): the LLM
capability methods engage, the cross-check resolves per policy, and
the tool-loop assessment composes — while code still computes every
fact."""

from __future__ import annotations

from contract_agent.graph import run_contract
from contract_agent.model_backends import MockModelBackend
from contract_agent.retriever import KeywordRetriever
from helpers import contract


class FakeLLMBackend(MockModelBackend):
    """A stand-in provider: template drafting + canned LLM capabilities."""

    name = "fake-llm"
    _model = "fake-model-1"

    def __init__(self):
        super().__init__()
        self._usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    def usage_totals(self):
        return dict(self._usage)

    def reset_usage(self):
        self._usage = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    def _tick(self):
        self._usage["calls"] += 1
        self._usage["input_tokens"] += 100
        self._usage["output_tokens"] += 25

    def extract_clause_fields(self, unit):
        self._tick()
        return None  # rules extraction stands; no discrepancies

    def review_clause_with_llm(self, clause):
        self._tick()
        # Agree with the rules on everything except confidentiality,
        # where the fake model is stricter (review_required) — the
        # disagreement must escalate the clause, per policy.
        if clause["clause_type"] == "confidentiality":
            return {
                "position": "review_required",
                "confidence": 0.7,
                "rationale": "One-way reading risk.",
                "backend": self.name,
            }
        return {
            "position": clause.get("_rule_position", "preferred"),
            "confidence": 0.9,
            "rationale": "Matches the playbook.",
            "backend": self.name,
        }

    def assess_with_tools(self, context, tool_specs, dispatch, max_tool_calls):
        self._tick()
        text, _summary = dispatch("clause_facts", {})
        assert "posture" in text
        return {
            "summary": "LLM assessment composed over tool-gathered facts.",
            "risk_factors": ["fake factor"],
            "tool_calls": [{"name": "clause_facts", "summary": "clause_facts -> posture"}],
        }

    def assess(self, context):  # pragma: no cover - tool path is used
        self._tick()
        return {"summary": "LLM assessment.", "risk_factors": []}

    def propose_options(self, context):
        self._tick()
        return None  # template proposals stand

    def verify_draft(self, context):
        self._tick()
        return {"grounded": True, "issues": [], "summary": "grounded"}

    def review_draft(self, context):
        self._tick()
        return {"verdict": "pass", "findings": [], "model": self._model}


def test_provider_mode_crosscheck_and_tool_loop():
    backend = FakeLLMBackend()
    result = run_contract(contract(), backend=backend, retriever=KeywordRetriever())
    by_type = {v.clause_type: v for v in result.clause_verdicts}
    # The confidentiality clause: rules say preferred, the fake LLM
    # disagreed -> escalated to review_required by the policy.
    conf = by_type["confidentiality"]
    assert conf.position == "review_required"
    assert conf.resolution == "escalated_disagreement"
    assert conf.escalated is True
    assert conf.llm_position == "review_required"
    # Assessment came from the tool loop.
    assert result.assessment.source == "llm"
    assert result.assessment.tool_calls
    # Verification and review ran their LLM paths.
    assert result.verification.source == "llm"
    assert result.review.source == "llm"
    # Facts are still computed in code.
    assert result.facts.posture == "negotiable"
    # Telemetry reflects the fake provider's usage.
    assert result.telemetry.backend == "fake-llm"
    assert result.telemetry.input_tokens == backend.usage_totals()["input_tokens"]
