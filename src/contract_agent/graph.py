"""LangGraph pipeline for the contract review agent.

    ingest → extract → screen → classify → facts → ┌ retrieve ┐
                                                     ├ discrepancies ┤ (fan-out)
                                                     └ history ┘
                          → assess → options → draft → verify → review
                          → validate → gate → approval_gate → END

The three evidence branches (playbook retrieval, extraction
cross-check discrepancies, counterparty memory + counsel feedback)
are independent reads over state the earlier nodes already produced,
so they run as a parallel fan-out; the assessment joins them. The
joined state is identical to a sequential run — the fan-out is an
execution detail, not a behaviour change.

Responsibilities are split on purpose:

- **Code computes**: clause segmentation, typed clause signals,
  playbook verdicts, posture, absence detection, option scores,
  guardrail checks. The model never invents these.
- **The model drafts**: per-clause extraction and an independent
  playbook placement (cross-checked against the rules), the risk
  summary, option wording, the review packet and counterparty draft,
  self-verification, independent review — in provider mode. In the
  default mode deterministic templates take those paths.
- **Counsel decides**: the graph ends at a checkpointed gate. Nothing
  is sent, signed, or filed by any node; approval is recorded by the
  service layer and only then may an opt-in webhook route the packet
  to the customer's system of choice.

The agent flags and explains. It never states a legal conclusion,
never approves a contract, and never sends redlines — every output is
a draft for counsel review, and the guardrails in ``guardrails.py``
enforce that as code.

Resilience: provider-backed nodes run under a per-node timeout with
bounded retries for the steps that are safe to repeat (see
``resilience.py``); drafting and the gate are never retried.

Token budget: with ``RUN_TOKEN_BUDGET`` set, provider steps degrade
to their deterministic paths once cumulative usage crosses the budget
— the run completes on templates instead of failing (see
``_BudgetGuard``).
"""

from __future__ import annotations

import time
from typing import Any, Optional, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from .assessment import build_assessment, citations_for, memory_evidence_lines
from .autonomy import recommend_autonomy
from .clause_review import review_clause
from .clarify import build_information_request
from .config import env_int, env_str, load_dotenv
from .crosscheck import resolve_clause_verdict
from .errors import ProviderError
from .events import NullSink, RunEvent
from .extractor import extract_clauses, extraction_discrepancies
from .facts import compute_facts
from .guardrails import validate_drafts
from .model_backends import DraftContext, clause_rows_text, estimate_cost_usd
from .options import build_options
from .playbook_data import PLAYBOOK
from .resilience import ResilientBackend
from .retriever import get_retriever
from .reviewer import review_drafts
from .schemas import (
    AgentResult,
    Assessment,
    AutonomyRecommendation,
    ClauseExtraction,
    ClauseOption,
    ClauseUnit,
    ClauseVerdict,
    ContractFacts,
    ContractInput,
    Drafts,
    InformationRequest,
    RetrievedPlaybook,
    ReviewResult,
    RunTelemetry,
    TraceStep,
    Validation,
    VerifyResult,
)
from .screening import (
    fence,
    sanitize_text,
    sanitized_clause_texts,
    sanitized_cover,
    screen_contract,
)
from .segmentation import segment_contract
from .store import format_position_counts
from .tools_agent import AssessmentToolBox
from .verify import verify_drafts

# Backend method -> the pipeline node it serves. The budget guard
# degrades per node: once the budget is exceeded, that node's provider
# calls stop and its deterministic path runs instead.
_METHOD_NODE = {
    "extract_clause_fields": "extract",
    "review_clause_with_llm": "classify",
    "assess": "assess",
    "assess_with_tools": "assess",
    "propose_options": "options",
    "draft_documents": "draft",
    "verify_draft": "verify",
    "review_draft": "review",
    "compose_information_request": "gate",
}

# Nodes whose provider steps may degrade under a token budget. The
# gate node itself (autonomy, the counsel decision) never degrades —
# only its optional model call (the information-request wording) does.
_PROVIDER_STEP_NODES = {
    "extract", "classify", "assess", "options", "draft", "verify", "review", "gate",
}


