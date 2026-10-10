"""Model backends.

Default = ``MockModelBackend``: deterministic, offline, no API key. This is
the offline fallback — what the tests, the evals, and the default demo run
use, so results are reproducible. It is a fallback, not the product: the
standard run is a real provider API (see the README's configuration
table).

Optional = OpenAI / Anthropic / Ollama backends, selected with
``MODEL_BACKEND``. Ollama is the fully local option: a preset over the
OpenAI-compatible client pointed at a local Ollama server, no API key.
Any other OpenAI-compatible hosted provider (LiteLLM, Together, Groq,
NVIDIA NIM, …) works through the plain OpenAI backend + ``OPENAI_BASE_URL``.
All surfaces (API, CLI, traced demo) honour the same variables. The LLM
backends genuinely call the provider APIs through the official SDKs (the
optional ``llm`` extra) and feed the same pipeline as the mock: the graph
assembles the review packet and playbook citations, and the guardrails in
``guardrails.py`` run on the model's drafts afterwards, exactly as they
do on template drafts.

Extra LLM-only capabilities: when a real backend is active, the graph
also uses it for per-clause extraction (``extractor.py``), an independent
per-clause playbook placement that is cross-checked against the
deterministic rules (``crosscheck.py``), the document assessment, and
next-step proposals. The mock backend deliberately exposes none of those
methods, so none of them can fire in the default mode — the deterministic
fallbacks run instead.

Missing key or missing SDK raises a loud, actionable ``RuntimeError`` —
the backends never run implicitly and never fail silently.
"""

from __future__ import annotations

import json
import re

from .config import env_float, env_str, load_dotenv
from .errors import translate_construction_error, translate_provider_error
from .prompts import (
    ASSESS_SYSTEM_PROMPT,
    ASSESS_TOOLS_SYSTEM_PROMPT,
    ASSESS_USER_TEMPLATE,
    DRAFT_SYSTEM_PROMPT,
    DRAFT_USER_TEMPLATE,
    EXTRACT_SYSTEM_PROMPT,
    EXTRACT_USER_TEMPLATE,
    INFO_REQUEST_SYSTEM_PROMPT,
    INFO_REQUEST_USER_TEMPLATE,
    OPTIONS_SYSTEM_PROMPT,
    OPTIONS_USER_TEMPLATE,
    REVIEWER_SYSTEM_PROMPT,
    REVIEWER_USER_TEMPLATE,
    REVIEW_CLAUSE_SYSTEM_PROMPT,
    REVIEW_CLAUSE_USER_TEMPLATE,
    VERIFY_SYSTEM_PROMPT,
    VERIFY_USER_TEMPLATE,
)

# Shared request timeout for provider API calls (seconds), overridable
# with the LLM_TIMEOUT_SECONDS environment variable.
DEFAULT_TIMEOUT_SECONDS = 60.0

# Indicative USD list prices per 1M tokens (input, output) for the
# telemetry cost ESTIMATE. Verify against your provider — prices move.
# A model missing from the table reports cost as None (unknown), never
# a guessed number; local models price at 0.
PRICE_TABLE: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "llama3.1": (0.0, 0.0),
    "nomic-embed-text": (0.0, 0.0),
}


