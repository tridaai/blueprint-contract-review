"""Service layer: run + counsel-decision bookkeeping.

Approving a draft records counsel's decision and marks the packet
final. By default it performs NO external action (no email, no
counterparty portal call, nothing sent). The one opt-in exception is
output routing: when ``ACTION_WEBHOOK_URL`` is configured, the
approved packet is POSTed to that endpoint — the customer's system
of choice — and the outcome is recorded on the result (see
``dispatch_approval_webhook``).

Records live in the record store (``store.py``): PostgreSQL in
production, so analyses and decisions survive restarts and are
shared across replicas; the in-memory store is the test double.
Contract documents and the drafted packet are archived through the
object-store port (``objectstore.py``) under
``contracts/<contract_id>/...`` keys.

The counsel gate is checkpointed (``checkpoints.py``, on unless
``CHECKPOINTS=off``): an analysis pauses its graph at the gate with
the state persisted under the contract id, and approve/reject resume
that thread with the decision. The split is deliberate — the store
is the record of decisions (and the only thing the decision flow
reads); the checkpointer holds graph state. A missing/finished
thread never blocks a decision.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .checkpoints import get_checkpointer
from .config import env_float, env_int, env_str, load_dotenv
from .graph import resume_decision, run_contract
from .model_backends import ModelBackend, get_backend
from .objectstore import get_object_store
from .ports import Checkpointer, EventSink, ObjectStore
from .retriever import Retriever, get_retriever
from .schemas import (
    EDIT_OUTCOMES,
    AgentResult,
    ContractInput,
    EditOutcome,
    EditOutcomeInput,
)
from .store import ApprovalRecord, counterparty_summary, default_store

__all__ = ["ApprovalRecord", "BatchItem", "ContractService", "Store"]


# Store protocol re-export for call sites that typed against the service.
from .ports import Store  # noqa: E402,F401


@dataclass
class BatchItem:
    """One outcome of a batch analysis: a result, or the error that
    stopped that one contract — never the whole batch."""

    contract_id: str
    result: AgentResult | None = None
    error: str | None = None


def dispatch_approval_webhook(record: ApprovalRecord) -> str | None:
    """POST the approved packet to ``ACTION_WEBHOOK_URL`` (off by default).

    This is the output-routing seam — the thin adapter between an
    approval and the customer's system of choice (their CLM, a work
    queue, an automation endpoint). When ``ACTION_WEBHOOK_SECRET`` is
    also set, the request is HMAC-signed (``X-Trida-Signature``).
    Returns ``None`` when no URL is
    configured (the default: no external action at all), ``"sent"``
    on a 2xx response, and ``"failed"`` on any error or non-2xx — a
    failed dispatch never undoes the approval; it is recorded on the
    result for follow-up. Short timeout on purpose: an approval must
    not hang on a downstream system.
    """
    load_dotenv()
    url = env_str("ACTION_WEBHOOK_URL")
    if not url:
        return None
    result = record.result
    payload = {
        "contract_id": result.contract_id,
        "status": result.status,
        "decided_by": result.decided_by,
        "decided_at": result.decided_at,
        "posture": result.facts.posture if result.facts else "",
        "clause_verdicts": [v.model_dump(mode="json") for v in result.clause_verdicts],
        "clause_diffs": [d.model_dump(mode="json") for d in result.clause_diffs],
        "proposed_edits": [e.model_dump(mode="json") for e in result.proposed_edits],
        "edit_outcomes": [o.model_dump(mode="json") for o in result.edit_outcomes],
        "edit_reflection": [r.model_dump(mode="json") for r in result.edit_reflection],
        "round_diff": (
            result.round_diff.model_dump(mode="json") if result.round_diff else None
        ),
        "round_chain": (
            result.round_chain.model_dump(mode="json") if result.round_chain else None
        ),
        "exhibit_checks": [c.model_dump(mode="json") for c in result.exhibit_checks],
        "obligations": [o.model_dump(mode="json") for o in result.obligations],
        "drafts": {
            "subject": result.drafts.subject if result.drafts else "",
            "counterparty_response": result.drafts.counterparty_response if result.drafts else "",
            "review_packet": result.drafts.review_packet if result.drafts else "",
        },
    }
    return _post_signed_webhook(url, payload)


def _post_signed_webhook(url: str, payload: dict) -> str:
    """POST ``payload`` as JSON to ``url``, HMAC-signed when
    ``ACTION_WEBHOOK_SECRET`` is set: the receiver verifies
    X-Trida-Signature against the raw body with the shared secret
    (HMAC-SHA256, "sha256=<hex>") before trusting it — an unsigned
    endpoint on the open internet would accept anyone's payload.
    Returns ``"sent"`` on a 2xx, ``"failed"`` on any error or
    non-2xx. Short timeout on purpose: a dispatch must not hang the
    operation that triggered it, and a failure is recorded, never
    raised."""
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    secret = env_str("ACTION_WEBHOOK_SECRET")
    if secret:
        signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Trida-Signature"] = f"sha256={signature}"
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method="POST",
    )
    timeout = env_float("ACTION_WEBHOOK_TIMEOUT_SECONDS", 5.0)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return "sent" if 200 <= response.status < 300 else "failed"
    except Exception:  # dispatch failure is recorded, never raised
        return "failed"


def dispatch_sla_breach_webhook(record: ApprovalRecord, item: dict, notified_at: str) -> str | None:
    """POST one queue SLA breach to ``ACTION_WEBHOOK_URL`` (off by
    default), on the approval channel's signing scheme.

    A breach is an operational alarm, not a packet: the payload
    carries the queue facts (contract, counterparty, severity tier,
    budget vs waited, owner) so the receiving system — a ticket
    queue, a chat channel, legal ops' board — can route it. Returns
    ``None`` when no URL is configured (the default: the breach is
    announced on the event stream only), ``"sent"`` / ``"failed"``
    otherwise; a failed dispatch never unmakes the breach report.
    """
    load_dotenv()
    url = env_str("ACTION_WEBHOOK_URL")
    if not url:
        return None
    payload = {
        "event": "sla_breach",
        "contract_id": item["contract_id"],
        "counterparty_name": item["counterparty_name"],
        "posture": item["posture"],
        "worst_position": item["worst_position"],
        "sla_budget_business_days": item["sla_budget_business_days"],
        "business_days_waiting": item["business_days_waiting"],
        "assignee": item["assignee"],
        "unassigned": not item["assignee"],
        "notified_at": notified_at,
    }
    return _post_signed_webhook(url, payload)


# ---------------------------------------------------------------------------
# Queue SLA: how long a contract may wait for counsel, by severity
# ---------------------------------------------------------------------------

# Budgets in BUSINESS days, keyed by the contract's worst clause
# position — the queue's severity tier. A contract whose worst
# position is "preferred" has nothing to decide urgently and carries
# no budget. Operators tune the budgets via the QUEUE_SLA_* env vars;
# the defaults are the blueprint's stated policy, not a promise made
# to any counterparty.
_SLA_DEFAULTS = {"walk_away": 2, "review_required": 5, "acceptable_fallback": 10}


def sla_budget_business_days(worst_position: str) -> int | None:
    """The SLA budget for a severity tier, or None when the tier
    carries no budget (``preferred`` — nothing flagged)."""
    if worst_position not in _SLA_DEFAULTS:
        return None
    env_name = f"QUEUE_SLA_{worst_position.upper()}_BUSINESS_DAYS"
    return env_int(env_name, _SLA_DEFAULTS[worst_position])


def business_days_between(start, end) -> int:
    """Business days elapsed from ``start`` to ``end``: the Mon–Fri
    days in (start, end] — the receipt day itself does not count, the
    current day does. Zero when ``end`` is not after ``start``."""
    if end <= start:
        return 0
    days = 0
    cursor = start
    from datetime import timedelta

    while cursor < end:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def _parse_received_date(raw: str):
    """The date a contract was received, from its ISO timestamp.
    None when intake carried nothing parseable — the queue then
    reports no SLA state rather than inventing an age."""
    if not raw:
        return None
    from datetime import date as _date
    from datetime import datetime as _datetime

    try:
        return _datetime.fromisoformat(str(raw)).date()
    except ValueError:
        try:
            return _date.fromisoformat(str(raw)[:10])
        except ValueError:
            return None


@dataclass
class ContractService:
    backend: ModelBackend | None = None
    retriever: Retriever | None = None
    store: Store | None = None
    # The gate checkpointer: a saver instance, None (= resolve from
    # the environment, CHECKPOINTS, default on), or False (= disabled,
    # whatever the environment says — the CLI batch path uses this
    # because it persists nothing).
    checkpointer: Checkpointer | bool | None = None
    object_store: ObjectStore | None = None
    _resolved_store: Store | None = field(default=None, repr=False)
    _resolved_checkpointer: Checkpointer | None = field(default=None, repr=False)
    _checkpointer_resolved: bool = field(default=False, repr=False)
    _resolved_object_store: ObjectStore | None = field(default=None, repr=False)

    def _get_store(self) -> Store:
        if self._resolved_store is None:
            self._resolved_store = self.store or default_store()
        return self._resolved_store

    def _get_checkpointer(self) -> Checkpointer | None:
        if not self._checkpointer_resolved:
            self._checkpointer_resolved = True
            if self.checkpointer is False:
                self._resolved_checkpointer = None
            elif self.checkpointer is not None:
                self._resolved_checkpointer = self.checkpointer  # type: ignore[assignment]
            else:
                self._resolved_checkpointer = get_checkpointer()
        return self._resolved_checkpointer

    def _get_object_store(self) -> ObjectStore:
        if self._resolved_object_store is None:
            self._resolved_object_store = self.object_store or get_object_store()
        return self._resolved_object_store

    def _archive_documents(self, model: ContractInput, result: AgentResult) -> None:
        """Archive the source documents and drafts to object storage.

        Best-effort: an archive failure never fails an analysis (the
        record of the review is the store); the keys are deterministic
        so a later run re-archives the same objects.
        """
        try:
            objects = self._get_object_store()
            for doc in model.documents:
                if doc.raw_text:
                    key = f"contracts/{model.contract_id}/{doc.document_id or doc.doc_type}.txt"
                    objects.put(key, doc.raw_text.encode("utf-8"))
            if model.contract_text:
                objects.put(
                    f"contracts/{model.contract_id}/agreement.txt",
                    model.contract_text.encode("utf-8"),
                )
            if result.drafts:
                objects.put(
                    f"contracts/{model.contract_id}/review-packet.txt",
                    result.drafts.review_packet.encode("utf-8"),
                )
                objects.put(
                    f"contracts/{model.contract_id}/counterparty-response.txt",
                    result.drafts.counterparty_response.encode("utf-8"),
                )
        except Exception:
            pass

    def _resume_thread(self, contract_id: str, decision: dict) -> None:
        """Complete the checkpointed graph thread with a decision.

        Best-effort by design: the store record the caller just saved
        is the decision of record. A thread that is missing or already
        finished (the analysis ran with checkpoints off, modes were
        mixed) has nothing to resume, and a resume failure never
        unmakes a recorded human decision.
        """
        checkpointer = self._get_checkpointer()
        if checkpointer is None:
            return
        try:
            resume_decision(contract_id, decision, checkpointer)
        except Exception:
            pass

    def analyze(
        self, contract: ContractInput | dict, event_sink: EventSink | None = None
    ) -> AgentResult:
        # Backend and retriever come from the environment (MODEL_BACKEND /
        # RETRIEVER, with the repo-root .env loaded) unless injected.
        # ``event_sink`` (optional) receives the run's structured events
        # (see events.py) — the API's streaming endpoint passes one.
        backend = self.backend or get_backend()
        if self.retriever is None:
            self.retriever = get_retriever()
        model = (
            contract
            if isinstance(contract, ContractInput)
            else ContractInput.model_validate(contract)
        )
        return self._analyze_model(model, backend, self.retriever, event_sink=event_sink)

    def _analyze_model(
        self,
        model: ContractInput,
        backend: ModelBackend,
        retriever: Retriever,
        event_sink: EventSink | None = None,
    ) -> AgentResult:
        # Memory: what the store already knows about this counterparty
        # becomes assessment evidence for the new analysis. The raw
        # entries ride along too — the assessment tool loop (provider
        # mode) reads counterparty history through them.
        priors = self._get_store().prior_contracts(
            exclude_contract_id=model.contract_id
        )
        history = self._history_summary(model, priors)
        # Feedback loop: what counsel said about earlier contracts
        # with this counterparty (with their reasons) joins the same
        # evidence — the next assessment shows the pattern of human
        # oversight.
        feedback = self._feedback_for(model)
        if feedback:
            history = {**(history or {}), "feedback": feedback}
        # Concession profile: how this counterparty has actually
        # moved across its stored negotiations (concessions.py) —
        # computed facts about their past rounds, offered to the
        # assessment as priors alongside the posture memory.
        concession_lines = self._concession_lines(model)
        if concession_lines:
            history = {**(history or {}), "concession_lines": concession_lines}
        result = run_contract(
            model,
            backend=backend,
            retriever=retriever,
            history=history,
            priors=priors,
            event_sink=event_sink,
            checkpointer=self._get_checkpointer(),
            thread_id=model.contract_id,
        )
        self._attach_round_diff(model, result)
        self._get_store().save(
            ApprovalRecord(result=result, contract=model.model_dump(mode="json"))
        )
        self._archive_documents(model, result)
        return result

    def _chain_rounds(
        self, model: ContractInput, result: AgentResult
    ) -> list[AgentResult]:
        """The negotiation chain this result belongs to, oldest →
        newest: follow the ``supersedes`` links through the store's
        records. A link that was never analysed here ends the walk —
        the chain is what the store actually holds, nothing invented.
        Cycles (bad intake naming a loop) terminate on the seen set."""
        chain = [result]
        seen = {model.contract_id}
        supersedes = model.supersedes
        while supersedes and supersedes not in seen:
            seen.add(supersedes)
            record = self._get_store().get(supersedes)
            if record is None:
                break
            chain.append(record.result)
            supersedes = (record.contract or {}).get("supersedes") or ""
        chain.reverse()
        return chain

    def _attach_round_diff(self, model: ContractInput, result: AgentResult) -> None:
        """Negotiation-round diffing: when intake names the round this
        draft supersedes and that round is in the store, align the two
        rounds' clauses and attach the verdict movement (see
        ``round_diff.py``). A superseded round that was never analysed
        here simply means no diff — the review stands on its own.

        With three or more rounds in the store the whole chain is
        walked as well (``build_round_chain``): per-clause movement
        history, cumulative movement, and re-trade detection — a
        clause that improved in an earlier round and worsens again
        later is flagged as re-traded, not merely "worsened"."""
        if not model.supersedes or model.supersedes == model.contract_id:
            return
        prior = self._get_store().get(model.supersedes)
        if prior is None:
            return
        from .round_diff import build_round_chain, build_round_diff
        from .schemas import TraceStep

        diff = build_round_diff(prior.result, result)
        result.round_diff = diff
        result.trace.append(
            TraceStep(
                name="round_diff",
                title=f"Negotiation movement vs {diff.previous_contract_id}",
                details=[
                    f"{diff.improved} improved · {diff.worsened} worsened · "
                    f"{diff.unchanged} unchanged · {diff.new_clauses} new · "
                    f"{diff.removed_clauses} removed",
                    *diff.summary,
                ],
            )
        )
        # Edit reflection: the edits counsel marked accepted/modified
        # on the superseded round, checked against THIS draft's text
        # (see edit_reflection.py). Computed whenever the prior round
        # carries outcomes — the chain walk below is separate.
        if prior.result.edit_outcomes:
            from .edit_reflection import build_edit_reflection

            result.edit_reflection = build_edit_reflection(prior.result, result)
            flagged = sum(
                1
                for entry in result.edit_reflection
                if entry.status == "agreed_not_reflected"
            )
            result.trace.append(
                TraceStep(
                    name="edit_reflection",
                    title=(
                        f"Edit reflection vs {prior.result.contract_id}: "
                        f"{len(result.edit_reflection)} agreed edit(s) checked · "
                        f"{flagged} agreed but not reflected"
                    ),
                    details=[
                        f"{entry.clause_type} [{entry.kind}]: {entry.status} — {entry.detail}"
                        for entry in result.edit_reflection
                    ],
                )
            )
        rounds = self._chain_rounds(model, result)
        re_traded_types: set[str] = set()
        if len(rounds) >= 3:
            chain = build_round_chain(rounds)
            result.round_chain = chain
            re_traded_types = set(chain.re_traded_clause_types)
            result.trace.append(
                TraceStep(
                    name="round_chain",
                    title=(
                        f"Negotiation chain: {chain.rounds} rounds · "
                        f"{chain.re_trades} re-trade(s)"
                    ),
                    details=list(chain.summary),
                )
            )
        # Reflection patterns: a clause type whose agreed edits were
        # flagged agreed-but-not-reflected in two consecutive
        # follow-ups (see reflection_patterns.py). The pattern is
        # computed over the chain's stored reflections — this round's
        # own reflection was attached just above.
        from .reflection_patterns import find_reflection_patterns

        patterns = find_reflection_patterns(rounds)
        if patterns:
            result.reflection_patterns = patterns
            result.trace.append(
                TraceStep(
                    name="reflection_pattern",
                    title=(
                        f"Reflection patterns: {len(patterns)} clause "
                        "type(s) agreed twice, reflected neither time"
                    ),
                    details=[pattern.detail for pattern in patterns],
                )
            )
        if len(rounds) >= 3 or patterns:
            # Tier refinement: with the full chain known, rebuild the
            # clause diffs so fallback-tier proposals use the true
            # round number, the chain's re-trade flags, and any
            # reflection-pattern escalations (the graph could only
            # assume "round 2" from the supersedes link).
            from .clause_diffs import build_clause_diffs
            from .proposed_edits import build_proposed_edits

            result.clause_diffs = build_clause_diffs(
                result.clauses,
                result.clause_verdicts,
                round_number=len(rounds),
                re_traded_types=re_traded_types,
                reflection_escalated_types={p.clause_type for p in patterns},
            )
            # The edit script maps the diffs' tier language — when
            # the tiers move with the chain, the edits move with them.
            result.proposed_edits = build_proposed_edits(result.clause_diffs)

    def analyze_batch(
        self,
        contracts: list[ContractInput | dict],
        concurrency: int = 1,
    ) -> list[BatchItem]:
        """Analyse many contracts, in input order, optionally concurrently.

        Each item runs the same pipeline as :meth:`analyze` and gets
        its own result object. Injected backend/retriever are shared
        (test doubles are stateless); otherwise every item resolves
        fresh ones from the environment, so a provider backend's usage
        counters and a retriever's per-run stats never race across
        threads. The store is shared deliberately — batch items see
        each other as memory, like a real review queue — and its
        writes are serialised by the store itself (Postgres: one
        connection per call; in-memory: a lock). A contract that
        fails (bad payload, provider down) lands in its item's
        ``error``; it never fails the batch.
        """

        def run_one(raw: ContractInput | dict) -> BatchItem:
            contract_id = (
                raw.get("contract_id", "unknown")
                if isinstance(raw, dict)
                else getattr(raw, "contract_id", "unknown")
            )
            try:
                model = (
                    raw
                    if isinstance(raw, ContractInput)
                    else ContractInput.model_validate(raw)
                )
                backend = self.backend or get_backend()
                retriever = self.retriever or get_retriever()
                result = self._analyze_model(model, backend, retriever)
                return BatchItem(contract_id=model.contract_id, result=result)
            except Exception as exc:  # one bad contract never fails the batch
                return BatchItem(
                    contract_id=str(contract_id),
                    error=f"{type(exc).__name__}: {exc}",
                )

        workers = max(1, int(concurrency))
        if workers == 1 or len(contracts) <= 1:
            return [run_one(item) for item in contracts]
        semaphore = threading.Semaphore(workers)

        def guarded(item: ContractInput | dict) -> BatchItem:
            with semaphore:
                return run_one(item)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(guarded, contracts))

    def counterparty_profile(self, counterparty: str) -> dict:
        """The concession profile for one counterparty, computed
        from every stored record (see ``concessions.py``): clause
        types conceded vs held across their negotiation chains,
        re-trade counts, rounds to clear walk-away positions, and
        counsel's decision tallies. An unknown counterparty yields a
        zero profile — the caller decides whether that is a 404."""
        from .concessions import build_concession_profile

        return build_concession_profile(
            counterparty, self._get_store().all_records()
        )

    def _concession_lines(self, contract: ContractInput) -> list[str]:
        """The counterparty's concession profile as evidence lines
        for a new analysis, excluding the contract being analysed.
        Empty when there is no multi-round history — a pattern needs
        at least one negotiation, not one contract."""
        if not contract.counterparty_name:
            return []
        from .concessions import build_concession_profile, concession_evidence_lines

        records = [
            record
            for record in self._get_store().all_records()
            if record.result.contract_id != contract.contract_id
        ]
        profile = build_concession_profile(contract.counterparty_name, records)
        return concession_evidence_lines(profile)

    def _feedback_for(self, contract: ContractInput) -> list[dict]:
        """Recent counsel feedback for this counterparty.

        Bounded to the last 3 matching decisions (the store returns
        newest first), reasons truncated — feedback is evidence, not
        a transcript.
        """
        entries = self._get_store().decision_feedback(
            exclude_contract_id=contract.contract_id
        )
        if not entries:
            return []
        matched: list[dict] = []
        for entry in entries:
            if contract.counterparty_name and entry["counterparty_name"] == contract.counterparty_name:
                matched.append(
                    {
                        "decision": entry["decision"],
                        "reason": entry["reason"][:160],
                        "match": "counterparty",
                    }
                )
            if len(matched) == 3:
                break
        return matched

    def _history_summary(
        self, contract: ContractInput, priors: list[dict] | None = None
    ) -> dict | None:
        """Summarise prior analysed contracts for this counterparty.

        Returns None when there is nothing to remember.
        """
        if priors is None:
            priors = self._get_store().prior_contracts(
                exclude_contract_id=contract.contract_id
            )
        if not priors:
            return None
        summary = counterparty_summary(priors, contract.counterparty_name)
        if not summary["counterparty_count"]:
            return None
        return summary

    def approve(
        self,
        contract_id: str,
        approver: str,
        reason: str = "",
        edit_outcomes: list[EditOutcomeInput] | None = None,
    ) -> AgentResult:
        store = self._get_store()
        record = store.get(contract_id)
        if record is None:
            raise KeyError(f"Unknown contract_id: {contract_id} (analyze it first)")
        if record.result.status != "awaiting_counsel":
            raise ValueError(
                f"Contract {contract_id} is not awaiting counsel "
                f"(status: {record.result.status})"
            )
        if record.result.validation is not None and not record.result.validation.passed:
            failed = "; ".join(
                f"{c.name}: {c.detail}"
                for c in record.result.validation.checks
                if not c.passed
            )
            raise ValueError(
                "Draft failed guardrail validation and cannot be approved: " + failed
            )
        # Per-edit outcomes settled together with the approval are
        # recorded BEFORE dispatch, so the routed packet carries what
        # counsel actually agreed, edit by edit.
        if edit_outcomes:
            self._apply_edit_outcomes(record, edit_outcomes, approver)
        record.approved = True
        record.approver = approver
        record.approve_reason = reason
        record.result.status = "approved"
        record.result.decision = "approved"
        record.result.decided_by = approver
        record.result.decision_reason = reason or None
        record.result.decided_at = datetime.now(timezone.utc).isoformat()
        # Output routing: by default there is NO external action. When
        # the operator configures ACTION_WEBHOOK_URL, the approved
        # packet is POSTed to that endpoint (the customer's system of
        # choice) and the outcome is recorded — a failed dispatch
        # leaves the approval standing and says so on the result.
        record.result.external_action_taken = False
        dispatch = dispatch_approval_webhook(record)
        if dispatch is not None:
            record.dispatch_status = dispatch
            record.result.dispatch_status = dispatch
            if dispatch == "sent":
                record.result.external_action_taken = True
        store.save(record)
        self._resume_thread(
            contract_id,
            {"decision": "approved", "actor": approver, "reason": reason},
        )
        if edit_outcomes:
            self._refresh_successor_reflection(contract_id)
        return record.result

    def reject(self, contract_id: str, reviewer: str, reason: str = "") -> AgentResult:
        store = self._get_store()
        record = store.get(contract_id)
        if record is None:
            raise KeyError(f"Unknown contract_id: {contract_id} (analyze it first)")
        if record.result.status != "awaiting_counsel":
            raise ValueError(
                f"Contract {contract_id} is not awaiting counsel "
                f"(status: {record.result.status})"
            )
        record.rejected_by = reviewer
        record.reject_reason = reason
        record.result.status = "rejected"
        record.result.decision = "rejected"
        record.result.decided_by = reviewer
        record.result.decision_reason = reason or None
        record.result.decided_at = datetime.now(timezone.utc).isoformat()
        record.result.external_action_taken = False
        store.save(record)
        self._resume_thread(
            contract_id,
            {"decision": "rejected", "actor": reviewer, "reason": reason},
        )
        return record.result

    # ------------------------------------------------------------------
    # Per-edit acceptance tracking (round 6)
    # ------------------------------------------------------------------

    def _apply_edit_outcomes(
        self,
        record: ApprovalRecord,
        outcomes: list[EditOutcomeInput],
        actor: str,
    ) -> None:
        """Upsert counsel's per-edit outcomes onto a record's result.

        Each outcome names an edit by its index in the stored
        result's ``proposed_edits`` — the snapshot fields (clause,
        type, kind) are copied from the edit itself so the outcome
        stays readable even if renders change. Re-marking an edit
        replaces its outcome. Validation failures name the edit,
        because a silent drop here would corrupt the negotiation's
        memory of what was agreed."""
        result = record.result
        by_index = {o.edit_index: o for o in result.edit_outcomes}
        for item in outcomes:
            if item.outcome not in EDIT_OUTCOMES:
                raise ValueError(
                    f"Edit outcome must be one of {EDIT_OUTCOMES}, "
                    f"got {item.outcome!r}"
                )
            if item.edit_index < 0 or item.edit_index >= len(result.proposed_edits):
                raise ValueError(
                    f"edit_index {item.edit_index} is out of range — "
                    f"contract {result.contract_id} has "
                    f"{len(result.proposed_edits)} proposed edit(s)"
                )
            edit = result.proposed_edits[item.edit_index]
            by_index[item.edit_index] = EditOutcome(
                edit_index=item.edit_index,
                clause_id=edit.clause_id,
                clause_type=edit.clause_type,
                kind=edit.kind,
                outcome=item.outcome,
                note=item.note or "",
                modified_text=item.modified_text or "",
                marked_by=actor,
                marked_at=datetime.now(timezone.utc).isoformat(),
            )
        result.edit_outcomes = [by_index[i] for i in sorted(by_index)]

    def record_edit_outcomes(
        self,
        contract_id: str,
        outcomes: list[EditOutcomeInput],
        actor: str,
    ) -> AgentResult:
        """Record counsel's per-edit outcomes on a stored record —
        ahead of, or together with, the packet-level gate decision.

        Outcomes are negotiation bookkeeping: they decide nothing,
        but the follow-up round's reflection check reads the
        accepted/modified ones. When a successor round is already in
        the store, its reflection is recomputed against the outcomes
        just recorded."""
        store = self._get_store()
        record = store.get(contract_id)
        if record is None:
            raise KeyError(f"Unknown contract_id: {contract_id} (analyze it first)")
        self._apply_edit_outcomes(record, outcomes, actor)
        store.save(record)
        self._refresh_successor_reflection(contract_id)
        return record.result

    def _refresh_successor_reflection(self, contract_id: str) -> None:
        """Recompute the reflection of any stored round that
        supersedes ``contract_id`` — outcomes are often marked after
        the follow-up draft has already been analysed, and its
        stored reflection must not go stale against them. The
        reflection patterns that read the chain's reflections (one
        more round down) are refreshed with it."""
        from .edit_reflection import build_edit_reflection

        store = self._get_store()
        prior = store.get(contract_id)
        if prior is None:
            return
        for record in store.all_records():
            if (record.contract or {}).get("supersedes") != contract_id:
                continue
            refreshed = build_edit_reflection(prior.result, record.result)
            changed = refreshed != record.result.edit_reflection
            if changed:
                record.result.edit_reflection = refreshed
            if self._refresh_reflection_patterns(record):
                changed = True
            if changed:
                store.save(record)
            # A pattern two rounds down reads THIS record's
            # reflection, so refresh the patterns (not the
            # reflections) of the rounds that supersede it.
            for nxt in store.all_records():
                if (nxt.contract or {}).get("supersedes") != record.result.contract_id:
                    continue
                if self._refresh_reflection_patterns(nxt):
                    store.save(nxt)

    def _stored_chain(self, record: ApprovalRecord) -> list[AgentResult]:
        """The stored negotiation chain ending at ``record``'s
        result, oldest → newest, walked through the store's
        ``supersedes`` links (the stored-record twin of
        ``_chain_rounds``)."""
        store = self._get_store()
        chain = [record.result]
        seen = {record.result.contract_id}
        supersedes = (record.contract or {}).get("supersedes") or ""
        while supersedes and supersedes not in seen:
            seen.add(supersedes)
            prior = store.get(supersedes)
            if prior is None:
                break
            chain.append(prior.result)
            supersedes = (prior.contract or {}).get("supersedes") or ""
        chain.reverse()
        return chain

    def _refresh_reflection_patterns(self, record: ApprovalRecord) -> bool:
        """Recompute one stored record's reflection patterns from
        its chain; when they change, re-derive its clause diffs so
        the tier escalation lands. Proposed edits are rebuilt only
        when counsel has marked no outcomes on the record yet —
        outcomes index into the edit list, and a settled negotiation
        is never renumbered under counsel's marks. Returns True when
        the record changed."""
        from .reflection_patterns import find_reflection_patterns

        chain = self._stored_chain(record)
        patterns = find_reflection_patterns(chain)
        if patterns == record.result.reflection_patterns:
            return False
        record.result.reflection_patterns = patterns
        from .clause_diffs import build_clause_diffs
        from .proposed_edits import build_proposed_edits

        chain_report = record.result.round_chain
        record.result.clause_diffs = build_clause_diffs(
            record.result.clauses,
            record.result.clause_verdicts,
            round_number=len(chain),
            re_traded_types=(
                set(chain_report.re_traded_clause_types) if chain_report else set()
            ),
            reflection_escalated_types={p.clause_type for p in patterns},
        )
        if not record.result.edit_outcomes:
            record.result.proposed_edits = build_proposed_edits(
                record.result.clause_diffs
            )
        return True

    def get(self, contract_id: str) -> AgentResult | None:
        record = self._get_store().get(contract_id)
        return record.result if record else None

    def negotiation_brief(self, contract_id: str) -> dict:
        """The negotiation brief for a contract's chain: one
        markdown document assembled in code from the stored records
        (chain movement and re-trades, open positions by tier, the
        proposed edits, exhibit findings, obligations — see
        ``brief.py``), exported through the object-store port and
        returned with its key. Counsel downloads it and takes it
        into the negotiation; the agent sends it nowhere.

        Exports form a LINEAGE, keyed by the chain's root contract
        in ``contracts/<root>/negotiation-brief-index.json``: every
        export is retained under a versioned key
        (``contracts/<id>/negotiation-brief-v<n>.md``) alongside the
        canonical ``contracts/<id>/negotiation-brief.md``, and its
        source digest (``brief.brief_source_digest``) is stored in
        the index. A new export diffs its digest against the
        previous export's — whichever round that export covered —
        and the brief leads with a "Since the last brief" section:
        the new round, decisions and outcomes recorded, clauses
        moved, edits proposed anew, obligations opened or closed.
        """
        store = self._get_store()
        record = store.get(contract_id)
        if record is None:
            raise KeyError(f"Unknown contract_id: {contract_id} (analyze it first)")
        from .brief import brief_delta_lines, brief_source_digest, build_negotiation_brief

        if record.contract:
            model = ContractInput.model_validate(record.contract)
            chain = self._chain_rounds(model, record.result)
        else:
            chain = [record.result]
        objects = self._get_object_store()
        root_id = chain[0].contract_id
        index_key = f"contracts/{root_id}/negotiation-brief-index.json"
        exports: list[dict] = []
        raw_index = objects.get(index_key)
        if raw_index:
            try:
                loaded = json.loads(raw_index.decode("utf-8"))
                if isinstance(loaded, list):
                    exports = loaded
            except (ValueError, UnicodeDecodeError):
                exports = []  # a corrupt index starts a new lineage, never blocks a brief
        previous = exports[-1] if exports else None
        digest = brief_source_digest(chain, record.result)
        delta = (
            brief_delta_lines(previous["digest"], digest)
            if previous is not None
            else []
        )
        markdown = build_negotiation_brief(
            chain,
            record.result,
            delta_lines=delta,
            first_export=previous is None,
        )
        seq = len(exports) + 1
        versioned_key = f"contracts/{contract_id}/negotiation-brief-v{seq}.md"
        objects.put(versioned_key, markdown.encode("utf-8"), content_type="text/markdown")
        key = f"contracts/{contract_id}/negotiation-brief.md"
        objects.put(key, markdown.encode("utf-8"), content_type="text/markdown")
        exports.append(
            {
                "seq": seq,
                "contract_id": contract_id,
                "key": versioned_key,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "digest": digest,
            }
        )
        objects.put(
            index_key,
            json.dumps(exports, indent=2).encode("utf-8"),
            content_type="application/json",
        )
        return {
            "contract_id": contract_id,
            "key": key,
            "markdown": markdown,
            "export_seq": seq,
            "versioned_key": versioned_key,
            "previous_export": (
                {"contract_id": previous["contract_id"], "key": previous["key"]}
                if previous is not None
                else None
            ),
            "delta_lines": delta,
        }

    def queue(self, today=None) -> list[dict]:
        """The counsel worklist: every contract still awaiting a
        decision, worked in the order counsel should take them —
        SLA breaches first (a contract that has waited past its
        budget outranks a fresher one, whatever its posture), then
        posture severity, worst clause position, and age. Each item
        carries its queue ownership (``assignee``, recorded by an
        operator — see :meth:`assign`) and its SLA state: the budget
        for its severity tier, the business days it has waited, and
        whether the budget is blown. Computed here, once, from the
        stored records rather than re-derived per surface. ``today``
        is a parameter so SLA behaviour is testable against a frozen
        clock."""
        from datetime import date as _date

        from .schemas import POSITION_SEVERITY

        today = today or _date.today()
        posture_rank = {"escalation_required": 0, "negotiable": 1, "aligned": 2}
        items: list[dict] = []
        for record in self._get_store().all_records():
            result = record.result
            if result.status != "awaiting_counsel":
                continue
            worst = result.facts.worst_position if result.facts else ""
            budget = sla_budget_business_days(worst)
            received = _parse_received_date(
                (record.contract or {}).get("received_at", "")
            )
            waiting = (
                business_days_between(received, today)
                if received is not None
                else None
            )
            items.append(
                {
                    "contract_id": result.contract_id,
                    "counterparty_name": result.counterparty_name,
                    "doc_type": result.doc_type,
                    "posture": result.facts.posture if result.facts else "",
                    "worst_position": worst,
                    "received_at": (record.contract or {}).get("received_at", ""),
                    "age_days": result.facts.contract_age_days if result.facts else 0,
                    "flagged_clauses": sum(
                        1 for v in result.clause_verdicts if v.position != "preferred"
                    ),
                    "needs_information": result.needs_information,
                    "reviewer_blocked": result.reviewer_blocked,
                    "autonomy_eligible": bool(
                        result.autonomy and result.autonomy.eligible_for_auto_triage
                    ),
                    "assignee": record.assignee or "",
                    "assigned_at": record.assigned_at or "",
                    "sla_budget_business_days": budget,
                    "business_days_waiting": waiting,
                    "sla_breached": bool(
                        budget is not None
                        and waiting is not None
                        and waiting > budget
                    ),
                }
            )
        items.sort(
            key=lambda item: (
                not item["sla_breached"],
                posture_rank.get(item["posture"], 1),
                -POSITION_SEVERITY.get(item["worst_position"], 0),
                item["received_at"] or "9999",
                item["contract_id"],
            )
        )
        return items

    def assign(self, contract_id: str, assignee: str, actor: str) -> dict:
        """Record who on counsel's team holds an awaiting contract.

        Ownership is bookkeeping for the queue: it is stored on the
        record (with who assigned it and when), shown on every queue
        surface, and never decides or routes anything by itself.
        Returns the contract's queue item after the assignment."""
        store = self._get_store()
        record = store.get(contract_id)
        if record is None:
            raise KeyError(f"Unknown contract_id: {contract_id} (analyze it first)")
        if record.result.status != "awaiting_counsel":
            raise ValueError(
                f"Contract {contract_id} is not awaiting counsel "
                f"(status: {record.result.status}) — decided contracts "
                "have no queue owner"
            )
        record.assignee = assignee
        record.assigned_by = actor
        record.assigned_at = datetime.now(timezone.utc).isoformat()
        store.save(record)
        for item in self.queue():
            if item["contract_id"] == contract_id:
                return item
        raise AssertionError("assigned contract missing from its own queue")

    def obligations_due(self, days: int = 90, today=None) -> dict:
        """Obligation deadlines across every stored contract: the
        register entries carrying a date that fall due within
        ``days`` (overdue included), soonest first. This is the
        operational surface of the obligations register — a computed
        notice deadline counsel can act on, not just read."""
        from datetime import date as _date

        from .obligations import due_obligations

        today = today or _date.today()
        rows: list[dict] = []
        for record in self._get_store().all_records():
            result = record.result
            for row in due_obligations(result.obligations, today, days):
                rows.append(
                    {
                        "contract_id": result.contract_id,
                        "counterparty_name": result.counterparty_name,
                        "status": result.status,
                        **row,
                    }
                )
        rows.sort(key=lambda row: (row["date"], row["contract_id"]))
        return {
            "as_of": today.isoformat(),
            "window_days": days,
            "count": len(rows),
            "obligations": rows,
        }

    def dispatch_obligation_sweep(
        self, days: int = 90, today=None, event_sink: EventSink | None = None
    ) -> dict:
        """The deadline dispatch sweep: walk every stored contract,
        find the obligations due within ``days`` of ``today``, emit
        one ``obligation_due`` event per deadline to ``event_sink``,
        and return the dispatch report.

        This is the scheduled counterpart of the per-run deadline
        events the pipeline emits at analysis time: analysis computes
        the deadlines; the sweep is what a nightly job (or counsel,
        from the console) runs so a deadline computed weeks ago still
        surfaces on the day it matters. **Report-only by design**: the
        sweep emits events to the configured sink and returns a
        report — it sends nothing to any counterparty or counsel
        address itself. Delivery (email, ticket, queue) is the
        consumer's job, behind the same event port as everything
        else. ``today`` is a parameter so the sweep is testable
        against a frozen clock."""
        from datetime import date as _date

        from .events import NullSink, RunEvent

        today = today or _date.today()
        due = self.obligations_due(days=days, today=today)
        sink = event_sink if event_sink is not None else NullSink()
        for row in due["obligations"]:
            sink.emit(
                RunEvent(
                    type="obligation_due",
                    contract_id=row["contract_id"],
                    detail={
                        "via": "deadline_sweep",
                        "kind": row["kind"],
                        "date": row["date"],
                        "days_until": row["days_until"],
                        "overdue": row["overdue"],
                        "computed": row["computed"],
                        "description": row["description"],
                        "source_clause_id": row["source_clause_id"],
                        "counterparty_name": row["counterparty_name"],
                        "contract_status": row["status"],
                    },
                )
            )
        swept = len(self._get_store().all_records())
        return {
            "as_of": today.isoformat(),
            "window_days": days,
            "contracts_swept": swept,
            "due_count": due["count"],
            "events_emitted": due["count"],
            "report_only": True,
            "note": (
                "Report-only sweep: one obligation_due event per due "
                "deadline was emitted to the event sink and this report "
                "was produced. Nothing was sent to any counterparty or "
                "counsel address by this operation."
            ),
            "obligations": due["obligations"],
        }

    def dispatch_sla_breach_sweep(
        self, today=None, event_sink: EventSink | None = None
    ) -> dict:
        """The SLA breach sweep: walk the counsel queue, and for
        every contract whose wait has crossed its SLA budget, emit
        one ``sla_breach`` event (and, when ``ACTION_WEBHOOK_URL``
        is configured, a signed webhook on the approval channel's
        scheme) — the FIRST time that contract is observed in
        breach. The record carries the notification stamp
        (``sla_breach_notified_at``), so a breach is announced once
        per contract however often the sweep runs; a contract that
        was decided in the meantime has left the queue and is never
        announced late.

        This is the operational counterpart of the queue's breach
        flag (round 5): the queue SHOWS breaches to whoever opens
        it; the sweep ANNOUNCES them to the event stream and the
        webhook consumer — an unassigned breach is the loudest
        event this system emits, and its detail says ``unassigned``
        outright. ``today`` is a parameter so breach behaviour is
        testable against a frozen clock."""
        from datetime import date as _date

        from .events import NullSink, RunEvent

        today = today or _date.today()
        sink = event_sink if event_sink is not None else NullSink()
        queue_items = self.queue(today=today)
        breached = [item for item in queue_items if item["sla_breached"]]
        newly: list[dict] = []
        already = 0
        webhooks = {"configured": bool(env_str("ACTION_WEBHOOK_URL")), "sent": 0, "failed": 0}
        store = self._get_store()
        for item in breached:
            record = store.get(item["contract_id"])
            if record is None:
                continue
            if record.sla_breach_notified_at:
                already += 1
                continue
            notified_at = datetime.now(timezone.utc).isoformat()
            sink.emit(
                RunEvent(
                    type="sla_breach",
                    contract_id=item["contract_id"],
                    detail={
                        "via": "sla_sweep",
                        "counterparty_name": item["counterparty_name"],
                        "posture": item["posture"],
                        "worst_position": item["worst_position"],
                        "sla_budget_business_days": item["sla_budget_business_days"],
                        "business_days_waiting": item["business_days_waiting"],
                        "assignee": item["assignee"],
                        "unassigned": not item["assignee"],
                        "notified_at": notified_at,
                    },
                )
            )
            dispatch = dispatch_sla_breach_webhook(record, item, notified_at)
            if dispatch is not None:
                webhooks[dispatch] += 1
            record.sla_breach_notified_at = notified_at
            store.save(record)
            newly.append(
                {
                    "contract_id": item["contract_id"],
                    "counterparty_name": item["counterparty_name"],
                    "worst_position": item["worst_position"],
                    "sla_budget_business_days": item["sla_budget_business_days"],
                    "business_days_waiting": item["business_days_waiting"],
                    "assignee": item["assignee"],
                    "unassigned": not item["assignee"],
                    "webhook": dispatch,
                    "notified_at": notified_at,
                }
            )
        return {
            "as_of": today.isoformat(),
            "awaiting": len(queue_items),
            "breaches_found": len(breached),
            "newly_reported": len(newly),
            "already_reported": already,
            "events_emitted": len(newly),
            "webhooks": webhooks,
            "breaches": newly,
            "note": (
                "One sla_breach event per contract, the first time "
                "the sweep observes it past its budget; the record "
                "carries the stamp, so re-running the sweep does not "
                "re-announce it. The webhook fires only when "
                "ACTION_WEBHOOK_URL is configured (signed when "
                "ACTION_WEBHOOK_SECRET is set) — otherwise the "
                "breach is announced on the event stream alone."
            ),
        }

    def audit_rows(self) -> list[dict]:
        """The audit trail: one flat row per analysed contract, with
        counsel's decision (who, when, why) and the safety outcomes
        (guardrails, repair, reviewer) that preceded it. This is the
        record a compliance review asks for — exportable as JSON or
        CSV from the API (``/audit/export``)."""
        rows: list[dict] = []
        for record in self._get_store().all_records():
            result = record.result
            rows.append(
                {
                    "contract_id": result.contract_id,
                    "counterparty_name": result.counterparty_name,
                    "doc_type": result.doc_type,
                    "posture": result.facts.posture if result.facts else "",
                    "worst_position": result.facts.worst_position if result.facts else "",
                    "status": result.status,
                    "decision": result.decision or "",
                    "decided_by": result.decided_by or "",
                    "decision_reason": result.decision_reason or "",
                    "decided_at": result.decided_at or "",
                    "validation_passed": (
                        result.validation.passed if result.validation else None
                    ),
                    "repair_attempted": result.repair_attempted,
                    "repaired": result.repaired,
                    "reviewer_blocked": result.reviewer_blocked,
                    "dispatch_status": result.dispatch_status or "",
                    "external_action_taken": result.external_action_taken,
                }
            )
        return rows

    def playbook_coverage(self, results: list[AgentResult] | None = None) -> dict:
        """Playbook coverage analytics over the stored runs.

        Three views legal ops needs to grow the playbook where it is
        thin: which playbook entries actually fire on verdicts (and
        which never have), which clause types most often come back
        flagged with NO fallback language to offer (a playbook gap,
        not a contract gap), and the position distribution per
        counterparty (who keeps sending walk-away paper).
        """
        from .playbook_data import PLAYBOOK

        if results is None:
            results = [record.result for record in self._get_store().all_records()]
        firing: Counter = Counter()
        flagged: Counter = Counter()
        without_fallback: Counter = Counter()
        by_counterparty: dict[str, Counter] = {}
        for result in results:
            positions = by_counterparty.setdefault(result.counterparty_name or "", Counter())
            for verdict in result.clause_verdicts:
                firing.update(verdict.playbook_refs)
                positions[verdict.position] += 1
                if verdict.position != "preferred":
                    flagged[verdict.clause_type] += 1
                    if not verdict.fallback_language:
                        without_fallback[verdict.clause_type] += 1
        return {
            "playbook_firing": dict(firing.most_common()),
            "playbook_never_fired": [
                entry["id"] for entry in PLAYBOOK if not firing[entry["id"]]
            ],
            "clause_types_without_fallback": [
                {
                    "clause_type": clause_type,
                    "flagged": flagged[clause_type],
                    "without_fallback": count,
                }
                for clause_type, count in without_fallback.most_common()
            ],
            "positions_by_counterparty": {
                counterparty: dict(counts)
                for counterparty, counts in sorted(by_counterparty.items())
                if counterparty
            },
        }

    def metrics(self) -> dict:
        """Operational summary over the stored runs (``/metrics``).

        Counts and telemetry aggregates only — no draft text, no
        counterparty detail beyond posture/status tallies. Token and
        cost sums cover provider runs; offline runs honestly report
        no tokens (None), so they are counted separately instead of
        diluting the averages with zeros.
        """
        records = self._get_store().all_records()
        results = [record.result for record in records]
        queue_items = self.queue()
        telemetries = [r.telemetry for r in results if r.telemetry is not None]
        provider_runs = [t for t in telemetries if t.input_tokens is not None]
        latencies = [t.latency_seconds for t in telemetries]
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "runs_total": len(results),
            "by_status": dict(Counter(r.status for r in results)),
            "by_posture": dict(
                Counter(r.facts.posture for r in results if r.facts)
            ),
            "decisions": {
                "approved": sum(1 for r in results if r.decision == "approved"),
                "rejected": sum(1 for r in results if r.decision == "rejected"),
                "awaiting": sum(1 for r in results if r.decision is None),
            },
            "guardrail_failures": sum(
                1 for r in results if r.validation is not None and not r.validation.passed
            ),
            "repairs": {
                "attempted": sum(1 for r in results if r.repair_attempted),
                "repaired": sum(1 for r in results if r.repaired),
            },
            "reviewer_blocks": sum(1 for r in results if r.reviewer_blocked),
            "queue_sla": {
                "awaiting": len(queue_items),
                "sla_breached": sum(1 for i in queue_items if i["sla_breached"]),
                "unassigned": sum(1 for i in queue_items if not i["assignee"]),
                "unassigned_breached": sum(
                    1
                    for i in queue_items
                    if i["sla_breached"] and not i["assignee"]
                ),
                "breach_events_emitted": sum(
                    1 for r in records if r.sla_breach_notified_at
                ),
                "budgets_business_days": {
                    tier: sla_budget_business_days(tier) for tier in _SLA_DEFAULTS
                },
            },
            "playbook_coverage": self.playbook_coverage(results),
            "telemetry": {
                "runs_with_telemetry": len(telemetries),
                "provider_runs": len(provider_runs),
                "model_calls": sum(t.model_calls for t in telemetries),
                "input_tokens": sum(t.input_tokens or 0 for t in provider_runs),
                "output_tokens": sum(t.output_tokens or 0 for t in provider_runs),
                "estimated_cost_usd": round(
                    sum(t.estimated_cost_usd or 0.0 for t in provider_runs), 6
                ),
                "mean_latency_seconds": (
                    round(sum(latencies) / len(latencies), 3) if latencies else None
                ),
            },
        }