class _BudgetGuard:
    """Backend proxy enforcing ``RUN_TOKEN_BUDGET`` per run.

    Wraps the run's backend: before each provider call in a degradable
    node it checks the cumulative usage reported so far; once usage
    exceeds the budget, the optional capability methods (extraction,
    clause cross-check, assessment, options, verify, review, info-
    request wording) disappear — the nodes read a missing method as
    "use the deterministic path", exactly like the mock backend — and
    ``draft_documents`` degrades to the template backend. The run
    completes; it never fails for budget reasons. ``status()`` reports
    the final accounting for telemetry.
    """

    def __init__(self, backend, template_backend, limit: int | None) -> None:
        object.__setattr__(self, "_backend", backend)
        object.__setattr__(self, "_template", template_backend)
        object.__setattr__(self, "_limit", limit)
        object.__setattr__(self, "_exceeded", False)

    def _over_budget(self) -> bool:
        if self._limit is None:
            return False
        if self._exceeded:
            return True
        usage = self._backend.usage_totals()
        used = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
        if used > self._limit:
            object.__setattr__(self, "_exceeded", True)
            return True
        return False

    def status(self) -> dict | None:
        if self._limit is None:
            return None
        usage = self._backend.usage_totals()
        used = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
        return {"used": used, "limit": self._limit, "exceeded": used > self._limit}

    def __getattr__(self, name: str):
        node = _METHOD_NODE.get(name)
        if node is not None and node in _PROVIDER_STEP_NODES and self._over_budget():
            if name == "draft_documents":
                return getattr(self._template, name)
            return None  # capability absent -> deterministic path
        return getattr(self._backend, name)

    def __setattr__(self, name: str, value) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._backend, name, value)


def _token_budget() -> int | None:
    load_dotenv()
    value = env_int("RUN_TOKEN_BUDGET", 0)
    return value if value and value > 0 else None


def _wrap_backend(backend):
    """Apply the per-node resilience policy (timeout + safe retries)."""
    return ResilientBackend(backend)


def _guardrail_repair_enabled() -> bool:
    load_dotenv()
    return env_str("GUARDRAIL_REPAIR", "on").strip().lower() not in {"off", "0", "false", "no"}


def _guardrail_repair_max_attempts() -> int:
    load_dotenv()
    return min(max(env_int("GUARDRAIL_REPAIR_MAX_ATTEMPTS", 1), 0), 3)


class AgentState(TypedDict, total=False):
    contract: dict[str, Any]
    clauses: list[dict[str, Any]]
    sanitized_texts: dict[str, str]
    extractions: list[dict[str, Any]]
    injection_flags: list[str]
    verdicts: list[dict[str, Any]]
    facts: dict[str, Any]
    playbook: list[dict[str, Any]]
    retrieval_info: str
    discrepancies: list[str]
    history_evidence: list[str]
    history: Optional[dict[str, Any]]
    priors: list[dict[str, Any]]
    assessment: dict[str, Any]
    options: list[dict[str, Any]]
    options_notes: str
    clause_rows: list[dict[str, Any]]
    drafts: dict[str, Any]
    verification: dict[str, Any]
    review: dict[str, Any]
    reviewer_blocked: bool
    validation: dict[str, Any]
    repair_attempted: bool
    repaired: bool
    repair_attempts: int
    original_validation: Optional[dict[str, Any]]
    autonomy: dict[str, Any]
    needs_information: bool
    information_request: Optional[dict[str, Any]]
    token_budget: Optional[dict[str, Any]]
    gate_status: str


# ---------------------------------------------------------------------------
# Shared context builders
# ---------------------------------------------------------------------------


def _rows(state: AgentState) -> list[dict]:
    """Clause verdict rows: the clause table every prompt shares."""
    units = {u["clause_id"]: u for u in state.get("clauses", [])}
    rows = []
    for v in state.get("verdicts", []):
        unit = units.get(v["clause_id"], {})
        rows.append(
            {
                "clause_id": v["clause_id"],
                "ref": unit.get("ref", ""),
                "heading": unit.get("heading", ""),
                "clause_type": v["clause_type"],
                "position": v["position"],
                "refs": v.get("playbook_refs", []),
                "rationale": v.get("rationale", ""),
                "fallback": v.get("fallback_language", ""),
            }
        )
    return rows


def _playbook_text_for(clause_type: str) -> str:
    entries = [e for e in PLAYBOOK if e["clause_type"] == clause_type]
    if not entries:
        entries = [e for e in PLAYBOOK if e["id"].startswith("PB-GEN")]
    return "\n".join(
        f"- [{e['id']}] {e['title']}: {e['text']}"
        + (f" Fallback language: {e['fallback_language']}" if e.get("fallback_language") else "")
        for e in entries
    )