def estimate_cost_usd(
    model: str | None, input_tokens: int | None, output_tokens: int | None
) -> float | None:
    """Estimated USD cost of a run's token usage, or None when the
    model is not in the price table (or usage is unknown)."""
    if not model or model not in PRICE_TABLE or input_tokens is None or output_tokens is None:
        return None
    input_rate, output_rate = PRICE_TABLE[model]
    return round(
        (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000, 6
    )

_POSITIONS = {"preferred", "acceptable_fallback", "review_required", "walk_away"}


class DraftContext(dict):
    """Loose dict of values used to render drafts (see graph.draft node)."""


# The ModelBackend protocol is declared in ports.py (the seam
# registry); it is re-exported here so existing imports keep working.
from .ports import ModelBackend  # noqa: E402,F401


_NEXT_STEP = {
    "accept_as_preferred": "no change is requested on the recommended clause; it already matches our preferred position.",
    "propose_fallback_language": "we propose the fallback wording set out in the review packet for the recommended clause, and we will review your revised draft when it arrives.",
    "escalate_to_senior_counsel": "the recommended clause is with our senior counsel for a decision; we will come back to you once counsel has reviewed it.",
    "request_counterparty_info": "we still need the items listed in the review packet before this clause can be decided; the next step is receiving them.",
}

_POSTURE_SENTENCE = {
    "aligned": "The draft aligns with our playbook on the clauses reviewed, subject to counsel's final check.",
    "negotiable": "The draft is workable, with the changes listed below requested before counsel can clear it.",
    "escalation_required": "The draft contains positions our playbook treats as walk-away items or critical gaps, and it is with senior counsel.",
}


def clause_rows_text(rows: list[dict]) -> str:
    """Render clause verdict rows (dicts built by the graph) as text —
    the clause table every prompt and the mock packet share."""
    lines = []
    for row in rows or []:
        refs = " ".join(f"[{r}]" for r in row.get("refs") or []) or "no playbook citation"
        label = row.get("ref") or row.get("clause_id")
        heading = f" — {row['heading']}" if row.get("heading") else ""
        lines.append(
            f"- {row.get('clause_id')} [{label}{heading}] {row.get('clause_type')}: "
            f"{row.get('position')} {refs} — {row.get('rationale') or ''}"
        )
        if row.get("fallback"):
            lines.append(f"  Proposed fallback language: {row['fallback']}")
    return "\n".join(lines) or "no clauses reviewed"


class MockModelBackend:
    """Deterministic template renderer. No network, no randomness.

    The template quotes the counterparty's cover note verbatim in the
    response draft, the way a busy drafter pastes what arrived. That is
    deliberate — and it is why the guardrail layer matters: unvetted
    source text can carry language (a cover email pressing "confirm
    you accept all terms today") that must never reach a counterparty
    draft. The adversarial samples demonstrate exactly that path: the
    guardrails flag the quoted sentence and the bounded repair strips
    it before counsel ever sees the draft.
    """

    name = "mock"

    def __init__(self) -> None:
        self._calls = 0

    def usage_totals(self) -> dict:
        """Call accounting for telemetry. Token counts are honestly
        zero-shaped here — no model ran, and ``run_contract`` reports
        mock tokens as None rather than dressing up a template render
        as model usage. Only the call count is real."""
        return {"input_tokens": 0, "output_tokens": 0, "calls": getattr(self, "_calls", 0)}

    def reset_usage(self) -> None:
        self._calls = 0

    def draft_documents(self, context: DraftContext) -> tuple[str, str, str]:
        self._calls = getattr(self, "_calls", 0) + 1
        posture = str(context.get("posture") or "negotiable")
        subject = (
            f"Contract {context['contract_id']} — {context['counterparty_name']} "
            f"({context['doc_type']}): first-pass review complete (draft for counsel review)"
        )
        citations = context.get("citations") or []
        citation_text = " ".join(f"[{c}]" for c in citations)
        rows = context.get("clause_rows") or []
        flagged = [
            r for r in rows
            if r.get("position") in {"acceptable_fallback", "review_required", "walk_away"}
        ]
        recommended_kind = str(context.get("recommended_option_kind") or "")
        next_step = _NEXT_STEP.get(
            recommended_kind,
            "our counsel is reviewing the draft and we will come back to you.",
        )

        packet_lines = [
            f"REVIEW PACKET — contract {context['contract_id']} (DRAFT FOR COUNSEL REVIEW — not a legal conclusion)",
            f"Counterparty: {context['counterparty_name']} · Our role: {context.get('our_role')}",
            f"Type: {context['doc_type']} · Title: {context.get('title') or 'untitled'}",
            f"Document posture (computed): {posture} · Positions: {context.get('positions_summary') or 'none'}",
            f"Missing standard clauses (computed): {', '.join(context.get('missing_clauses') or []) or 'none'}",
            f"Assessment: {context.get('assessment_summary') or 'not recorded'}",
            "Risk factors:",
        ]
        packet_lines += [
            f"- {factor}" for factor in (context.get("assessment_risk_factors") or [])
        ] or ["- none recorded"]
        packet_lines.append("Clause-by-clause review:")
        packet_lines.append(clause_rows_text(rows))
        packet_lines.append("Options scored by review code:")
        packet_lines += [
            f"- [{o['kind']}] {o['title']} (clause {o.get('clause_id') or '—'}) — score {o['score']}"
            + (" (recommended)" if o.get("recommended") else "")
            for o in (context.get("options") or [])
        ]
        packet_lines.append(f"Playbook citations: {citation_text}")
        packet_lines.append(
            "Status: draft for counsel review — awaiting counsel's decision. "
            "This packet flags positions against the playbook; it is not a "
            "legal conclusion and it approves nothing."
        )
        packet = "\n".join(packet_lines)

        paragraphs = [
            f"Dear {context['counterparty_name']} team,",
            (
                f"Thank you for the draft {context['doc_type']} ({context.get('title') or 'untitled'}). "
                f"{_POSTURE_SENTENCE[posture]} Our requested changes, clause by clause, are:"
            ),
        ]
        changes = [
            f"- Clause {r.get('ref') or r['clause_id']} ({r.get('heading') or r['clause_type']}): "
            f"requested position per our playbook [{', '.join(r.get('refs') or [])}]"
            + (f" — proposed wording: {r['fallback']}" if r.get("fallback") else "")
            for r in flagged
        ]
        paragraphs.append("\n".join(changes) if changes else "No changes are requested on the clauses reviewed.")
        if context.get("missing_clauses"):
            paragraphs.append(
                "The draft also omits standard clauses we expect to see: "
                + ", ".join(context["missing_clauses"])
                + ". Please include them in your revised draft."
            )
        if context.get("cover_text") and not context.get("repair_feedback"):
            # First pass only: the template pastes the cover note in
            # verbatim, the way a busy drafter does. A repair redraft
            # (validation feedback present) is the careful pass — the
            # unvetted quote is exactly what it drops.
            paragraphs.append(f"For reference, your note: \"{context['cover_text']}\"")
        paragraphs += [
            f"Next step: {next_step}",
            (
                "This draft response is issued for discussion only. Nothing in "
                "it accepts, approves, or executes the contract, and no term "
                "is agreed until a written agreement is signed by both "
                "parties.\nLegal Operations (draft for counsel review)"
            ),
        ]
        response = "\n\n".join(paragraphs)
        return subject, response, packet


def _missing_key_error(backend: str, variable: str) -> RuntimeError:
    return RuntimeError(
        f"MODEL_BACKEND={backend} but {variable} is not set. "
        f"Add {variable}=<your key> to the .env file in the repo root "
        "(copy .env.example to .env) or export it in your shell, then re-run."
    )


def _missing_sdk_error(backend: str, package: str) -> RuntimeError:
    return RuntimeError(
        f"MODEL_BACKEND={backend} needs the {package} SDK, which is not installed. "
        "Install the optional llm extra: uv sync --extra llm "
        "(pip fallback: pip install -e \".[llm]\"), then re-run."
    )


def parse_json_object(text: str) -> dict | None:
    """Parse a model reply that should be a single JSON object.

    Tolerates markdown fences and surrounding prose by falling back to
    the outermost ``{...}`` span. Returns ``None`` when nothing usable
    parses — callers treat an unparseable LLM reply as "no result" and
    fall back to the deterministic path, never as a run failure.
    """
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(cleaned[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    return data if isinstance(data, dict) else None


def parse_json_list(text: str) -> list | None:
    """Parse a model reply that should be a JSON array (or an object
    wrapping one under an ``options`` key). ``None`` when unusable."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        start, end = cleaned.find("["), cleaned.rfind("]")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(cleaned[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    if isinstance(data, dict) and isinstance(data.get("options"), list):
        return data["options"]
    return data if isinstance(data, list) else None


def _parse_verdict(text: str, backend_name: str) -> dict | None:
    """Parse an LLM clause-placement reply; ``None`` when unusable.

    The cross-check degrades gracefully, so a malformed reply records
    "no LLM placement" instead of failing the run — the rule verdict
    stands on its own.
    """
    data = parse_json_object(text)
    if data is None:
        return None
    position = str(data.get("position", "")).strip().lower()
    if position not in _POSITIONS:
        return None
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None
    return {
        "position": position,
        "confidence": min(max(confidence, 0.0), 1.0),
        "rationale": str(data.get("rationale", ""))[:500],
        "backend": backend_name,
    }


class _BaseLLMBackend:
    name = "llm"

    def _complete(self, system: str, user: str, max_tokens: int = 600) -> str:
        """One provider completion. Implemented by each provider backend."""
        raise NotImplementedError

    # -- usage accounting (the LLM eval pack reports tokens + cost) ------
    def _record_usage(self, usage: dict) -> None:
        totals = getattr(self, "_usage_totals", None)
        if totals is None:
            totals = self._usage_totals = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
        totals["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
        totals["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
        totals["calls"] += 1

    def usage_totals(self) -> dict:
        """Cumulative provider usage since construction / last reset."""
        return dict(
            getattr(self, "_usage_totals", {"input_tokens": 0, "output_tokens": 0, "calls": 0})
        )

    def reset_usage(self) -> None:
        self._usage_totals = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

    def complete_with_usage(
        self, system: str, user: str, max_tokens: int = 600, model: str | None = None
    ) -> tuple[str, dict | None]:
        """One completion plus the usage it consumed (``None`` when the
        provider/fake reported none). Used by the LLM-judge eval pack,
        where the judge may run on a different model than the pipeline."""
        original_model = self._model
        if model:
            self._model = model
        before = self.usage_totals()
        try:
            text = self._complete(system, user, max_tokens=max_tokens)
        finally:
            self._model = original_model
        after = self.usage_totals()
        delta = {k: after[k] - before[k] for k in after}
        return text, (delta if delta["calls"] else None)

    def _playbook_block(self, context: DraftContext) -> str:
        return "\n".join(
            f"- [{g.get("playbook_id") or g.get("policy_id") or g.get("guideline_id")}] {g['title']}: {g['text']}"
            + (f"\n  Fallback language: {g['fallback_language']}" if g.get("fallback_language") else "")
            for g in context.get("playbook_details", [])
        ) or "none retrieved"

    def _facts_block(self, context: DraftContext) -> dict:
        """The template variables every contract prompt shares."""
        return {
            "contract_id": context["contract_id"],
            "counterparty_name": context.get("counterparty_name") or "unknown counterparty",
            "our_role": context.get("our_role") or "customer",
            "doc_type": context.get("doc_type") or "general",
            "title": context.get("title") or "untitled",
            "posture": context.get("posture") or "negotiable",
            "positions_summary": context.get("positions_summary") or "none",
            "clause_table": clause_rows_text(context.get("clause_rows") or []),
            "missing_clauses": ", ".join(context.get("missing_clauses") or []) or "none",
            "discrepancies": "; ".join(context.get("discrepancies", [])) or "none",
            "cover_text": context.get("cover_text") or "none recorded",
            "playbook": self._playbook_block(context),
        }

    def _render_prompt(self, context: DraftContext) -> str:
        prompt = DRAFT_USER_TEMPLATE.format(
            **self._facts_block(context),
            assessment=context.get("assessment_summary") or "none recorded",
            recommended_option=context.get("recommended_option_text") or "none scored",
        )
        feedback = context.get("repair_feedback")
        if feedback:
            # Bounded-repair redraft: the previous attempt failed
            # validation — hand the model exactly what to fix.
            prompt += (
                "\n\nCORRECTION REQUIRED — a previous draft of these "
                "documents failed validation. Rewrite both documents, "
                "fixing every problem below, and introduce no claim the "
                f"facts above do not support:\n{feedback}"
            )
        return prompt

    @staticmethod
    def _split_subject(text: str, fallback_subject: str) -> tuple[str, str]:
        lines = text.strip().splitlines()
        if lines and lines[0].lower().startswith("subject:"):
            return lines[0].split(":", 1)[1].strip(), "\n".join(lines[1:]).strip()
        return fallback_subject, text.strip()

    def draft_documents(self, context: DraftContext) -> tuple[str, str, str]:
        text = self._complete(DRAFT_SYSTEM_PROMPT, self._render_prompt(context), max_tokens=1600)
        subject, rest = self._split_subject(
            text, f"Contract {context['contract_id']} — first-pass review (draft for counsel review)"
        )
        if "---RESPONSE---" in rest:
            packet, response = rest.split("---RESPONSE---", 1)
            return subject, response.strip(), packet.strip()
        # A reply without the separator still yields a usable packet;
        # the response is the model text itself, marked as such — the
        # guardrails and reviewer judge what is actually there.
        return subject, rest, rest

    def review_clause_with_llm(self, clause: dict) -> dict | None:
        """Independent LLM playbook placement for one clause (LLM only).

        The model is deliberately NOT shown the rule verdict — the two
        paths place the clause independently and the graph cross-checks
        them (see ``crosscheck.py`` for the resolution policy). Returns
        ``{"position", "confidence", "rationale", "backend"}`` or
        ``None`` when the reply is unusable. Never raises for a
        malformed reply.
        """
        user = REVIEW_CLAUSE_USER_TEMPLATE.format(
            clause_id=clause.get("clause_id") or "",
            clause_ref=clause.get("clause_ref") or clause.get("clause_id") or "",
            heading=clause.get("heading") or "untitled clause",
            clause_type=clause.get("clause_type") or "other",
            signals=", ".join(
                f"{k}={v}" for k, v in (clause.get("signals") or {}).items()
                if v not in (None, "", False)
            ) or "none extracted",
            playbook=clause.get("playbook_text") or "no playbook entry retrieved",
            clause_text=clause.get("clause_text") or "",
        )
        text = self._complete(REVIEW_CLAUSE_SYSTEM_PROMPT, user, max_tokens=300)
        return _parse_verdict(text, self.name)

    def extract_clause_fields(self, unit) -> dict | None:
        """Extract the type and typed signals of one clause (LLM only).

        Returns ``{"clause_type", "confidence", "signals",
        "key_facts"}`` or ``None`` when the reply is unusable — the
        extraction node then keeps the rules extraction for that
        clause (see ``extractor.py``).
        """
        user = EXTRACT_USER_TEMPLATE.format(
            clause_ref=unit.ref or unit.clause_id,
            heading=unit.heading or "untitled clause",
            clause_text=unit.text,
        )
        text = self._complete(EXTRACT_SYSTEM_PROMPT, user, max_tokens=500)
        data = parse_json_object(text)
        if data is None:
            return None
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        signals = data.get("signals")
        key_facts = data.get("key_facts")
        return {
            "clause_type": str(data.get("clause_type") or "other"),
            "confidence": min(max(confidence, 0.0), 1.0),
            "signals": signals if isinstance(signals, dict) else {},
            "key_facts": [str(f)[:200] for f in key_facts] if isinstance(key_facts, list) else [],
        }

    def _assess_user(self, context: DraftContext) -> str:
        return ASSESS_USER_TEMPLATE.format(**self._facts_block(context))

    @staticmethod
    def _parse_assessment(text: str) -> dict | None:
        data = parse_json_object(text)
        if data is None:
            return None
        summary = str(data.get("summary", "")).strip()
        if not summary:
            return None
        factors = data.get("risk_factors") or []
        if not isinstance(factors, list):
            factors = [str(factors)]
        return {
            "summary": summary[:800],
            "risk_factors": [str(f)[:300] for f in factors],
        }

    def assess(self, context: DraftContext) -> dict | None:
        """LLM document assessment over the computed facts (LLM only).

        Returns ``{"summary": ..., "risk_factors": [...]}`` or ``None``
        when the reply is unusable — the pipeline then uses the
        deterministic template assessment built from the same evidence.
        """
        return self._parse_assessment(
            self._complete(ASSESS_SYSTEM_PROMPT, self._assess_user(context), max_tokens=500)
        )

    def _tool_loop(
        self,
        system: str,
        user: str,
        tool_specs: list[dict],
        dispatch,
        max_tool_calls: int,
    ) -> tuple[str, list[dict]]:
        """Run a bounded tool loop; return (final_text, tool_call_log).

        Implemented per provider (the tool wire formats differ). The
        log carries {name, summary} per executed call for the trace.
        """
        raise NotImplementedError

    def assess_with_tools(
        self,
        context: DraftContext,
        tool_specs: list[dict],
        dispatch,
        max_tool_calls: int,
    ) -> dict | None:
        """Agentic assessment (LLM only): a bounded tool loop, then compose.

        The model may call the tools in ``tools_agent.TOOL_SPECS``
        (playbook search, clause facts, counterparty history) through
        ``dispatch`` before composing. Same return contract as
        :meth:`assess`, plus ``tool_calls`` — the executed calls with
        one-line summaries, which the trace shows. ``None`` when the
        final reply is unusable; the pipeline falls back as it does for
        the single-call path. A tool that raises never fails the loop:
        the loop feeds the error back and the model composes without it.
        """
        text, tool_log = self._tool_loop(
            ASSESS_TOOLS_SYSTEM_PROMPT,
            self._assess_user(context),
            tool_specs,
            dispatch,
            max_tool_calls,
        )
        data = self._parse_assessment(text)
        if data is None:
            return None
        data["tool_calls"] = tool_log
        return data

    def propose_options(self, context: DraftContext) -> list[dict] | None:
        """LLM next-step proposals (LLM only) — names, not numbers.

        Returns a list of ``{"clause_id", "kind", "title",
        "description"}`` dicts in reply order, or ``None`` when the
        reply has no usable entry. Kind validation and ALL scoring
        happen in ``options.py``.
        """
        user = OPTIONS_USER_TEMPLATE.format(
            **self._facts_block(context),
            assessment=context.get("assessment_summary") or "none recorded",
        )
        data = parse_json_list(self._complete(OPTIONS_SYSTEM_PROMPT, user, max_tokens=500))
        if not data:
            return None
        proposals: list[dict] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("kind", "")).strip().lower()
            title = str(entry.get("title", "")).strip()
            description = str(entry.get("description", "")).strip()
            if kind and title and description:
                proposals.append(
                    {
                        "clause_id": str(entry.get("clause_id", "")).strip(),
                        "kind": kind,
                        "title": title[:120],
                        "description": description[:300],
                    }
                )
        return proposals or None

    def compose_information_request(self, context: DraftContext) -> str | None:
        """Compose an information request (LLM backends only).

        The missing-items list is computed by code (``clarify.py``);
        the model only words the message. Returns the message text, or
        ``None`` when the reply is empty — the caller then uses the
        template wording with the same items.
        """
        user = INFO_REQUEST_USER_TEMPLATE.format(
            contract_id=context["contract_id"],
            counterparty_name=context.get("counterparty_name") or "unknown counterparty",
            doc_type=context.get("doc_type") or "general",
            missing_items="\n".join(
                f"- {item}" for item in context.get("missing_items", [])
            ),
        )
        text = self._complete(INFO_REQUEST_SYSTEM_PROMPT, user, max_tokens=400)
        return text.strip() or None

    def verify_draft(self, context: dict) -> dict | None:
        """LLM self-verification critique (LLM backends only).

        ``context`` is the plain dict built by ``verify.py``: verified
        facts + the drafts. Returns ``{"grounded", "issues", "summary"}``
        or ``None`` when the reply is unusable — the caller then runs
        the deterministic checklist and records the degradation.
        """
        user = VERIFY_USER_TEMPLATE.format(
            **self._facts_block(context),
            playbook_ids=", ".join(context.get("playbook_ids", [])) or "none",
            subject=context.get("subject", ""),
            review_packet=context.get("review_packet", ""),
            counterparty_response=context.get("counterparty_response", ""),
        )
        data = parse_json_object(self._complete(VERIFY_SYSTEM_PROMPT, user, max_tokens=500))
        if data is None or "grounded" not in data:
            return None
        issues = data.get("issues") or []
        if not isinstance(issues, list):
            issues = [str(issues)]
        return {
            "grounded": bool(data["grounded"]),
            "issues": [str(issue)[:300] for issue in issues],
            "summary": str(data.get("summary", ""))[:300],
        }

    def review_draft(self, context: dict) -> dict | None:
        """Independent LLM review (LLM backends only) — the critic half
        of the generator/critic split, with its own persona prompt.

        ``context`` is the plain dict built by ``reviewer.py``: verified
        facts, the assessment, packet contents, and the drafts. Returns
        ``{"verdict", "findings", "model"}`` or ``None`` when the reply
        is unusable — the caller then runs the deterministic checklist
        review and records the degradation. The reviewer may run on a
        different model than the pipeline: ``REVIEWER_MODEL`` when set,
        else the run's own model.
        """
        user = REVIEWER_USER_TEMPLATE.format(
            **self._facts_block(context),
            playbook_ids=", ".join(context.get("playbook_ids", [])) or "none",
            assessment_summary=context.get("assessment_summary") or "none recorded",
            assessment_citations=", ".join(context.get("assessment_citations", [])) or "none",
            packet_table=(
                "present" if context.get("packet_table_present") else "MISSING"
            ),
            packet_citations=", ".join(context.get("packet_citations") or []) or "none",
            packet_fallbacks=(
                "present" if context.get("packet_fallbacks_present") else "MISSING"
            ),
            packet_option_count=len(context.get("packet_options") or []),
            packet_recommended=context.get("packet_recommended") or "none",
            subject=context.get("subject", ""),
            review_packet=context.get("review_packet", ""),
            counterparty_response=context.get("counterparty_response", ""),
        )
        model = env_str("REVIEWER_MODEL") or None
        text, _usage = self.complete_with_usage(
            REVIEWER_SYSTEM_PROMPT, user, max_tokens=500, model=model
        )
        data = parse_json_object(text)
        if data is None:
            return None
        verdict = str(data.get("verdict", "")).strip().lower()
        if verdict not in {"pass", "concerns", "block"}:
            return None
        findings = data.get("findings") or []
        if not isinstance(findings, list):
            findings = [str(findings)]
        return {
            "verdict": verdict,
            "findings": [str(finding)[:300] for finding in findings],
            "model": model or self._model,
        }


class OpenAIBackend(_BaseLLMBackend):
    name = "openai"

    def __init__(self) -> None:
        api_key = env_str("OPENAI_API_KEY")
        if not api_key:
            raise _missing_key_error("openai", "OPENAI_API_KEY")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise _missing_sdk_error("openai", "OpenAI") from exc
        client_kwargs: dict = {
            "api_key": api_key,
            "timeout": env_float("LLM_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
            # No silent SDK retries: a dead endpoint must fail in seconds,
            # and degradation is the pipeline's job (recorded fallbacks),
            # not the transport's. Retrying is a deployment choice made
            # in front of this service, not inside it.
            "max_retries": 0,
        }
        # Optional: point at Azure OpenAI, a LiteLLM gateway, or any hosted
        # OpenAI-compatible endpoint without code changes.
        base_url = env_str("OPENAI_BASE_URL")
        if base_url:
            client_kwargs["base_url"] = base_url
        self.base_url = base_url or "https://api.openai.com/v1"
        try:
            self._client = OpenAI(**client_kwargs)
        except Exception as exc:  # construction failures are translated too
            raise translate_construction_error(
                exc, backend=self.name, base_url=self.base_url
            ) from exc
        self._model = env_str("OPENAI_MODEL", "gpt-4o-mini")

    def _complete(self, system: str, user: str, max_tokens: int = 600) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except Exception as exc:
            raise translate_provider_error(
                exc, backend=self.name, base_url=getattr(self, "base_url", None)
            ) from exc
        usage = getattr(response, "usage", None)
        if usage is not None:
            self._record_usage(
                {
                    "input_tokens": getattr(usage, "prompt_tokens", 0),
                    "output_tokens": getattr(usage, "completion_tokens", 0),
                }
            )
        return response.choices[0].message.content or ""

    def _create_chat(self, kwargs: dict):
        """chat.completions.create, tolerating clients without tool support.

        Some OpenAI-compatible clients reject the ``tools`` parameter
        outright (TypeError); those get one retry without it and the
        loop then composes from the up-front facts alone.
        """
        try:
            return self._client.chat.completions.create(**kwargs)
        except TypeError:
            if "tools" not in kwargs:
                raise
            self._tools_unsupported = True
            kwargs = {k: v for k, v in kwargs.items() if k != "tools"}
            return self._client.chat.completions.create(**kwargs)

    def _tool_loop(self, system, user, tool_specs, dispatch, max_tool_calls):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": spec["name"],
                    "description": spec["description"],
                    "parameters": spec["parameters"],
                },
            }
            for spec in tool_specs
        ]
        messages: list = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        log: list[dict] = []
        calls = 0
        while True:
            kwargs: dict = {"model": self._model, "max_tokens": 800, "messages": messages}
            if calls < max_tool_calls and not getattr(self, "_tools_unsupported", False):
                kwargs["tools"] = tools
            try:
                response = self._create_chat(kwargs)
            except Exception as exc:
                raise translate_provider_error(
                    exc, backend=self.name, base_url=getattr(self, "base_url", None)
                ) from exc
            usage = getattr(response, "usage", None)
            if usage is not None:
                self._record_usage(
                    {
                        "input_tokens": getattr(usage, "prompt_tokens", 0),
                        "output_tokens": getattr(usage, "completion_tokens", 0),
                    }
                )
            message = response.choices[0].message
            tool_calls = list(getattr(message, "tool_calls", None) or [])
            if not tool_calls or calls >= max_tool_calls:
                return message.content or "", log
            messages.append(message)
            for tool_call in tool_calls:
                if calls >= max_tool_calls:
                    # Still answer the call (the API requires it), but
                    # do not execute it — the budget is spent.
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": "tool budget exhausted — compose the final answer from the facts you have",
                        }
                    )
                    continue
                calls += 1
                name = tool_call.function.name
                try:
                    args = json.loads(tool_call.function.arguments or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except (json.JSONDecodeError, ValueError):
                    args = {}
                try:
                    result_text, summary = dispatch(name, args)
                except Exception as exc:  # a broken tool degrades, never fails
                    result_text = f"tool error: {exc}"
                    summary = f"{name} -> error: {exc}"
                log.append({"name": name, "summary": summary})
                messages.append(
                    {"role": "tool", "tool_call_id": tool_call.id, "content": result_text}
                )


class AnthropicBackend(_BaseLLMBackend):
    name = "anthropic"

    def __init__(self) -> None:
        api_key = env_str("ANTHROPIC_API_KEY")
        if not api_key:
            raise _missing_key_error("anthropic", "ANTHROPIC_API_KEY")
        try:
            import anthropic
        except ImportError as exc:
            raise _missing_sdk_error("anthropic", "Anthropic") from exc
        client_kwargs: dict = {
            "api_key": api_key,
            "timeout": env_float("LLM_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
            "max_retries": 0,  # see OpenAIBackend — degradation is the pipeline's job
        }
        base_url = env_str("ANTHROPIC_BASE_URL")
        if base_url:
            client_kwargs["base_url"] = base_url
        self.base_url = base_url or "https://api.anthropic.com"
        try:
            self._client = anthropic.Anthropic(**client_kwargs)
        except Exception as exc:  # construction failures are translated too
            raise translate_construction_error(
                exc, backend=self.name, base_url=self.base_url
            ) from exc
        self._model = env_str("ANTHROPIC_MODEL", "claude-sonnet-4-5")

    def _complete(self, system: str, user: str, max_tokens: int = 600) -> str:
        try:
            message = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:
            raise translate_provider_error(
                exc, backend=self.name, base_url=getattr(self, "base_url", None)
            ) from exc
        usage = getattr(message, "usage", None)
        if usage is not None:
            self._record_usage(
                {
                    "input_tokens": getattr(usage, "input_tokens", 0),
                    "output_tokens": getattr(usage, "output_tokens", 0),
                }
            )
        return "".join(block.text for block in message.content if block.type == "text")

    def _create_message(self, kwargs: dict):
        """messages.create, tolerating clients without tool support
        (see the OpenAI backend's ``_create_chat``)."""
        try:
            return self._client.messages.create(**kwargs)
        except TypeError:
            if "tools" not in kwargs:
                raise
            self._tools_unsupported = True
            kwargs = {k: v for k, v in kwargs.items() if k != "tools"}
            return self._client.messages.create(**kwargs)

    def _tool_loop(self, system, user, tool_specs, dispatch, max_tool_calls):
        tools = [
            {
                "name": spec["name"],
                "description": spec["description"],
                "input_schema": spec["parameters"],
            }
            for spec in tool_specs
        ]
        messages: list = [{"role": "user", "content": user}]
        log: list[dict] = []
        calls = 0
        while True:
            kwargs: dict = {
                "model": self._model,
                "max_tokens": 800,
                "system": system,
                "messages": messages,
            }
            if calls < max_tool_calls and not getattr(self, "_tools_unsupported", False):
                kwargs["tools"] = tools
            try:
                message = self._create_message(kwargs)
            except Exception as exc:
                raise translate_provider_error(
                    exc, backend=self.name, base_url=getattr(self, "base_url", None)
                ) from exc
            usage = getattr(message, "usage", None)
            if usage is not None:
                self._record_usage(
                    {
                        "input_tokens": getattr(usage, "input_tokens", 0),
                        "output_tokens": getattr(usage, "output_tokens", 0),
                    }
                )
            tool_uses = [
                block for block in message.content if getattr(block, "type", None) == "tool_use"
            ]
            if not tool_uses or calls >= max_tool_calls:
                return (
                    "".join(
                        block.text
                        for block in message.content
                        if getattr(block, "type", None) == "text"
                    ),
                    log,
                )
            messages.append({"role": "assistant", "content": message.content})
            results = []
            for block in tool_uses:
                if calls >= max_tool_calls:
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": "tool budget exhausted — compose the final answer from the facts you have",
                        }
                    )
                    continue
                calls += 1
                name = block.name
                try:
                    result_text, summary = dispatch(name, dict(block.input or {}))
                except Exception as exc:  # a broken tool degrades, never fails
                    result_text = f"tool error: {exc}"
                    summary = f"{name} -> error: {exc}"
                log.append({"name": name, "summary": summary})
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": result_text}
                )
            messages.append({"role": "user", "content": results})


class OllamaBackend(OpenAIBackend):
    """Local models via Ollama — a preset over the OpenAI-compatible path.

    Ollama serves an OpenAI-compatible API on localhost, so this backend
    is the OpenAI client pointed at ``OLLAMA_BASE_URL`` (default
    ``http://localhost:11434/v1``) with a placeholder key — Ollama needs
    no real API key. The result is a fully local real-LLM mode: pull a
    model (``ollama pull llama3.1``), set ``MODEL_BACKEND=ollama``, done.
    Embeddings in semantic/hybrid retrieval also come from Ollama in
    this mode (see ``retriever.py``).
    """

    name = "ollama"

    def __init__(self) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise _missing_sdk_error("ollama", "OpenAI") from exc
        self.base_url = env_str("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        try:
            self._client = OpenAI(
                api_key="ollama",  # placeholder — Ollama ignores it
                base_url=self.base_url,
                timeout=env_float("LLM_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
                max_retries=0,  # see OpenAIBackend — degradation is the pipeline's job
            )
        except Exception as exc:  # construction failures are translated too
            raise translate_construction_error(
                exc, backend=self.name, base_url=self.base_url
            ) from exc
        self._model = env_str("OLLAMA_MODEL", "llama3.1")


def get_backend(name: str | None = None) -> ModelBackend:
    """Select a backend by name or the MODEL_BACKEND env var (default: mock).

    Loads the repo-root ``.env`` first (real environment variables win), so
    every surface — API, CLI, traced demo — selects the backend the same way.
    """
    load_dotenv()
    selected = (name or env_str("MODEL_BACKEND") or "mock").lower()
    if selected == "mock":
        return MockModelBackend()
    if selected == "openai":
        return OpenAIBackend()
    if selected == "anthropic":
        return AnthropicBackend()
    if selected == "ollama":
        return OllamaBackend()
    raise ValueError(
        f"Unknown MODEL_BACKEND: {selected!r} (expected mock | openai | anthropic | ollama)"
    )
