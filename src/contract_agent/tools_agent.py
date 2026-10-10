"""Agent tools for the assessment step (provider mode).

In provider mode the assessment is *agentic*: instead of one composed
call over pre-assembled facts, the model runs a bounded tool loop — it
may pull more playbook context, the computed clause facts, or the
counterparty's history from prior reviews before it composes the risk
summary. The loop itself lives in the backends (``model_backends.py``,
per-provider tool formats); this module owns what the tools ARE: their
schemas (one neutral definition both provider formats render from)
and their dispatch.

Every tool here is a read over data the pipeline already holds — the
playbook corpus behind the retriever interface, the store's history
entries, and the computed facts. No tool sends, files, mutates, or
decides anything; the counsel gate is untouched.

The bound: ``ASSESSMENT_MAX_TOOL_CALLS`` (default 4, hard cap 6) caps
how many tool calls one assessment may make. When the budget runs out
the model is told to compose with what it has.
"""

from __future__ import annotations

from .config import env_int, load_dotenv
from .schemas import ContractInput
from .store import format_position_counts

DEFAULT_MAX_TOOL_CALLS = 4
HARD_MAX_TOOL_CALLS = 6

# Neutral tool definitions: name, description, JSON-schema parameters.
# The Anthropic backend renders these as {name, description,
# input_schema}; the OpenAI backend as {type: "function", function:
# {name, description, parameters}}. One definition, two wire formats.
TOOL_SPECS: list[dict] = [
    {
        "name": "search_playbook",
        "description": (
            "Search the company playbook corpus with your own query "
            "wording and get the top matching entries with their IDs. "
            "Use when the entries already provided look incomplete."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query in playbook wording."}
            },
            "required": ["query"],
        },
    },
    {
        "name": "clause_facts",
        "description": (
            "The computed facts for THIS contract: per-clause playbook "
            "verdicts, position counts, document posture, missing "
            "standard clauses, and extraction cross-check "
            "discrepancies. Always prefer these over assumptions."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "counterparty_history",
        "description": (
            "Prior contracts for a counterparty from the stored "
            "history: count, the postures they received, and counsel's "
            "recorded decisions — a counterparty that repeatedly sends "
            "walk-away clauses is itself a signal."
        ),
        "parameters": {
            "type": "object",
            "properties": {"counterparty_name": {"type": "string"}},
            "required": ["counterparty_name"],
        },
    },
]


def assessment_max_tool_calls() -> int:
    """The tool-call budget for one assessment (env, read at run time)."""
    load_dotenv()
    return min(max(env_int("ASSESSMENT_MAX_TOOL_CALLS", DEFAULT_MAX_TOOL_CALLS), 0), HARD_MAX_TOOL_CALLS)


class AssessmentToolBox:
    """Dispatches the assessment tools over the data one run holds.

    ``dispatch(name, args)`` returns ``(result_text, summary)``: the
    text fed back to the model, and a one-line summary for the trace.
    Unknown tools and bad arguments raise — the backend loop converts a
    raise into an error tool-result and the model composes without it,
    so a broken tool degrades the assessment, never the run.
    """

    def __init__(
        self,
        *,
        contract: ContractInput,
        facts: dict,
        discrepancies: list[str] | None = None,
        retriever=None,
        priors: list[dict] | None = None,
    ) -> None:
        self._contract = contract
        self._facts = facts
        self._discrepancies = discrepancies or []
        self._retriever = retriever
        self._priors = priors or []

    def dispatch(self, name: str, args: dict) -> tuple[str, str]:
        handler = {
            "search_playbook": self._search_playbook,
            "clause_facts": self._clause_facts,
            "counterparty_history": self._counterparty_history,
        }.get(name)
        if handler is None:
            raise ValueError(f"unknown tool: {name}")
        return handler(args or {})

    # -- the tools ------------------------------------------------------

    def _search_playbook(self, args: dict) -> tuple[str, str]:
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("search_playbook needs a non-empty 'query'")
        if self._retriever is None:
            raise ValueError("no retriever is configured for this run")
        results = self._retriever.retrieve(query, top_k=3)
        if not results:
            return "No playbook entries matched that query.", f"search_playbook({query!r}) -> 0 entries"
        text = "\n".join(f"[{r.policy_id}] {r.title}: {r.text}" for r in results)
        ids = ", ".join(r.policy_id for r in results)
        return text, f"search_playbook({query!r}) -> {len(results)} entries: {ids}"

    def _clause_facts(self, args: dict) -> tuple[str, str]:
        f = self._facts
        lines = [
            f"document posture: {f.get('posture')} (worst position: {f.get('worst_position')})",
            f"positions: {format_position_counts(f.get('positions_count') or {})}",
            f"clauses reviewed: {f.get('clause_count')}",
            f"missing standard clauses: {', '.join(f.get('missing_standard_clauses') or []) or 'none'}",
            f"critical absences: {', '.join(f.get('critical_absences') or []) or 'none'}",
        ]
        for row in f.get("clause_rows") or []:
            lines.append(
                f"clause {row.get('clause_id')} [{row.get('clause_type')}]: "
                f"{row.get('position')} (playbook: {', '.join(row.get('refs') or []) or 'none'})"
            )
        lines += [f"extraction discrepancy: {d}" for d in self._discrepancies]
        summary = (
            f"clause_facts -> posture={f.get('posture')}, "
            f"positions={format_position_counts(f.get('positions_count') or {})}"
        )
        return "\n".join(lines), summary

    def _counterparty_history(self, args: dict) -> tuple[str, str]:
        name = str(args.get("counterparty_name") or self._contract.counterparty_name)
        hits = [e for e in self._priors if name and e.get("counterparty_name") == name]
        if not hits:
            return (
                f"No prior contracts for counterparty {name!r} in the stored history.",
                f"counterparty_history({name!r}) -> no prior contracts",
            )
        postures = ", ".join(e.get("posture", "?") for e in hits[:5])
        decisions = ", ".join(
            e.get("decision") or "undecided" for e in hits[:5]
        )
        return (
            f"{len(hits)} prior contract(s) for counterparty {name!r} in the "
            f"stored history (most recent postures: {postures}; counsel "
            f"decisions: {decisions}).",
            f"counterparty_history({name!r}) -> {len(hits)} prior contract(s): {postures}",
        )