def _draft_context(
    state: AgentState, contract: ContractInput, *, repair_feedback: str | None = None
) -> DraftContext:
    facts = ContractFacts.model_validate(state["facts"])
    playbook = [RetrievedPlaybook.model_validate(g) for g in state.get("playbook", [])]
    options = [ClauseOption.model_validate(o) for o in state.get("options", [])]
    assessment = Assessment.model_validate(state["assessment"])
    verdicts = [ClauseVerdict.model_validate(v) for v in state.get("verdicts", [])]
    recommended = next((o for o in options if o.recommended), None)
    context = DraftContext(
        contract_id=contract.contract_id,
        counterparty_name=contract.counterparty_name,
        our_role=contract.our_role,
        doc_type=contract.doc_type,
        title=contract.title,
        posture=facts.posture,
        positions_summary=format_position_counts(facts.positions_count),
        clause_rows=_rows(state),
        missing_clauses=facts.missing_standard_clauses,
        discrepancies=facts.discrepancies,
        cover_text=sanitized_cover(contract),
        assessment_summary=assessment.summary,
        assessment_risk_factors=assessment.risk_factors,
        playbook_details=[g.model_dump() for g in playbook],
        citations=citations_for(verdicts, [g.model_dump() for g in playbook]),
        options=[o.model_dump() for o in options],
        recommended_option_kind=recommended.kind if recommended else "",
        recommended_option_text=(
            f"{recommended.title} — {recommended.description}" if recommended else ""
        ),
    )
    if repair_feedback:
        context["repair_feedback"] = repair_feedback
    return context


def _failed_checks_text(validation: Validation) -> str:
    return "\n".join(
        f"- {check.name}: {check.detail}" for check in validation.checks if not check.passed
    )


def _failed_count(validation: Validation) -> int:
    return sum(1 for check in validation.checks if not check.passed)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


def _build_graph(backend, retriever, sink, checkpointer=None):
    """Compile the pipeline graph around one run's collaborators."""

    def node(name: str, title: str):
        """Wrap a step function as a graph node that times itself,
        emits its run event, and accumulates ``duration_ms``."""

        def wrap(fn):
            def run(state: AgentState) -> dict[str, Any]:
                started = time.perf_counter()
                update = fn(state)
                duration_ms = round((time.perf_counter() - started) * 1000, 1)
                merged = dict(state.get("_durations") or {})
                merged[name] = duration_ms
                sink.emit(
                    RunEvent(
                        type="node_completed",
                        contract_id=state["contract"]["contract_id"],
                        node=name,
                        duration_ms=duration_ms,
                    )
                )
                return {**update, "_durations": merged}

            run.__name__ = f"node_{name}"
            return run

        return wrap

    @node("ingest", "Intake normalisation + clause segmentation")
    def _ingest(state: AgentState) -> dict[str, Any]:
        contract = ContractInput.model_validate(state["contract"])
        units = segment_contract(contract)
        return {"clauses": [u.model_dump() for u in units]}

    @node("extract", "Per-clause extraction + cross-check")
    def _extract(state: AgentState) -> dict[str, Any]:
        units = [ClauseUnit.model_validate(u) for u in state["clauses"]]
        extractions = extract_clauses(units, backend)
        return {"extractions": [e.model_dump() for e in extractions]}

    @node("screen", "Injection screening of counterparty text")
    def _screen(state: AgentState) -> dict[str, Any]:
        contract = ContractInput.model_validate(state["contract"])
        units = [ClauseUnit.model_validate(u) for u in state["clauses"]]
        return {
            "injection_flags": screen_contract(contract, units),
            "sanitized_texts": sanitized_clause_texts(units),
        }

    @node("classify", "Per-clause playbook review: rules + LLM cross-check")
    def _classify(state: AgentState) -> dict[str, Any]:
        units = [ClauseUnit.model_validate(u) for u in state["clauses"]]
        extractions = [ClauseExtraction.model_validate(e) for e in state["extractions"]]
        sanitized = state.get("sanitized_texts") or {}
        review_fn = getattr(backend, "review_clause_with_llm", None)
        verdicts: list[ClauseVerdict] = []
        for unit, extraction in zip(units, extractions):
            rule = review_clause(unit, extraction)
            if review_fn is None:
                verdicts.append(rule)
                continue
            clause = {
                "clause_id": unit.clause_id,
                "clause_ref": unit.ref or unit.clause_id,
                "heading": unit.heading,
                "clause_type": extraction.clause_type,
                "signals": extraction.signals,
                "playbook_text": _playbook_text_for(extraction.clause_type),
                "clause_text": sanitized.get(unit.clause_id, unit.text),
            }
            try:
                llm = review_fn(clause)
            except Exception:
                # A provider failure on one clause degrades that
                # clause to rules-only — never the whole run.
                llm = None
            verdicts.append(resolve_clause_verdict(rule, llm))
        return {"verdicts": [v.model_dump() for v in verdicts]}

    @node("facts", "Computed facts: posture, counts, absence detection")
    def _facts(state: AgentState) -> dict[str, Any]:
        contract = ContractInput.model_validate(state["contract"])
        units = [ClauseUnit.model_validate(u) for u in state["clauses"]]
        extractions = [ClauseExtraction.model_validate(e) for e in state["extractions"]]
        verdicts = [ClauseVerdict.model_validate(v) for v in state["verdicts"]]
        facts = compute_facts(contract, units, extractions, verdicts)
        return {"facts": facts.model_dump(), "clause_rows": _rows(state)}

    @node("retrieve", "Playbook retrieval")
    def _retrieve(state: AgentState) -> dict[str, Any]:
        contract = ContractInput.model_validate(state["contract"])
        facts = ContractFacts.model_validate(state["facts"])
        verdicts = [ClauseVerdict.model_validate(v) for v in state["verdicts"]]
        worst = [v for v in verdicts if v.position in {"walk_away", "review_required"}]
        query = " ".join(
            part
            for part in [
                contract.title,
                contract.doc_type.replace("_", " "),
                " ".join(sorted(facts.clauses_by_type)),
                " ".join(v.rationale for v in worst),
                " ".join(facts.missing_standard_clauses),
                "playbook preferred position fallback walk-away",
            ]
            if part
        )
        playbook = retriever.retrieve(query, top_k=5)
        return {
            "playbook": [g.model_dump() for g in playbook],
            "retrieval_info": getattr(retriever, "vector_store", "") or "",
        }

    @node("discrepancies", "Extraction cross-check discrepancies")
    def _discrepancies(state: AgentState) -> dict[str, Any]:
        extractions = [ClauseExtraction.model_validate(e) for e in state["extractions"]]
        return {"discrepancies": extraction_discrepancies(extractions)}

    @node("history", "Counterparty memory + counsel feedback")
    def _history(state: AgentState) -> dict[str, Any]:
        return {"history_evidence": memory_evidence_lines(state.get("history"))}

    @node("assess", "Document assessment (bounded tool loop in provider mode)")
    def _assess(state: AgentState) -> dict[str, Any]:
        contract = ContractInput.model_validate(state["contract"])
        facts = ContractFacts.model_validate(state["facts"])
        facts_dict = dict(state["facts"])
        facts_dict["clause_rows"] = _rows(state)
        toolbox = AssessmentToolBox(
            contract=contract,
            facts=facts_dict,
            discrepancies=state.get("discrepancies", []),
            retriever=retriever,
            priors=state.get("priors") or [],
        )
        assessment = build_assessment(
            contract=contract,
            facts=facts,
            verdicts=[ClauseVerdict.model_validate(v) for v in state["verdicts"]],
            clause_rows=_rows(state),
            extractions=[ClauseExtraction.model_validate(e) for e in state["extractions"]],
            playbook=state.get("playbook", []),
            backend=backend,
            history=state.get("history"),
            toolbox=toolbox,
            discrepancies=state.get("discrepancies", []),
            history_lines=state.get("history_evidence", []),
        )
        return {"assessment": assessment.model_dump()}

    @node("options", "Next-step options per flagged clause, scored in code")
    def _options(state: AgentState) -> dict[str, Any]:
        contract = ContractInput.model_validate(state["contract"])
        facts = ContractFacts.model_validate(state["facts"])
        options, notes = build_options(
            contract=contract,
            verdicts=[ClauseVerdict.model_validate(v) for v in state["verdicts"]],
            clause_rows=_rows(state),
            facts=facts,
            assessment_summary=state["assessment"]["summary"],
            playbook=state.get("playbook", []),
            backend=backend,
        )
        update: dict[str, Any] = {"options": [o.model_dump() for o in options]}
        if notes:
            update["options_notes"] = notes
        return update

    @node("draft", "Draft review packet + counterparty response")
    def _draft(state: AgentState) -> dict[str, Any]:
        contract = ContractInput.model_validate(state["contract"])
        subject, response, packet = backend.draft_documents(_draft_context(state, contract))
        return {
            "drafts": Drafts(
                subject=subject, counterparty_response=response, review_packet=packet
            ).model_dump()
        }

    @node("verify", "Self-verification of the drafts")
    def _verify(state: AgentState) -> dict[str, Any]:
        contract = ContractInput.model_validate(state["contract"])
        facts = ContractFacts.model_validate(state["facts"])
        drafts = Drafts.model_validate(state["drafts"])
        verdicts = [ClauseVerdict.model_validate(v) for v in state["verdicts"]]
        retrieved_ids = citations_for(verdicts, state.get("playbook", []))
        result = verify_drafts(
            contract=contract,
            facts=facts,
            clause_rows=_rows(state),
            drafts=drafts,
            retrieved_ids=retrieved_ids,
            backend=backend,
        )
        return {"verification": result.model_dump()}

    @node("review", "Independent review of the drafts")
    def _review(state: AgentState) -> dict[str, Any]:
        contract = ContractInput.model_validate(state["contract"])
        facts = ContractFacts.model_validate(state["facts"])
        drafts = Drafts.model_validate(state["drafts"])
        verdicts = [ClauseVerdict.model_validate(v) for v in state["verdicts"]]
        retrieved_ids = citations_for(verdicts, state.get("playbook", []))
        result = review_drafts(
            contract=contract,
            facts=facts,
            clause_rows=_rows(state),
            assessment=Assessment.model_validate(state["assessment"]),
            verdicts=verdicts,
            options=[ClauseOption.model_validate(o) for o in state.get("options", [])],
            drafts=drafts,
            retrieved_ids=retrieved_ids,
            backend=backend,
        )
        return {
            "review": result.model_dump(),
            "reviewer_blocked": bool(result.enabled and result.verdict == "block"),
        }

    @node("validate", "Guardrail checks (+ bounded repair)")
    def _validate(state: AgentState) -> dict[str, Any]:
        contract = ContractInput.model_validate(state["contract"])
        verdicts = [ClauseVerdict.model_validate(v) for v in state["verdicts"]]
        citations = citations_for(verdicts, state.get("playbook", []))
        flagged_refs = [
            v.playbook_refs for v in verdicts if v.position != "preferred"
        ]
        drafts = Drafts.model_validate(state["drafts"])
        validation = validate_drafts(contract, drafts, citations, flagged_refs)
        update: dict[str, Any] = {
            "validation": validation.model_dump(),
            "repair_attempted": False,
            "repaired": False,
            "repair_attempts": 0,
            "original_validation": None,
        }
        if validation.passed or not _guardrail_repair_enabled():
            return update
        max_attempts = _guardrail_repair_max_attempts()
        original = validation
        attempts = 0
        while attempts < max_attempts and not validation.passed:
            attempts += 1
            feedback = _failed_checks_text(validation)
            try:
                subject, response, packet = backend.draft_documents(
                    _draft_context(state, contract, repair_feedback=feedback)
                )
            except Exception:
                break  # a failed redraft keeps the original drafts + failures
            candidate = Drafts(
                subject=subject, counterparty_response=response, review_packet=packet
            )
            candidate_validation = validate_drafts(contract, candidate, citations, flagged_refs)
            if candidate_validation.passed or _failed_count(candidate_validation) < _failed_count(validation):
                drafts, validation = candidate, candidate_validation
        update.update(
            {
                "drafts": drafts.model_dump(),
                "validation": validation.model_dump(),
                "repair_attempted": attempts > 0,
                "repaired": validation.passed,
                "repair_attempts": attempts,
                "original_validation": original.model_dump(),
            }
        )
        return update

    @node("gate", "Autonomy recommendation + information request")
    def _gate(state: AgentState) -> dict[str, Any]:
        contract = ContractInput.model_validate(state["contract"])
        facts = ContractFacts.model_validate(state["facts"])
        validation = Validation.model_validate(state["validation"])
        review = (
            ReviewResult.model_validate(state["review"]) if state.get("review") else None
        )
        autonomy = recommend_autonomy(
            verdicts=[ClauseVerdict.model_validate(v) for v in state["verdicts"]],
            facts=facts,
            validation=validation,
            repair_attempted=bool(state.get("repair_attempted")),
            review=review,
            reviewer_blocked=bool(state.get("reviewer_blocked")),
        )
        info = build_information_request(
            contract=contract,
            facts=facts,
            backend=backend,
        )
        return {
            "autonomy": autonomy.model_dump(),
            "needs_information": bool(info and info.needed),
            "information_request": info.model_dump() if info else None,
            "gate_status": "awaiting_counsel",
        }

    def _approval_gate(state: AgentState) -> dict[str, Any]:
        """The counsel gate: pause the thread until a decision resumes it.

        With a checkpointer the run pauses here (``interrupt``) and the
        service's approve/reject resumes the same thread with the
        decision payload. Without one this node is a pass-through and
        the decision lives only in the store — same outcome, less
        ceremony. Either way no node after this one exists: END.
        """
        if checkpointer is None:
            return {"gate_status": state.get("gate_status", "awaiting_counsel")}
        facts = state.get("facts") or {}
        validation = state.get("validation") or {}
        decision = interrupt(
            {
                "contract_id": state["contract"]["contract_id"],
                "status": "awaiting_counsel",
                "posture": facts.get("posture"),
                "worst_position": facts.get("worst_position"),
                "validation_passed": validation.get("passed"),
                "external_action_taken": False,
            }
        )
        if isinstance(decision, dict) and decision.get("decision") in (
            "approved",
            "rejected",
        ):
            return {"gate_status": decision["decision"]}
        return {"gate_status": "awaiting_counsel"}

    builder = StateGraph(AgentState)
    for name, fn in [
        ("ingest", _ingest),
        ("extract", _extract),
        ("screen", _screen),
        ("classify", _classify),
        ("facts", _facts),
        ("retrieve", _retrieve),
        ("discrepancies", _discrepancies),
        ("history", _history),
        ("assess", _assess),
        ("options", _options),
        ("draft", _draft),
        ("verify", _verify),
        ("review", _review),
        ("validate", _validate),
        ("gate", _gate),
        ("approval_gate", _approval_gate),
    ]:
        builder.add_node(name, fn)
    builder.set_entry_point("ingest")
    builder.add_edge("ingest", "extract")
    builder.add_edge("extract", "screen")
    builder.add_edge("screen", "classify")
    builder.add_edge("classify", "facts")
    # Evidence fan-out: three independent branches off facts,
    # joined by assess.
    builder.add_edge("facts", "retrieve")
    builder.add_edge("facts", "discrepancies")
    builder.add_edge("facts", "history")
    builder.add_edge("retrieve", "assess")
    builder.add_edge("discrepancies", "assess")
    builder.add_edge("history", "assess")
    builder.add_edge("assess", "options")
    builder.add_edge("options", "draft")
    builder.add_edge("draft", "verify")
    builder.add_edge("verify", "review")
    builder.add_edge("review", "validate")
    builder.add_edge("validate", "gate")
    builder.add_edge("gate", "approval_gate")
    builder.add_edge("approval_gate", END)
    return builder.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# Trace + telemetry + entry points
# ---------------------------------------------------------------------------


def _durations(state: AgentState) -> dict[str, float]:
    return dict(state.get("_durations") or {})


def _build_trace(state: AgentState) -> list[TraceStep]:
    """Assemble the human-readable step trace from the final state."""
    d = _durations(state)
    facts = ContractFacts.model_validate(state["facts"])
    verdicts = [ClauseVerdict.model_validate(v) for v in state.get("verdicts", [])]
    playbook = [RetrievedPlaybook.model_validate(g) for g in state.get("playbook", [])]
    options = [ClauseOption.model_validate(o) for o in state.get("options", [])]
    assessment = Assessment.model_validate(state["assessment"])
    validation = Validation.model_validate(state["validation"])
    verification = (
        VerifyResult.model_validate(state["verification"])
        if state.get("verification")
        else None
    )
    review = ReviewResult.model_validate(state["review"]) if state.get("review") else None

    def step(name: str, title: str, details: list[str]) -> TraceStep:
        return TraceStep(name=name, title=title, details=details, duration_ms=d.get(name, 0.0))

    steps = [
        step(
            "ingest",
            "Intake + clause segmentation",
            [
                f"{facts.clause_count} clause unit(s) cut from the agreement text"
                + (f"; {facts.unheaded_units} unheaded" if facts.unheaded_units else ""),
                f"exhibits referenced: {', '.join(facts.exhibits_referenced) or 'none'}"
                f" · missing: {', '.join(facts.exhibits_missing) or 'none'}",
                f"signature pages present: {facts.signature_pages_present}",
            ],
        ),
        step(
            "extract",
            "Per-clause extraction + cross-check",
            [
                f"{e['clause_id']}: {e['clause_type']} (confidence {e['type_confidence']}, "
                f"via {e['type_source']})"
                + (f" — signals: {', '.join(f'{k}={v}' for k, v in e['signals'].items() if v not in (None, '', False))}" if e.get("signals") else "")
                for e in state.get("extractions", [])
            ]
            + (
                [f"{len(state.get('discrepancies', []))} cross-check discrepancy(ies) recorded"]
                if state.get("discrepancies")
                else []
            ),
        ),
        step(
            "screen",
            "Injection screening",
            state.get("injection_flags")
            or ["no injection patterns found in counterparty text"],
        ),
        step(
            "classify",
            "Per-clause playbook review (rules + LLM cross-check)",
            [
                f"{v.clause_id} [{v.clause_type}]: {v.position} "
                f"(rules: {v.rule_position}; resolution: {v.resolution}"
                + (f"; LLM: {v.llm_position}" if v.llm_position else "")
                + ("; ESCALATED by disagreement" if v.escalated else "")
                + ")"
                for v in verdicts
            ],
        ),
        step(
            "facts",
            "Computed facts",
            [
                f"posture {facts.posture} · worst position {facts.worst_position}",
                f"positions: {format_position_counts(facts.positions_count)}",
                *(
                    [f"missing standard clauses: {', '.join(facts.missing_standard_clauses)}"]
                    if facts.missing_standard_clauses
                    else []
                ),
                *(
                    [f"CRITICAL absences: {', '.join(facts.critical_absences)}"]
                    if facts.critical_absences
                    else []
                ),
            ],
        ),
        step(
            "retrieve",
            "Playbook retrieval",
            [
                f"[{g.playbook_id}] {g.title} (score {g.score}, via "
                f"{'+'.join(g.sources) or 'keyword'})"
                for g in playbook
            ]
            or ["no playbook entries retrieved"],
        ),
        step(
            "assess",
            "Document assessment",
            [
                f"{assessment.summary} (source: {assessment.source})",
                *([f"note: {assessment.note}"] if assessment.note else []),
                *[f"tool: {tc.get('name')} — {tc.get('summary')}" for tc in assessment.tool_calls],
                *assessment.evidence,
            ],
        ),
        step(
            "options",
            "Next-step options (scored in code)",
            [
                f"[{o.kind}] clause {o.clause_id or '—'}: {o.title} — score {o.score}"
                + (" ← recommended" if o.recommended else "")
                for o in options
            ]
            + ([f"note: {state['options_notes']}"] if state.get("options_notes") else []),
        ),
        step(
            "draft",
            "Drafts composed",
            [
                "review packet + counterparty response drafted — nothing sent, "
                "nothing agreed, no legal conclusion stated"
            ],
        ),
    ]
    if verification is not None:
        steps.append(
            step(
                "verify",
                "Self-verification",
                [
                    f"grounded: {verification.grounded} (source: {verification.source})",
                    *[f"issue: {i}" for i in verification.issues],
                    *([f"note: {verification.note}"] if verification.note else []),
                ],
            )
        )
    if review is not None:
        steps.append(
            step(
                "review",
                "Independent review",
                [
                    f"verdict: {review.verdict} (source: {review.source}, "
                    f"enabled: {review.enabled})",
                    *[f"finding: {f}" for f in review.findings],
                    *(
                        ["⛔ reviewer blocked — flagged for counsel"]
                        if state.get("reviewer_blocked")
                        else []
                    ),
                ],
            )
        )
    validate_details = [
        f"[{'PASS' if c.passed else 'FAIL'}] {c.name} — {c.detail}"
        for c in validation.checks
    ]
    if state.get("repair_attempted"):
        validate_details.append(
            f"repair: {state.get('repair_attempts')} attempt(s) — "
            f"{'repaired' if state.get('repaired') else 'still failing'}"
        )
    steps.append(step("validate", "Guardrail checks", validate_details))
    autonomy = state.get("autonomy") or {}
    gate_details = [
        f"autonomy recommendation: "
        f"{'eligible for auto-triage' if autonomy.get('eligible_for_auto_triage') else 'counsel decision required'}"
        " (recommendation only — never acted on)",
        *autonomy.get("reasons", []),
        *[f"blocker: {b}" for b in autonomy.get("blockers", [])],
    ]
    if state.get("needs_information"):
        gate_details.append(
            "information request composed for the counterparty (not sent) — missing: "
            + "; ".join((state.get("information_request") or {}).get("missing_items", []))
        )
    steps.append(step("gate", "Counsel gate", gate_details))
    steps.append(
        step(
            "approval_gate",
            "Counsel decision",
            [
                f"status: {state.get('gate_status', 'awaiting_counsel')} · "
                "external action taken: False (nothing is sent, signed, "
                "or filed by the agent)"
            ],
        )
    )
    return steps


def _run_telemetry(backend, started: float, budget_status: dict | None) -> dict:
    """Assemble the run's telemetry block from the backend's accounting.

    The mock backend reports zero tokens for its render calls — those
    are template renders, not model usage — so mock runs record token
    counts as None ("no model ran") rather than dressing them up.
    """
    usage = backend.usage_totals()
    is_mock = getattr(backend, "name", "") == "mock"
    input_tokens = None if is_mock else int(usage.get("input_tokens", 0))
    output_tokens = None if is_mock else int(usage.get("output_tokens", 0))
    model = getattr(backend, "_model", "") or ""
    return {
        "backend": getattr(backend, "name", "unknown"),
        "model": model,
        "model_calls": int(usage.get("calls", 0)),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": estimate_cost_usd(model, input_tokens, output_tokens),
        "latency_seconds": round(time.perf_counter() - started, 3),
        "budget": budget_status,
    }


def run_contract(
    contract: ContractInput,
    backend=None,
    retriever=None,
    history: dict | None = None,
    priors: list[dict] | None = None,
    event_sink=None,
    checkpointer: BaseCheckpointSaver | None = None,
    thread_id: str | None = None,
) -> AgentResult:
    """Run one contract through the pipeline to the counsel gate.

    ``backend``/``retriever`` default to the environment-configured
    ones; ``history``/``priors`` carry the store's memory for this
    counterparty (the service layer supplies them); ``event_sink``
    receives structured run events; ``checkpointer`` + ``thread_id``
    pause the run at the gate for a later resume.
    """
    from .model_backends import MockModelBackend, get_backend

    started = time.perf_counter()
    sink = event_sink or NullSink()
    raw_backend = backend or get_backend()
    if hasattr(raw_backend, "reset_usage"):
        raw_backend.reset_usage()
    budget = _BudgetGuard(raw_backend, MockModelBackend(), _token_budget())
    wrapped = _wrap_backend(budget)
    retriever = retriever or get_retriever()
    graph = _build_graph(wrapped, retriever, sink, checkpointer=checkpointer)
    initial: AgentState = {
        "contract": contract.model_dump(),
        "history": history,
        "priors": priors or [],
    }
    sink.emit(
        RunEvent(
            type="run_started",
            contract_id=contract.contract_id,
            detail={"counterparty_name": contract.counterparty_name},
        )
    )
    config = (
        {"configurable": {"thread_id": thread_id or contract.contract_id}}
        if checkpointer is not None
        else None
    )
    try:
        if config is not None:
            state = graph.invoke(initial, config)
            interrupts = state.get("__interrupt__") or []
            if interrupts:
                # Paused at the gate: read the working state back from
                # the checkpoint — it is the state the decision will
                # resume, and the store records it as awaiting.
                snapshot = graph.get_state(config)
                state = dict(snapshot.values)
        else:
            state = graph.invoke(initial)
    except ProviderError:
        raise
    except Exception as exc:
        sink.emit(
            RunEvent(
                type="run_failed",
                contract_id=contract.contract_id,
                detail={"error": f"{type(exc).__name__}: {exc}"},
            )
        )
        raise
    result = AgentResult(
        contract_id=contract.contract_id,
        status="awaiting_counsel",
        counterparty_name=contract.counterparty_name,
        doc_type=contract.doc_type,
        clauses=[ClauseUnit.model_validate(u) for u in state.get("clauses", [])],
        extractions=[ClauseExtraction.model_validate(e) for e in state.get("extractions", [])],
        facts=ContractFacts.model_validate(state["facts"]),
        injection_flags=state.get("injection_flags", []),
        clause_verdicts=[ClauseVerdict.model_validate(v) for v in state.get("verdicts", [])],
        playbook=[RetrievedPlaybook.model_validate(g) for g in state.get("playbook", [])],
        vector_store=state.get("retrieval_info", ""),
        assessment=Assessment.model_validate(state["assessment"]),
        options=[ClauseOption.model_validate(o) for o in state.get("options", [])],
        drafts=Drafts.model_validate(state["drafts"]) if state.get("drafts") else None,
        verification=(
            VerifyResult.model_validate(state["verification"])
            if state.get("verification")
            else None
        ),
        review=ReviewResult.model_validate(state["review"]) if state.get("review") else None,
        reviewer_blocked=bool(state.get("reviewer_blocked")),
        validation=(
            Validation.model_validate(state["validation"])
            if state.get("validation")
            else None
        ),
        repair_attempted=bool(state.get("repair_attempted")),
        repaired=bool(state.get("repaired")),
        repair_attempts=int(state.get("repair_attempts", 0)),
        original_validation=(
            Validation.model_validate(state["original_validation"])
            if state.get("original_validation")
            else None
        ),
        needs_information=bool(state.get("needs_information")),
        trace=_build_trace(state),
    )
    if state.get("information_request"):
        result.information_request = InformationRequest.model_validate(
            state["information_request"]
        )
    if state.get("autonomy"):
        result.autonomy = AutonomyRecommendation.model_validate(state["autonomy"])
    result.telemetry = RunTelemetry.model_validate(
        _run_telemetry(raw_backend, started, budget.status())
    )
    sink.emit(
        RunEvent(
            type="run_completed",
            contract_id=contract.contract_id,
            detail={"status": result.status},
        )
    )
    return result


def resume_decision(
    contract_id: str,
    decision: dict,
    checkpointer: BaseCheckpointSaver,
    backend=None,
    retriever=None,
) -> bool:
    """Resume a gate-paused thread with counsel's decision.

    Returns True when a paused thread was found and completed, False
    when there is nothing to resume (checkpoints off during analysis,
    thread already finished). The decision of record lives in the
    store; this only closes out the graph thread.
    """
    from .model_backends import get_backend

    sink = NullSink()
    graph = _build_graph(
        _wrap_backend(backend or get_backend()),
        retriever or get_retriever(),
        sink,
        checkpointer=checkpointer,
    )
    config = {"configurable": {"thread_id": contract_id}}
    snapshot = graph.get_state(config)
    if not snapshot or not snapshot.next:
        return False
    from langgraph.types import Command

    graph.invoke(Command(resume=decision), config)
    return True
