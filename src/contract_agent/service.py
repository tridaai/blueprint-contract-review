"""Service layer: run + counsel-decision bookkeeping.

Approving a draft records counsel's decision and marks the packet
final. By default it performs NO external action (no email, no
counterparty portal call, nothing sent). The one opt-in exception is
output routing: when ``ACTION_WEBHOOK_URL`` is configured, the
approved packet is POSTed to that endpoint — the customer's system
of choice — and the outcome is recorded on the result (see
``dispatch_approval_webhook``). The same signed seam carries two
operational event families: queue SLA breaches (see
``dispatch_sla_breach_webhook``) and cross-round drift findings
(see ``dispatch_drift_finding_webhook``). Every dispatch the seam
attempts is recorded in the webhook outbox (``webhook_outbox.py``)
with its exact payload, and a failed delivery is repaired there
by a recorded re-send (see ``resend_webhook``) — never by
regenerating the event.

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
from .calibration_store import CalibrationStore, default_calibration_store
from .proposal_store import PlaybookProposalStore, default_proposal_store
from .webhook_outbox import OutboxStore, default_outbox_store, record_dispatch
from .retriever import Retriever, get_retriever
from .schemas import (
    EDIT_OUTCOMES,
    AgentResult,
    ContractInput,
    DriftDispatch,
    EditOutcome,
    EditOutcomeInput,
    PatternTermSuspension,
    PlaybookProposal,
    SuspensionReconfirmation,
    WebhookAttempt,
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


def build_approval_payload(record: ApprovalRecord) -> dict:
    """The approved packet as a webhook payload: the decision,
    the verdicts and diffs it rests on, the per-edit outcomes
    counsel recorded, and the drafts. Built from the record
    alone, so the outbox can store the exact payload posted and
    a re-send never has to regenerate it."""
    result = record.result
    return {
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
    return _post_signed_webhook(url, build_approval_payload(record))


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
    return _post_signed_webhook(
        url, build_sla_breach_payload(item, notified_at)
    )


def build_sla_breach_payload(item: dict, notified_at: str) -> dict:
    """One queue SLA breach as a webhook payload: the queue facts
    a receiving system routes on. Built from the queue item alone,
    so the outbox stores the exact payload posted."""
    return {
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


def dispatch_drift_finding_webhook(
    result: AgentResult, check, prior_contract_id: str
) -> str | None:
    """POST one cross-round drift finding to ``ACTION_WEBHOOK_URL``
    (off by default), on the approval channel's signing scheme.

    The approval webhook reports counsel's decisions; the drift
    findings are the other events a contract system of record
    most wants pushed — a sub-processor engaged inside its
    notice window, a prior invention removed from the schedule
    it was protected by — because they happen BETWEEN decisions,
    when nobody is looking at the packet. The payload carries the
    finding (check, status, the movement the check computed) and
    never contract text: the receiving system learns what moved
    and why it is a finding, and reads the round itself from its
    own record. Returns ``None`` when no URL is configured (the
    default: the finding is announced on the event stream only),
    ``"sent"`` / ``"failed"`` otherwise; a failed dispatch never
    unmakes the finding or the analysis that produced it.
    """
    load_dotenv()
    url = env_str("ACTION_WEBHOOK_URL")
    if not url:
        return None
    return _post_signed_webhook(
        url, build_drift_finding_payload(result, check, prior_contract_id)
    )


def build_drift_finding_payload(
    result: AgentResult, check, prior_contract_id: str
) -> dict:
    """One drift finding as a webhook payload: the finding and
    the movement it computed, never contract text. Built from the
    check alone, so the outbox stores the exact payload posted."""
    return {
        "event": "drift_finding",
        "check": check.name,
        "status": check.status,
        "contract_id": result.contract_id,
        "prior_contract_id": prior_contract_id,
        "counterparty_name": result.counterparty_name,
        "exhibit_document_id": check.exhibit_document_id,
        "promised": check.promised,
        "found": check.found,
        "detail": check.detail,
    }


def build_stale_failure_payload(rows: list[dict], announced_at: str) -> dict:
    """One stale-failure sweep announcement as a webhook payload:
    every stale failure the sweep is announcing, named — the
    operator's system of record learns which deliveries died,
    for which contracts, and how long ago, in one event per
    sweep rather than one per entry. The rows are the outbox's
    own stale-failure rows (``webhook_outbox.stale_failure_rows``):
    identifiers and delivery facts, never payload contents —
    the announcement says a delivery is dead, not what it
    carried."""
    return {
        "event": "webhook_stale_failure",
        "announced_at": announced_at,
        "stale_count": len(rows),
        "failures": [
            {
                "outbox_id": row["outbox_id"],
                "family": row["family"],
                "contract_id": row["contract_id"],
                "label": row["label"],
                "age_days": row["age_days"],
                "attempts": row["attempts"],
                "last_attempt_at": row["last_attempt_at"],
            }
            for row in rows
        ],
    }


def dispatch_stale_failure_webhook(rows: list[dict], announced_at: str) -> str | None:
    """POST one stale-failure announcement to ``ACTION_WEBHOOK_URL``
    (off by default), on the approval channel's signing scheme.
    Returns ``None`` when no URL is configured (the announcement
    is then the event stream and the entries' stamps alone),
    ``"sent"`` / ``"failed"`` otherwise. The announcement is NOT
    itself recorded in the outbox: it is an ops meta-event about
    the ledger, and a failed announcement recorded as a failed
    entry would go stale and announce itself forever — the
    entries' announcement stamps are its record."""
    load_dotenv()
    url = env_str("ACTION_WEBHOOK_URL")
    if not url:
        return None
    return _post_signed_webhook(url, build_stale_failure_payload(rows, announced_at))


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
    proposal_store: PlaybookProposalStore | None = None
    calibration_store: CalibrationStore | None = None
    outbox_store: OutboxStore | None = None
    _resolved_store: Store | None = field(default=None, repr=False)
    _resolved_checkpointer: Checkpointer | None = field(default=None, repr=False)
    _checkpointer_resolved: bool = field(default=False, repr=False)
    _resolved_object_store: ObjectStore | None = field(default=None, repr=False)
    _resolved_proposal_store: PlaybookProposalStore | None = field(
        default=None, repr=False
    )
    _proposals_registered: bool = field(default=False, repr=False)
    _resolved_calibration_store: CalibrationStore | None = field(
        default=None, repr=False
    )
    _resolved_outbox_store: OutboxStore | None = field(
        default=None, repr=False
    )

    def _get_store(self) -> Store:
        if self._resolved_store is None:
            self._resolved_store = self.store or default_store()
        return self._resolved_store

    def _get_proposal_store(self) -> PlaybookProposalStore:
        if self._resolved_proposal_store is None:
            self._resolved_proposal_store = (
                self.proposal_store or default_proposal_store()
            )
        if not self._proposals_registered:
            self._proposals_registered = True
            # Activations are durable in the proposal store; the
            # registry is process-local, so a fresh process
            # re-registers them here, on first use.
            from .playbook_registry import register_entries

            for proposal in self._resolved_proposal_store.all():
                if proposal.status == "activated":
                    register_entries(
                        [entry.model_dump() for entry in proposal.entries]
                    )
            self._refresh_retriever()
        return self._resolved_proposal_store

    def _get_calibration_store(self) -> CalibrationStore:
        if self._resolved_calibration_store is None:
            self._resolved_calibration_store = (
                self.calibration_store or default_calibration_store()
            )
        return self._resolved_calibration_store

    def _get_outbox_store(self) -> OutboxStore:
        if self._resolved_outbox_store is None:
            self._resolved_outbox_store = (
                self.outbox_store or default_outbox_store()
            )
        return self._resolved_outbox_store

    def _refresh_retriever(self) -> None:
        """Push newly registered playbook entries into the held
        retriever's corpus (``retriever.refresh``): an activation
        is retrievable immediately, not on the next restart. A
        service with no held retriever builds one per run from the
        active playbook, so there is nothing to refresh; a pinned
        corpus keeps itself (refresh reports False)."""
        refresh = getattr(self.retriever, "refresh", None)
        if callable(refresh):
            refresh()

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
        # The same profile, structured, rides along for the option
        # scorer's capped history term (options.py) — the assessment
        # reads the lines; the scorer reads the tendencies.
        tendencies = self._concession_tendencies(model)
        if tendencies:
            history = {**(history or {}), "concession_tendencies": tendencies}
        # Reflection patterns already active in THIS contract's
        # chain (reflection_patterns.py, read off the stored rounds):
        # the option scorer's pattern term (options.py) reads them,
        # so the options stop scoring a twice-undelivered clause as
        # if nothing had happened.
        pattern_types = self._reflection_pattern_types(model)
        # Calibration acted on: a clause type whose pattern term
        # counsel has suspended does not get the term, whatever its
        # chain's patterns say — the set-aside is counsel's, and
        # the scorer honours it from the moment it is recorded.
        suspended = self._suspended_pattern_types()
        if suspended:
            pattern_types = pattern_types - suspended
        if pattern_types:
            history = {
                **(history or {}),
                "reflection_pattern_types": sorted(pattern_types),
            }
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
        self._attach_subprocessor_drift(model, result)
        self._attach_certificate_renewal(model, result)
        self._attach_prior_invention_drift(model, result)
        self._attach_lapsed_covenant_readings(model, result)
        self._dispatch_drift_findings(model, result, event_sink=event_sink)
        self._get_store().save(
            ApprovalRecord(
                result=result,
                contract=model.model_dump(mode="json"),
                analyzed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        )
        # A new round is evidence: any walk-away outcome along this
        # chain that was waiting on a follow-up resolves against it.
        self._refresh_chain_walk_away(model.contract_id)
        self._archive_documents(model, result)
        return result

    def _reflection_pattern_types(self, contract: ContractInput) -> set[str]:
        """Clause types under an active reflection pattern in the
        chain this contract continues, computed from the STORED
        rounds before the run starts (the scorer reads the patterns
        in force when drafting begins). A pattern that forms only
        on THIS round's own reflection is attached to this result
        after the run (``_attach_round_diff``) — it escalates this
        round's tiers immediately, and joins this set from the
        next round of the chain onward. Chain-scoped by the
        supersedes links, like every chain fact: another chain of
        the same counterparty does not leak in."""
        if not contract.supersedes or contract.supersedes == contract.contract_id:
            return set()
        from .reflection_patterns import find_reflection_patterns

        chain: list[AgentResult] = []
        seen = {contract.contract_id}
        supersedes = contract.supersedes
        while supersedes and supersedes not in seen:
            seen.add(supersedes)
            record = self._get_store().get(supersedes)
            if record is None:
                break
            chain.append(record.result)
            supersedes = (record.contract or {}).get("supersedes") or ""
        chain.reverse()
        return {p.clause_type for p in find_reflection_patterns(chain)}

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

    def _attach_subprocessor_drift(
        self, model: ContractInput, result: AgentResult
    ) -> None:
        """Sub-processor list drift (``subprocessor_drift.py``):
        when this round supersedes a stored one and both rounds
        carry a sub-processor list annex, diff the lists and check
        any addition against the clause's notice clock. Attached
        after the run like the round diff — the comparison needs
        the prior round's stored package, which only the service
        holds — and kept apart from the single-round exhibit
        checks the facts node already read."""
        if not model.supersedes or model.supersedes == model.contract_id:
            return
        prior = self._get_store().get(model.supersedes)
        if prior is None or not prior.contract:
            return
        from .schemas import TraceStep
        from .subprocessor_drift import build_subprocessor_drift

        prior_model = ContractInput.model_validate(prior.contract)
        drift = build_subprocessor_drift(prior_model, model)
        if not drift:
            return
        result.subprocessor_drift = drift
        check = drift[0]
        result.trace.append(
            TraceStep(
                name="subprocessor_drift",
                title=(
                    f"Sub-processor list drift vs {prior_model.contract_id}: "
                    f"{check.status}"
                ),
                details=[check.detail],
            )
        )

    def _chain_contracts(self, model: ContractInput) -> list[ContractInput]:
        """The supersedes chain behind ``model`` as stored
        contract inputs, oldest first, ending with the round
        ``model`` directly supersedes. (``_chain_rounds`` above
        walks the same links for RESULTS; this walk is for the
        intake packages, which is what requirement composition
        reads.) A missing or unstored ancestor ends the walk
        where the record ends — the chain is what the store can
        show, never a reconstruction. Cycles in bad intake
        terminate on the seen set."""
        store = self._get_store()
        rounds: list[ContractInput] = []
        seen = {model.contract_id}
        cursor = model.supersedes
        while cursor and cursor not in seen:
            seen.add(cursor)
            record = store.get(cursor)
            if record is None or not record.contract:
                break
            ancestor = ContractInput.model_validate(record.contract)
            rounds.append(ancestor)
            cursor = ancestor.supersedes
        rounds.reverse()
        return rounds

    def _attach_certificate_renewal(
        self, model: ContractInput, result: AgentResult
    ) -> None:
        """Renewal-certificate evidence (``certificate_renewal.py``):
        when this round supersedes a stored one and its certificate
        runs past the prior round's, the renewal is read against
        the requirement set composed across the whole chain —
        each requirement from the round that last set it — so
        limits or endorsements quietly dropped at renewal are a
        mismatch against the clause that set them even when the
        current draft has been restated to match the thinner
        certificate, and a short amendment round that restated
        nothing cannot thin the set. Attached after the run like
        the drift: the comparison needs the chain's stored
        packages."""
        if not model.supersedes or model.supersedes == model.contract_id:
            return
        prior = self._get_store().get(model.supersedes)
        if prior is None or not prior.contract:
            return
        from .certificate_renewal import build_certificate_renewal
        from .schemas import TraceStep

        prior_model = ContractInput.model_validate(prior.contract)
        renewal = build_certificate_renewal(
            prior_model, model, chain=self._chain_contracts(model)
        )
        if not renewal:
            return
        result.certificate_renewal = renewal
        check = renewal[0]
        result.trace.append(
            TraceStep(
                name="certificate_renewal",
                title=(
                    f"Certificate renewal vs the chain-composed "
                    f"requirement set: {check.status}"
                ),
                details=[check.detail],
            )
        )

    def _attach_prior_invention_drift(
        self, model: ContractInput, result: AgentResult
    ) -> None:
        """Prior-invention schedule drift (``prior_invention_drift.py``):
        when this round supersedes a stored one, the schedule's
        movement between the rounds is computed and attached — a
        removal is the finding, because the assignment excludes
        what the schedule lists. Attached after the run, like the
        sub-processor drift: the comparison needs the prior
        round's stored package."""
        if not model.supersedes or model.supersedes == model.contract_id:
            return
        prior = self._get_store().get(model.supersedes)
        if prior is None or not prior.contract:
            return
        from .prior_invention_drift import build_prior_invention_drift
        from .schemas import TraceStep

        prior_model = ContractInput.model_validate(prior.contract)
        drift = build_prior_invention_drift(prior_model, model)
        if not drift:
            return
        result.prior_invention_drift = drift
        check = drift[0]
        result.trace.append(
            TraceStep(
                name="prior_invention_drift",
                title=(
                    f"Prior-invention schedule drift vs "
                    f"{prior_model.contract_id}: {check.status}"
                ),
                details=[check.detail],
            )
        )

    def _attach_lapsed_covenant_readings(
        self, model: ContractInput, result: AgentResult
    ) -> None:
        """Lapsed-covenant readings (``covenant_history.py``):
        when this round supersedes stored rounds, the fresh
        covenants this letter carries are read against the
        chain's dated covenants of the same kind — where the
        nearest prior one has lapsed, the reading is attached:
        the prior round and its expiry named, the lapse's
        evidence (confirmed, notified, still open) named, and
        the new covenant read as the new bargain it is, not a
        continuation of the restriction that lapsed. Attached
        after the run, like the drift readings: the comparison
        needs the chain's stored records, follow-through acts
        included."""
        if not model.supersedes or model.supersedes == model.contract_id:
            return
        from datetime import date as _date

        from .covenant_history import build_lapsed_covenant_readings
        from .schemas import TraceStep

        store = self._get_store()
        prior_rounds: list[tuple[str, list, list]] = []
        seen = {model.contract_id}
        supersedes = model.supersedes
        while supersedes and supersedes not in seen:
            seen.add(supersedes)
            record = store.get(supersedes)
            if record is None:
                break
            prior_rounds.append(
                (
                    record.result.contract_id,
                    record.result.obligations,
                    record.covenant_follow_throughs,
                )
            )
            supersedes = (record.contract or {}).get("supersedes") or ""
        if not prior_rounds:
            return
        prior_rounds.reverse()
        readings = build_lapsed_covenant_readings(
            prior_rounds, result.obligations, _date.today()
        )
        if not readings:
            return
        result.lapsed_covenant_readings = readings
        for reading in readings:
            result.trace.append(
                TraceStep(
                    name="prior_covenant_lapsed",
                    title=(
                        f"Lapsed covenant read against this letter: "
                        f"{reading.found}"
                    ),
                    details=[reading.detail],
                )
            )

    def _dispatch_drift_findings(
        self,
        model: ContractInput,
        result: AgentResult,
        event_sink: EventSink | None = None,
    ) -> None:
        """Drift findings as pushed events (round 13): a drift
        check that came back a FINDING (``mismatch``) is announced
        the moment the round is analyzed — a ``drift_finding``
        event on the run's stream, and, when ``ACTION_WEBHOOK_URL``
        is configured, a signed webhook to the customer's system
        of record, with the outcome recorded on the result
        (``drift_dispatches``). Movement that breaks nothing
        (``consistent``) stays on the result and the brief: it is
        information for the reader, not an alarm for a system
        that was not in the room. Only the two drift families
        push — the certificate renewal has its own watch and
        sweep. A failed dispatch is recorded, never raised: the
        finding stands either way."""
        from .events import RunEvent
        from .schemas import TraceStep

        findings = [
            check
            for check in (
                *result.subprocessor_drift,
                *result.prior_invention_drift,
            )
            if check.status == "mismatch"
        ]
        if not findings:
            return
        prior_id = model.supersedes or ""
        for check in findings:
            if event_sink is not None:
                event_sink.emit(
                    RunEvent(
                        type="drift_finding",
                        contract_id=model.contract_id,
                        detail={
                            "check": check.name,
                            "status": check.status,
                            "prior_contract_id": prior_id,
                            "found": check.found,
                        },
                    )
                )
            dispatch = dispatch_drift_finding_webhook(result, check, prior_id)
            if dispatch is None:
                continue
            record_dispatch(
                self._get_outbox_store(),
                family="drift_finding",
                contract_id=result.contract_id,
                label=check.name,
                payload=build_drift_finding_payload(result, check, prior_id),
                outcome=dispatch,
            )
            result.drift_dispatches.append(
                DriftDispatch(
                    check=check.name,
                    status=check.status,
                    prior_contract_id=prior_id,
                    dispatch=dispatch,
                )
            )
            result.trace.append(
                TraceStep(
                    name="drift_webhook",
                    title=(
                        f"Drift finding pushed ({check.name}): {dispatch}"
                    ),
                    details=[check.detail],
                )
            )

    def annex_history(self, contract_id: str) -> dict:
        """The annex history for the chain ending at
        ``contract_id`` (``annex_history.py``): the sub-processor
        list's and the prior-invention schedule's whole timelines
        — each round's annex, each step's movement, the notice
        clock read on every addition, the transfer reading on
        every removal — in one view, where the drift checks give
        only the latest step. Computed from the stored intake
        packages with the drift modules' own readings, so the
        biography and the findings cannot disagree. Raises
        ``KeyError`` for a contract the store does not hold.

        The certificate chain's biography rides on the same view
        (``certificate_history.py``, round 16): each round's
        requirement set and certificate, each step's renewal
        verdict with its drops named — and the renewal watch's
        operative reading for the chain (``watch``), computed by
        the watch's own rule over an unbounded window: the
        biography names which expiry the watch stands on
        whatever its distance, because the window is the watch's
        concern, not the timeline's."""
        store = self._get_store()
        record = store.get(contract_id)
        if record is None or not record.contract:
            raise KeyError(
                f"Unknown contract_id: {contract_id} (analyze it first)"
            )
        from datetime import date as _date

        from .annex_history import build_annex_history
        from .certificate_history import build_certificate_history
        from .certificates import certificate_watches

        model = ContractInput.model_validate(record.contract)
        chain = [*self._chain_contracts(model), model]
        history = build_annex_history(chain)
        results = {}
        for link in chain:
            link_record = store.get(link.contract_id)
            if link_record is not None:
                results[link.contract_id] = link_record.result
        certificates = build_certificate_history(chain, results)
        if certificates is not None:
            chain_ids = {c.contract_id for c in chain}
            watch_rows = certificate_watches(
                store.all_records(), _date.today(), 36500
            )
            watch = next(
                (row for row in watch_rows if row["contract_id"] in chain_ids),
                None,
            )
            if watch is not None:
                # The watch's follow-through rides on the
                # biography too (round 17): the acts recorded
                # against the record the watch stands on, so the
                # timeline shows not only that the watch is open
                # but whether anyone acted on it.
                from .certificates import watch_follow_through_state

                source = store.get(watch["contract_id"])
                acts = (
                    list(source.certificate_watch_follow_throughs)
                    if source is not None
                    else []
                )
                watch = {
                    **watch,
                    "follow_through": acts,
                    "follow_through_state": watch_follow_through_state(acts),
                }
                certificates["watch"] = watch
            history["note"] += (
                " The certificate chain's biography rides under "
                "'certificates', computed with the renewal "
                "check's own readings; its 'watch' is the renewal "
                "watch's operative reading for this chain, with "
                "the follow-through acts recorded against it."
            )
        history["certificates"] = certificates
        return history

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

    def counterparty_profile(
        self, counterparty: str, matter: str | None = None
    ) -> dict:
        """The concession profile for one counterparty, computed
        from every stored record (see ``concessions.py``): clause
        types conceded vs held across their negotiation chains,
        re-trade counts, rounds to clear walk-away positions, and
        counsel's decision tallies. With a ``matter`` tag the
        profile is scoped to that matter's records alone — the
        all-matters profile stays the default. An unknown
        counterparty yields a zero profile — the caller decides
        whether that is a 404."""
        from .concessions import build_concession_profile

        return build_concession_profile(
            counterparty, self._get_store().all_records(), matter=matter
        )

    def _concession_lines(self, contract: ContractInput) -> list[str]:
        """The counterparty's concession profile as evidence lines
        for a new analysis, excluding the contract being analysed.
        Empty when there is no multi-round history — a pattern needs
        at least one negotiation, not one contract. A contract
        carrying a matter tag reads its own matter's profile: the
        counterparty's behaviour on other matters does not bleed
        into this one's priors."""
        if not contract.counterparty_name:
            return []
        from .concessions import build_concession_profile, concession_evidence_lines

        records = [
            record
            for record in self._get_store().all_records()
            if record.result.contract_id != contract.contract_id
        ]
        profile = build_concession_profile(
            contract.counterparty_name, records, matter=contract.matter or None
        )
        return concession_evidence_lines(profile)

    def _concession_tendencies(self, contract: ContractInput) -> dict:
        """The counterparty's concession profile as the option
        scorer's history input: clause type → {tendency, re_trades}
        for the types with a signal worth scoring on (a conceded or
        held type, or any type they have re-traded). Computed from
        the stored records only — the current contract is not in the
        store yet, and a counterparty with no multi-round chain has
        no tendencies: the history term is then simply absent. A
        matter-tagged contract scores on its own matter's
        tendencies, for the same reason its evidence lines do."""
        from .concessions import build_concession_profile

        records = [
            record
            for record in self._get_store().all_records()
            if record.result.contract_id != contract.contract_id
        ]
        profile = build_concession_profile(
            contract.counterparty_name, records, matter=contract.matter or None
        )
        tendencies: dict[str, dict] = {}
        for row in profile["clause_types"]:
            if row["tendency"] in ("concedes", "holds") or row["re_trades"]:
                tendencies[row["clause_type"]] = {
                    "tendency": row["tendency"],
                    "re_trades": row["re_trades"],
                }
        return tendencies

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
        self._capture_walk_away(record)
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
            record_dispatch(
                self._get_outbox_store(),
                family="approval",
                contract_id=record.result.contract_id,
                label="approved packet",
                payload=build_approval_payload(record),
                outcome=dispatch,
            )
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
        self._capture_walk_away(record)
        store.save(record)
        self._resume_thread(
            contract_id,
            {"decision": "rejected", "actor": reviewer, "reason": reason},
        )
        return record.result

    # ------------------------------------------------------------------
    # Walk-away line acceptance tracking (round 9)
    # ------------------------------------------------------------------

    def _capture_walk_away(self, record: ApprovalRecord) -> None:
        """Capture the walk-away lines the decided packet proposed
        (``walkaway.py``) onto the decision record, resolved against
        whatever of the chain is already in the store. Called by
        approve()/reject() after the decision fields are set, before
        the record is saved — the decision is the anchor, the chain
        (the follow-up round, when one exists) is the evidence."""
        from .walkaway import (
            capture_walk_away_outcomes,
            chain_for,
            resolve_walk_away_outcomes,
        )

        result = record.result
        outcomes = capture_walk_away_outcomes(result)
        if not outcomes:
            return
        chain = chain_for(self._get_store().all_records(), result.contract_id)
        if chain:
            outcomes = resolve_walk_away_outcomes(
                outcomes, chain, result.contract_id
            )
        result.walk_away_outcomes = outcomes

    def _refresh_chain_walk_away(self, contract_id: str) -> None:
        """Re-resolve the captured walk-away outcomes along the
        chain containing ``contract_id``: a new round's arrival is
        the evidence pending outcomes were waiting on. Resolution is
        deterministic over the stored chain, so re-resolving is
        idempotent; only records whose outcomes changed are saved."""
        from .walkaway import chain_for, resolve_walk_away_outcomes

        store = self._get_store()
        chain = chain_for(store.all_records(), contract_id)
        for record in chain:
            if not record.result.walk_away_outcomes:
                continue
            resolved = resolve_walk_away_outcomes(
                record.result.walk_away_outcomes,
                chain,
                record.result.contract_id,
            )
            if resolved != record.result.walk_away_outcomes:
                record.result.walk_away_outcomes = resolved
                store.save(record)

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

    def matter_brief(self, counterparty: str, matter: str) -> dict:
        """The matter brief: one markdown document across every
        chain that shares this counterparty and matter tag (see
        ``matter_brief.py``) — combined open positions, exhibit
        findings across the documents, and all obligations sorted
        by deadline — exported through the object-store port under
        ``matters/<slug>/`` with the chain brief's lineage
        discipline: versioned keys, a retained digest per export in
        the index, and a "Since the last matter brief" delta
        against the previous export. Raises KeyError when no stored
        contract carries the counterparty + matter pair, ValueError
        when the matter tag is empty (an untagged contract is in no
        matter)."""
        if not (matter or "").strip():
            raise ValueError("A matter tag is required — untagged contracts form no matter.")
        from .matter_brief import (
            build_matter_brief,
            matter_delta_lines,
            matter_records,
            matter_slug,
            matter_source_digest,
        )

        chains = matter_records(
            self._get_store().all_records(), counterparty, matter
        )
        if not chains:
            raise KeyError(
                f"No stored contracts for counterparty {counterparty!r} "
                f"in matter {matter!r} (analyze them first)"
            )
        objects = self._get_object_store()
        slug = matter_slug(counterparty, matter)
        index_key = f"matters/{slug}/matter-brief-index.json"
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
        digest = matter_source_digest(counterparty, matter, chains)
        delta = (
            matter_delta_lines(previous["digest"], digest)
            if previous is not None
            else []
        )
        markdown = build_matter_brief(
            counterparty,
            matter,
            chains,
            delta_lines=delta,
            first_export=previous is None,
        )
        seq = len(exports) + 1
        versioned_key = f"matters/{slug}/matter-brief-v{seq}.md"
        objects.put(versioned_key, markdown.encode("utf-8"), content_type="text/markdown")
        key = f"matters/{slug}/matter-brief.md"
        objects.put(key, markdown.encode("utf-8"), content_type="text/markdown")
        exports.append(
            {
                "seq": seq,
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
            "counterparty": counterparty,
            "matter": matter,
            "key": key,
            "markdown": markdown,
            "export_seq": seq,
            "versioned_key": versioned_key,
            "previous_export": (
                {"key": previous["key"]} if previous is not None else None
            ),
            "delta_lines": delta,
            "chains": len(chains),
            "contracts": sum(len(chain) for chain in chains),
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
        notice deadline counsel can act on, not just read.

        Restrictive-covenant rows carry their follow-through
        (round 16): whether the covenant has lapsed, the acts
        recorded against it, and its state — so the register
        never reads an expired restriction as a live one, and a
        lapse nobody closed is named ``open`` on the row."""
        from datetime import date as _date

        from .obligations import COVENANT_KINDS, due_obligations, follow_through_state

        today = today or _date.today()
        rows: list[dict] = []
        for record in self._get_store().all_records():
            result = record.result
            for row in due_obligations(result.obligations, today, days):
                row = {
                    "contract_id": result.contract_id,
                    "counterparty_name": result.counterparty_name,
                    "status": result.status,
                    **row,
                }
                if row["kind"] in COVENANT_KINDS:
                    lapsed = row["days_until"] <= 0
                    acts = [
                        act
                        for act in record.covenant_follow_throughs
                        if act.get("kind") == row["kind"]
                    ]
                    row["lapsed"] = lapsed
                    row["follow_through"] = acts
                    row["follow_through_state"] = follow_through_state(
                        record.covenant_follow_throughs, row["kind"],
                        lapsed=lapsed,
                    )
                rows.append(row)
        rows.sort(key=lambda row: (row["date"], row["contract_id"]))
        return {
            "as_of": today.isoformat(),
            "window_days": days,
            "count": len(rows),
            "obligations": rows,
        }

    def covenant_follow_through(self, contract_id: str, today=None) -> dict:
        """One contract's covenant register, read back with its
        follow-through (round 16): for each dated restrictive
        covenant on the contract's register — its expiry,
        whether it has lapsed, the acts recorded against it,
        and its state. Undated covenants are absent: their clock
        never started, so there is no lapse to follow through.
        Raises ``KeyError`` for a contract the store does not
        hold."""
        from datetime import date as _date

        from .obligations import COVENANT_KINDS, follow_through_state

        today = today or _date.today()
        record = self._get_store().get(contract_id)
        if record is None:
            raise KeyError(
                f"Unknown contract_id: {contract_id} (analyze it first)"
            )
        rows: list[dict] = []
        for obligation in record.result.obligations:
            if obligation.kind not in COVENANT_KINDS or not obligation.date:
                continue
            try:
                expiry = _date.fromisoformat(obligation.date)
            except ValueError:
                continue
            lapsed = expiry <= today
            acts = [
                act
                for act in record.covenant_follow_throughs
                if act.get("kind") == obligation.kind
            ]
            rows.append(
                {
                    "kind": obligation.kind,
                    "date": obligation.date,
                    "computed": obligation.computed,
                    "description": obligation.description,
                    "detail": obligation.detail,
                    "source_clause_id": obligation.source_clause_id,
                    "lapsed": lapsed,
                    "follow_through": acts,
                    "follow_through_state": follow_through_state(
                        record.covenant_follow_throughs, obligation.kind,
                        lapsed=lapsed,
                    ),
                }
            )
        rows.sort(key=lambda row: (row["date"], row["kind"]))
        return {
            "contract_id": contract_id,
            "counterparty_name": record.result.counterparty_name,
            "as_of": today.isoformat(),
            "covenants": rows,
            "open_lapses": sum(
                1 for row in rows if row["follow_through_state"] == "open"
            ),
            "note": (
                "A covenant's expiry is the day its restriction "
                "lifts. The sweep announces the lapse; the loop "
                "closes on the record — counterparty notified, "
                "then the restriction confirmed lifted — by the "
                "named acts in follow_through."
            ),
        }

    def record_covenant_follow_through(
        self, contract_id: str, kind: str, state: str, actor: str,
        note: str = "", today=None,
    ) -> dict:
        """Record one follow-through act on a lapsed covenant
        (round 16): the announcement said a restriction lifted;
        this is the act that closes the loop — the counterparty
        was notified, or the lapse stands confirmed — stamped
        with its actor on the contract's record.

        The act binds to reality: the contract must carry a
        DATED covenant of that kind, and the covenant must have
        lapsed (its expiry date reached) — there is no
        follow-through on a restriction still running, and the
        refusal names the date it lifts. Each state is recorded
        once per covenant: the record is the loop's state, and
        a repeat is refused naming the act already on it.
        Returns the contract's covenant register reading
        (:meth:`covenant_follow_through`)."""
        from datetime import date as _date

        from .obligations import COVENANT_FOLLOW_THROUGH_STATES, COVENANT_KINDS
        from .schemas import CovenantFollowThrough

        actor = (actor or "").strip()
        if not actor:
            raise ValueError(
                "A covenant follow-through act needs a named actor: "
                "it closes a lapse, and the record carries the name."
            )
        kind = (kind or "").strip()
        if kind not in COVENANT_KINDS:
            raise ValueError(
                f"kind {kind!r} is not a restrictive covenant — the "
                f"follow-through closes non_compete / non_solicit "
                "lapses, the register's covenant kinds."
            )
        state = (state or "").strip()
        if state not in COVENANT_FOLLOW_THROUGH_STATES:
            raise ValueError(
                f"state {state!r} is not a follow-through state — "
                "the loop closes as counterparty_notified, then "
                "restriction_confirmed_lifted."
            )
        store = self._get_store()
        record = store.get(contract_id)
        if record is None:
            raise KeyError(
                f"Unknown contract_id: {contract_id} (analyze it first)"
            )
        today = today or _date.today()
        covenant = next(
            (
                o
                for o in record.result.obligations
                if o.kind == kind and o.date
            ),
            None,
        )
        if covenant is None:
            raise ValueError(
                f"Contract {contract_id} carries no dated {kind} "
                "covenant on its register — an undated covenant's "
                "clock never started, so there is no lapse to "
                "follow through."
            )
        expiry = _date.fromisoformat(covenant.date)
        if expiry > today:
            raise ValueError(
                f"The {kind} covenant on {contract_id} does not "
                f"lapse until {covenant.date} — the restriction is "
                "still running, and there is no lapse to follow "
                "through yet."
            )
        existing = next(
            (
                act
                for act in record.covenant_follow_throughs
                if act.get("kind") == kind and act.get("state") == state
            ),
            None,
        )
        if existing is not None:
            raise ValueError(
                f"The {kind} covenant on {contract_id} already "
                f"carries a {state} act (by {existing.get('actor', '')} "
                f"on {existing.get('at', '')}) — a follow-through "
                "state is recorded once; the record is the loop's "
                "state, not a log of reminders."
            )
        record.covenant_follow_throughs.append(
            CovenantFollowThrough(
                kind=kind,
                state=state,
                actor=actor,
                at=datetime.now(timezone.utc).isoformat(),
                note=note or "",
            ).model_dump(mode="json")
        )
        store.save(record)
        return self.covenant_follow_through(contract_id, today=today)

    def certificate_watches(self, window_days: int | None = None, today=None) -> dict:
        """The certificate renewal watch (``certificates.py``):
        relied-upon certificates of insurance — the ones whose
        stored expiry check came back consistent — expiring within
        the watch window (``CERTIFICATE_EXPIRY_WATCH_DAYS``, default
        90, or ``window_days`` when given), soonest first, overdue
        included. A certificate that failed its review is a finding
        on the record, not a watch; this surface exists for the
        certificates nobody had a reason to doubt, expiring
        mid-term while the agreement runs on.

        Each row carries its follow-through (round 17): the acts
        recorded against the watch on the record it stands on,
        and their state — so the watch never reads as handled
        when nobody has acted, and a chased renewal is named as
        chased, not as renewed."""
        from datetime import date as _date

        from .certificates import (
            certificate_watch_window_days,
            certificate_watches,
            watch_follow_through_state,
        )

        today = today or _date.today()
        window = (
            certificate_watch_window_days() if window_days is None else window_days
        )
        records = self._get_store().all_records()
        rows = certificate_watches(records, today, window)
        by_id = {r.result.contract_id: r for r in records}
        for row in rows:
            record = by_id.get(row["contract_id"])
            acts = (
                list(record.certificate_watch_follow_throughs)
                if record is not None
                else []
            )
            row["follow_through"] = acts
            row["follow_through_state"] = watch_follow_through_state(acts)
        return {
            "as_of": today.isoformat(),
            "window_days": window,
            "count": len(rows),
            "certificates": rows,
        }

    def certificate_watch(self, contract_id: str, today=None) -> dict:
        """One contract's certificate renewal watch, read back
        with its follow-through (round 17): whether the contract
        the watch stands on is being watched at all, the expiry
        it stands on, the acts recorded against it, and its
        state. A contract whose certificate is not relied upon,
        or whose expiry sits outside the watch window, reads
        ``watched: False`` with the reason — the watch's silence
        about a contract is itself a reading, not an absence.
        Raises ``KeyError`` for a contract the store does not
        hold."""
        from datetime import date as _date

        today = today or _date.today()
        record = self._get_store().get(contract_id)
        if record is None:
            raise KeyError(
                f"Unknown contract_id: {contract_id} (analyze it first)"
            )
        view = self.certificate_watches(today=today)
        row = next(
            (r for r in view["certificates"] if r["contract_id"] == contract_id),
            None,
        )
        if row is None:
            return {
                "contract_id": contract_id,
                "counterparty_name": record.result.counterparty_name,
                "as_of": today.isoformat(),
                "watched": False,
                "follow_through": list(
                    record.certificate_watch_follow_throughs
                ),
                "note": (
                    "This contract carries no certificate on the "
                    "renewal watch: either its certificate was not "
                    "relied upon at review (a finding on the record, "
                    "not a watch), its chain's operative certificate "
                    "sits outside the watch window, a renewal "
                    "evidenced the requirement set and moved the "
                    "watch to a later round's record, or the watch "
                    "stands on an earlier round of its chain, where "
                    "a renewal failed the evidence check."
                ),
            }
        return {
            **row,
            "as_of": today.isoformat(),
            "watched": True,
            "note": (
                "The watch announces a relied-upon certificate "
                "expiring mid-term; the loop closes on the record — "
                "renewal chased, or the risk accepted by a named "
                "act of counsel — while the watch itself closes "
                "only on a renewal that evidences the requirement "
                "set in full."
            ),
        }

    def record_certificate_watch_follow_through(
        self, contract_id: str, state: str, actor: str,
        note: str = "", today=None,
    ) -> dict:
        """Record one follow-through act on a certificate renewal
        watch (round 17): the watch announced a relied-upon
        certificate expiring mid-term; this is the act that
        records someone did something about it — the renewal was
        chased, or counsel accepted the risk of the lapsing
        cover, with the note saying why — stamped with its actor
        on the record the watch stands on.

        The act binds to reality, the covenant discipline: the
        contract must currently stand on the watch (a relied-upon
        certificate inside the watch window) — there is no
        follow-through on a watch that is not running, and the
        refusal says so. ``risk_accepted`` requires the note: an
        accepted risk without its reason is not a record counsel
        can defend later. Each state is recorded once per watch:
        the record is the loop's state, and a repeat is refused
        naming the act already on it. Returns the contract's
        watch reading (:meth:`certificate_watch`)."""
        from datetime import date as _date

        from .certificates import CERTIFICATE_WATCH_FOLLOW_THROUGH_STATES
        from .schemas import CertificateWatchFollowThrough

        actor = (actor or "").strip()
        if not actor:
            raise ValueError(
                "A certificate-watch follow-through act needs a named "
                "actor: it records that someone acted on the watch, "
                "and the record carries the name."
            )
        state = (state or "").strip()
        if state not in CERTIFICATE_WATCH_FOLLOW_THROUGH_STATES:
            raise ValueError(
                f"state {state!r} is not a watch follow-through "
                "state — the loop closes as renewal_chased, or "
                "risk_accepted with the reason in the note."
            )
        if state == "risk_accepted" and not (note or "").strip():
            raise ValueError(
                "Accepting the risk of a lapsing certificate needs "
                "the note: the reason the cover may lapse is the "
                "record counsel defends later, not a formality."
            )
        store = self._get_store()
        record = store.get(contract_id)
        if record is None:
            raise KeyError(
                f"Unknown contract_id: {contract_id} (analyze it first)"
            )
        today = today or _date.today()
        view = self.certificate_watches(today=today)
        row = next(
            (r for r in view["certificates"] if r["contract_id"] == contract_id),
            None,
        )
        if row is None:
            raise ValueError(
                f"Contract {contract_id} carries no certificate on "
                "the renewal watch — its certificate was not relied "
                "upon at review, sits outside the watch window, or "
                "a renewal already evidenced the requirement set "
                "and moved the watch to a later round's record. "
                "There is no running watch to follow through."
            )
        existing = next(
            (
                act
                for act in record.certificate_watch_follow_throughs
                if act.get("state") == state
            ),
            None,
        )
        if existing is not None:
            raise ValueError(
                f"The certificate watch on {contract_id} already "
                f"carries a {state} act (by {existing.get('actor', '')} "
                f"on {existing.get('at', '')}) — a follow-through "
                "state is recorded once; the record is the loop's "
                "state, not a log of reminders."
            )
        record.certificate_watch_follow_throughs.append(
            CertificateWatchFollowThrough(
                state=state,
                actor=actor,
                at=datetime.now(timezone.utc).isoformat(),
                note=note or "",
                exhibit_document_id=row["exhibit_document_id"],
                expiry_date=row["expiry_date"],
            ).model_dump(mode="json")
        )
        store.save(record)
        return self.certificate_watch(contract_id, today=today)

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
        surfaces on the day it matters. The sweep also runs the
        certificate renewal watch (``certificates.py``): every
        relied-upon certificate of insurance expiring within the
        watch window (``CERTIFICATE_EXPIRY_WATCH_DAYS``, default 90 —
        its own window, not the obligations ``days``) draws one
        ``certificate_expiring`` event, so cover that passed review
        and lapses mid-term is announced by the same machinery.

        Restrictive-covenant expiries are announced distinctly
        (round 15): a due ``non_compete`` / ``non_solicit`` entry
        draws a ``covenant_expiring`` event, not an
        ``obligation_due``. The date on a covenant is the day a
        restriction LIFTS — operationally the opposite of a duty
        falling due — and consumers route the two differently:
        "a former employee may now be hired" is not "a notice
        deadline is near". The event carries the covenant kind
        and ``effect: restriction_lapses``; the report counts
        covenant announcements separately and lists them under
        ``covenants``.

        The announcement is follow-through-aware (round 17): a
        covenant whose lapse stands CONFIRMED on the record
        (``restriction_confirmed_lifted``, round 16) has been
        announced, acted on, and closed — the sweep does not
        announce it again. It is counted as closed
        (``covenant_closed_count``, listed under
        ``closed_covenants``) rather than re-announced, while a
        lapse still open — or only as far as the counterparty
        being notified — keeps announcing: the announcement is a
        chase that stops when the loop closes. The follow-through
        acts themselves stay on the record untouched: the
        announced history is the record's, and the sweep only
        reads it. **Report-only by design**: the
        sweep emits events to the configured sink and returns a
        report — it sends nothing to any counterparty or counsel
        address itself. Delivery (email, ticket, queue) is the
        consumer's job, behind the same event port as everything
        else. ``today`` is a parameter so the sweep is testable
        against a frozen clock."""
        from datetime import date as _date

        from .events import NullSink, RunEvent
        from .obligations import COVENANT_KINDS

        today = today or _date.today()
        due = self.obligations_due(days=days, today=today)
        sink = event_sink if event_sink is not None else NullSink()
        covenants: list[dict] = []
        closed_covenants: list[dict] = []
        for row in due["obligations"]:
            if row["kind"] in COVENANT_KINDS:
                covenants.append(row)
                if row.get("follow_through_state") == (
                    "restriction_confirmed_lifted"
                ):
                    # The loop is closed on the record: announced,
                    # acted on, confirmed. Counted, not re-announced.
                    closed_covenants.append(row)
                    continue
                sink.emit(
                    RunEvent(
                        type="covenant_expiring",
                        contract_id=row["contract_id"],
                        detail={
                            "via": "deadline_sweep",
                            "kind": row["kind"],
                            "effect": "restriction_lapses",
                            "date": row["date"],
                            "days_until": row["days_until"],
                            "overdue": row["overdue"],
                            "computed": row["computed"],
                            "description": row["description"],
                            "source_clause_id": row["source_clause_id"],
                            "counterparty_name": row["counterparty_name"],
                            "contract_status": row["status"],
                            "follow_through_state": row.get(
                                "follow_through_state", ""
                            ),
                        },
                    )
                )
                continue
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
        watches = self.certificate_watches(today=today)
        for row in watches["certificates"]:
            sink.emit(
                RunEvent(
                    type="certificate_expiring",
                    contract_id=row["contract_id"],
                    detail={
                        "via": "deadline_sweep",
                        "expiry_date": row["expiry_date"],
                        "days_until": row["days_until"],
                        "overdue": row["overdue"],
                        "counterparty_name": row["counterparty_name"],
                        "contract_status": row["status"],
                        "exhibit_document_id": row["exhibit_document_id"],
                    },
                )
            )
        swept = len(self._get_store().all_records())
        covenant_announced = len(covenants) - len(closed_covenants)
        return {
            "as_of": today.isoformat(),
            "window_days": days,
            "contracts_swept": swept,
            "due_count": due["count"],
            "events_emitted": due["count"] - len(closed_covenants),
            "obligation_events_emitted": due["count"] - len(covenants),
            "covenant_count": len(covenants),
            "covenant_events_emitted": covenant_announced,
            "covenant_closed_count": len(closed_covenants),
            "certificate_watch_window_days": watches["window_days"],
            "certificate_expiring_count": watches["count"],
            "certificate_events_emitted": watches["count"],
            "report_only": True,
            "note": (
                "Report-only sweep: one obligation_due event per due "
                "deadline, one covenant_expiring event per due "
                "restrictive-covenant expiry (a restriction lapsing, "
                "not a duty falling due), and one "
                "certificate_expiring event per watched certificate "
                "were emitted to the event sink and this report was "
                "produced. A covenant whose lapse stands confirmed "
                "on the record is counted closed, not re-announced: "
                "the chase stops when the loop closes. Nothing was "
                "sent to any counterparty or counsel address by "
                "this operation."
            ),
            "obligations": due["obligations"],
            "covenants": covenants,
            "closed_covenants": closed_covenants,
            "certificates": watches["certificates"],
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
                record_dispatch(
                    self._get_outbox_store(),
                    family="sla_breach",
                    contract_id=item["contract_id"],
                    label="queue SLA breach",
                    payload=build_sla_breach_payload(item, notified_at),
                    outcome=dispatch,
                )
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

    # ------------------------------------------------------------------
    # Webhook outbox (round 14): the delivery ledger + re-send act
    # ------------------------------------------------------------------

    def webhook_outbox(self, status: str | None = None) -> dict:
        """The webhook outbox view: every signed dispatch the
        service has attempted — approved packets, SLA breaches,
        drift findings — with its family, its target contract,
        its latest outcome, and its full attempt history. The
        stored payloads are NOT in this view (they carry drafts);
        the view answers the operational questions: what left,
        what landed, what is still failed and waiting on a
        re-send. ``status`` filters to one outcome (``failed``
        is the worklist). The ``staleness`` block reads the
        ledger's ageing (round 15): which failures have sat
        unrepaired past the stale threshold, and the retention
        rule the prune enforces — the same reading ``/metrics``
        and the counsel queue carry."""
        from .webhook_outbox import (
            entry_age_days,
            outbox_retention_days,
            outbox_stale_failure_days,
            stale_failure_rows,
            unannounced_stale_failures,
        )

        entries = self._get_outbox_store().all()
        entries.sort(key=lambda e: (e.created_at, e.outbox_id))
        counts = {
            "total": len(entries),
            "sent": sum(1 for e in entries if e.status == "sent"),
            "failed": sum(1 for e in entries if e.status == "failed"),
            "resends": sum(max(0, len(e.attempts) - 1) for e in entries),
            "by_family": dict(Counter(e.family for e in entries)),
        }
        stale = stale_failure_rows(entries)
        shown = [
            e for e in entries if status is None or e.status == status
        ]
        return {
            "counts": counts,
            "staleness": {
                "stale_threshold_days": outbox_stale_failure_days(),
                "retention_days": outbox_retention_days(),
                "stale_failed": len(stale),
                "stale_failures": stale,
                "unannounced_stale_failed": len(
                    unannounced_stale_failures(entries)
                ),
            },
            "entries": [
                {
                    "outbox_id": e.outbox_id,
                    "family": e.family,
                    "contract_id": e.contract_id,
                    "label": e.label,
                    "status": e.status,
                    "attempts": [a.model_dump(mode="json") for a in e.attempts],
                    "created_at": e.created_at,
                    "updated_at": e.updated_at,
                    "stale_announced_at": e.stale_announced_at,
                    "age_days": (
                        round(age, 1)
                        if (age := entry_age_days(e)) is not None
                        else None
                    ),
                }
                for e in shown
            ],
            "note": (
                "One entry per dispatch, recorded as it happened, "
                "with the exact payload stored for repair. A failed "
                "entry is re-sent deliberately (POST "
                "/webhooks/outbox/{id}/resend, actor required) — "
                "the event is never regenerated to recover a "
                "delivery. Sent entries older than the retention "
                "age may be pruned by a named operator (POST "
                "/webhooks/outbox/prune); failed entries are never "
                "pruned, and one left unrepaired past the stale "
                "threshold is named in the staleness block and "
                "announced by the stale-failure sweep (POST "
                "/webhooks/outbox/announce-stale) once per stale "
                "period — the stamp is on the entry."
            ),
        }

    def stale_webhook_failures(self) -> list[dict]:
        """The stale-failure worklist: failed outbox entries
        that have sat unrepaired past the stale threshold
        (``WEBHOOK_OUTBOX_STALE_FAILURE_DAYS``, default 7),
        oldest first. This is the reading the counsel queue
        carries beside its suspension alerts — a delivery that
        died is operational attention of the same kind as a
        contract nobody has picked up."""
        from .webhook_outbox import stale_failure_rows

        return stale_failure_rows(self._get_outbox_store().all())

    def dispatch_stale_failure_sweep(
        self, event_sink: EventSink | None = None
    ) -> dict:
        """The stale-failure announcement sweep (round 16): walk
        the outbox, and for every failed entry that has crossed
        the stale threshold unannounced, ANNOUNCE it — one
        ``webhook_stale_failure`` event per entry on the event
        stream, and one signed webhook per sweep naming them
        all when ``ACTION_WEBHOOK_URL`` is configured — then
        stamp each entry (``stale_announced_at``) so it is
        announced once per stale period, the SLA breach sweep's
        dedupe discipline.

        Round 15 surfaced stale failures on reads; a surface
        waits for someone to open it. The sweep is what a
        nightly job (or an operator, from the console) runs so a
        dead delivery reaches the operator's system of record
        instead. A re-send attempt clears an entry's stamp, so
        a delivery that was touched and failed again re-arms:
        if it sits for another stale period it is announced
        again. The announcement dispatch is not recorded in the
        outbox (see ``dispatch_stale_failure_webhook``) — the
        stamps are its record. Report-only in the same sense as
        the other sweeps: nothing here repairs a delivery; it
        makes sure somebody knows."""
        from datetime import date as _date

        from .events import NullSink, RunEvent
        from .webhook_outbox import (
            outbox_stale_failure_days,
            stale_failure_rows,
            unannounced_stale_failures,
        )

        sink = event_sink if event_sink is not None else NullSink()
        store = self._get_outbox_store()
        entries = store.all()
        stale_rows = stale_failure_rows(entries)
        rows_by_id = {row["outbox_id"]: row for row in stale_rows}
        due = [
            entry
            for entry in unannounced_stale_failures(entries)
            if entry.outbox_id in rows_by_id
        ]
        announced_at = datetime.now(timezone.utc).isoformat()
        announced: list[dict] = []
        for entry in due:
            row = rows_by_id[entry.outbox_id]
            sink.emit(
                RunEvent(
                    type="webhook_stale_failure",
                    contract_id=entry.contract_id,
                    detail={
                        "via": "stale_failure_sweep",
                        "outbox_id": entry.outbox_id,
                        "family": entry.family,
                        "label": entry.label,
                        "age_days": row["age_days"],
                        "attempts": row["attempts"],
                        "last_attempt_at": row["last_attempt_at"],
                        "announced_at": announced_at,
                    },
                )
            )
            entry.stale_announced_at = announced_at
            store.save(entry)
            announced.append({**row, "announced": True,
                              "stale_announced_at": announced_at})
        dispatch = None
        if announced:
            dispatch = dispatch_stale_failure_webhook(announced, announced_at)
        return {
            "as_of": _date.today().isoformat(),
            "announced_at": announced_at,
            "stale_threshold_days": outbox_stale_failure_days(),
            "stale_failed": len(stale_rows),
            "newly_announced": len(announced),
            "already_announced": len(stale_rows) - len(announced),
            "events_emitted": len(announced),
            "webhooks": {
                "configured": bool(env_str("ACTION_WEBHOOK_URL")),
                "sent": 1 if dispatch == "sent" else 0,
                "failed": 1 if dispatch == "failed" else 0,
            },
            "failures": announced,
            "note": (
                "One announcement per stale failure per stale "
                "period: entries are stamped as announced, and a "
                "re-send attempt clears the stamp, so a delivery "
                "touched and failed again is announced again when "
                "it crosses the threshold anew. The signed webhook "
                "fires once per sweep naming every newly stale "
                "failure, only when ACTION_WEBHOOK_URL is "
                "configured — otherwise the announcement is the "
                "event stream and the stamps."
            ),
        }

    def prune_webhook_outbox(self, actor: str) -> dict:
        """Prune the outbox under the retention rule: SENT
        entries older than ``WEBHOOK_OUTBOX_RETENTION_DAYS``
        (default 90) are removed; everything else stays.

        The prune is a governed act, not housekeeping on a
        timer: it takes a named operator, it runs only when
        called, and its report names what it took. FAILED
        entries are never pruned — a failure leaves the ledger
        only by being repaired and then ageing out as a sent
        entry — so the report also names the failures it kept,
        stale ones flagged: pruning the history must never be
        how a dead delivery gets forgotten."""
        from .webhook_outbox import (
            outbox_retention_days,
            outbox_stale_failure_days,
            prunable_sent_entries,
            stale_failure_rows,
        )

        actor = (actor or "").strip()
        if not actor:
            raise ValueError(
                "A prune needs a named actor: it removes delivery "
                "history, and the report carries the name."
            )
        store = self._get_outbox_store()
        entries = store.all()
        retention = outbox_retention_days()
        prunable = prunable_sent_entries(entries, retention_days=retention)
        pruned = []
        for entry in prunable:
            if store.delete(entry.outbox_id):
                pruned.append(entry.outbox_id)
        kept = store.all()
        return {
            "actor": actor,
            "pruned_at": datetime.now(timezone.utc).isoformat(),
            "retention_days": retention,
            "stale_threshold_days": outbox_stale_failure_days(),
            "pruned": pruned,
            "pruned_count": len(pruned),
            "kept_sent": sum(1 for e in kept if e.status == "sent"),
            "kept_failed": sum(1 for e in kept if e.status == "failed"),
            "stale_failures_kept": stale_failure_rows(kept),
            "note": (
                "Only sent entries past the retention age were "
                "removed. Failed entries are never pruned: a "
                "failure leaves the ledger by being repaired (a "
                "re-send that lands) and then ageing out as a "
                "sent entry."
            ),
        }

    def resend_webhook(
        self, outbox_id: str, actor: str, note: str = ""
    ) -> dict:
        """Re-send one FAILED outbox entry: the stored payload,
        posted again through the same signed seam, with the
        attempt recorded under the operator's name.

        The repair is an act on the delivery, not a replay of
        the event: the approval is not re-decided, the contract
        is not re-analysed, the breach is not re-announced — the
        bytes that failed to arrive are sent again, and the
        outbox entry keeps both attempts. Only a failed entry
        can be re-sent (a delivered one needs no repair, and
        re-posting it would double-deliver). The originating
        records keep their first outcome as their history; the
        outbox is the delivery ledger of record. Raises
        ``KeyError`` for an unknown entry and ``ValueError``
        when the entry is not failed, the actor is missing, or
        ``ACTION_WEBHOOK_URL`` is no longer configured (fix the
        endpoint first — a re-send to nowhere would only spend
        the attempt)."""
        actor = (actor or "").strip()
        if not actor:
            raise ValueError(
                "A re-send needs a named actor: it is a deliberate "
                "act on the delivery record."
            )
        store = self._get_outbox_store()
        entry = store.get(outbox_id)
        if entry is None:
            raise KeyError(f"Unknown outbox entry: {outbox_id}")
        if entry.status != "failed":
            raise ValueError(
                f"Outbox entry {outbox_id} is {entry.status} — only a "
                "failed delivery can be re-sent; re-posting a "
                "delivered payload would double-deliver it."
            )
        load_dotenv()
        url = env_str("ACTION_WEBHOOK_URL")
        if not url:
            raise ValueError(
                "ACTION_WEBHOOK_URL is not configured — set the "
                "endpoint before re-sending, or the attempt is spent "
                "on a destination that does not exist."
            )
        outcome = _post_signed_webhook(url, entry.payload)
        entry.attempts.append(
            WebhookAttempt(
                at=datetime.now(timezone.utc).isoformat(),
                outcome=outcome,
                actor=actor,
                note=note or "",
            )
        )
        entry.status = outcome
        entry.updated_at = entry.attempts[-1].at
        # The touch re-arms the stale announcement (round 16):
        # this stale period has been acted on, so if the entry
        # fails again and sits for another stale period, the
        # sweep announces it anew rather than treating the old
        # announcement as covering a delivery state nobody has
        # seen since.
        entry.stale_announced_at = ""
        store.save(entry)
        return {
            "outbox_id": entry.outbox_id,
            "family": entry.family,
            "contract_id": entry.contract_id,
            "label": entry.label,
            "status": entry.status,
            "attempts": [a.model_dump(mode="json") for a in entry.attempts],
            "resent_by": actor,
            "outcome": outcome,
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
        from .playbook_registry import active_playbook

        PLAYBOOK = active_playbook()
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

    # ------------------------------------------------------------------
    # Playbook proposals: drafted from coverage, decided by counsel
    # ------------------------------------------------------------------

    def generate_playbook_proposals(self) -> list[PlaybookProposal]:
        """Draft a playbook proposal for every coverage gap the
        stored runs support (``playbook_proposals.py``): a clause
        type flagged without fallback language at least
        ``MIN_GAP_FLAGS`` times, with no proposal already on record
        in any status (a dismissed gap stays dismissed) and no
        fallback coverage in the active playbook. Returns the
        proposals created by this call — generation is idempotent;
        a second call with no new gaps creates nothing."""
        from .playbook_proposals import (
            MIN_GAP_FLAGS,
            covered_clause_types,
            draft_playbook_proposal,
        )

        store = self._get_proposal_store()
        results = [record.result for record in self._get_store().all_records()]
        coverage = self.playbook_coverage(results)
        existing_types = {p.clause_type for p in store.all()}
        covered = covered_clause_types()
        created: list[PlaybookProposal] = []
        for row in coverage["clause_types_without_fallback"]:
            clause_type = row["clause_type"]
            if row["without_fallback"] < MIN_GAP_FLAGS:
                continue
            if clause_type in existing_types or clause_type in covered:
                continue
            examples: list[str] = []
            for result in results:
                if any(
                    verdict.clause_type == clause_type
                    and verdict.position != "preferred"
                    and not verdict.fallback_language
                    for verdict in result.clause_verdicts
                ):
                    examples.append(result.contract_id)
                if len(examples) >= 3:
                    break
            proposal = draft_playbook_proposal(
                clause_type,
                flagged=row["flagged"],
                without_fallback=row["without_fallback"],
                example_contract_ids=examples,
            )
            store.save(proposal)
            created.append(proposal)
        return created

    def list_playbook_proposals(
        self, status: str | None = None
    ) -> list[PlaybookProposal]:
        """Every proposal on record, awaiting counsel first."""
        proposals = self._get_proposal_store().all()
        if status:
            proposals = [p for p in proposals if p.status == status]
        return sorted(
            proposals, key=lambda p: (p.status != "proposed", p.clause_type)
        )

    def activate_playbook_proposal(
        self, proposal_id: str, actor: str, note: str = ""
    ) -> PlaybookProposal:
        """Counsel activates a proposal: its entries join the active
        playbook (``playbook_registry``) from this moment — verdicts
        may cite them, their fallback language may be offered, their
        walk-away lines may be proposed. The decision is stamped
        with the actor; the agent never calls this itself."""
        from .playbook_registry import register_entries

        store = self._get_proposal_store()
        proposal = store.get(proposal_id)
        if proposal is None:
            raise KeyError(f"Unknown playbook proposal: {proposal_id}")
        if proposal.status != "proposed":
            raise ValueError(
                f"Proposal {proposal_id} is already {proposal.status} — "
                "only a proposed proposal can be activated."
            )
        proposal.status = "activated"
        proposal.decided_by = actor
        proposal.decided_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        proposal.decision_note = note or ""
        store.save(proposal)
        register_entries([entry.model_dump() for entry in proposal.entries])
        self._refresh_retriever()
        return proposal

    def dismiss_playbook_proposal(
        self, proposal_id: str, actor: str, note: str = ""
    ) -> PlaybookProposal:
        """Counsel dismisses a proposal: it leaves the queue and is
        never re-drafted — the coverage gap stays visible in the
        metrics, with counsel's decision on record beside it."""
        store = self._get_proposal_store()
        proposal = store.get(proposal_id)
        if proposal is None:
            raise KeyError(f"Unknown playbook proposal: {proposal_id}")
        if proposal.status != "proposed":
            raise ValueError(
                f"Proposal {proposal_id} is already {proposal.status} — "
                "only a proposed proposal can be dismissed."
            )
        proposal.status = "dismissed"
        proposal.decided_by = actor
        proposal.decided_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        proposal.decision_note = note or ""
        store.save(proposal)
        return proposal

    def retire_playbook_proposal(
        self, proposal_id: str, actor: str, reason: str = ""
    ) -> PlaybookProposal:
        """Counsel retires an activated proposal: the lifecycle
        closes (proposed → activated → retired), its entries leave
        the active playbook and retrieval — the mirror image of
        activation — and the reason goes on the record. The
        proposal itself is never deleted: its effectiveness view
        keeps measuring the window it was live for, and reports
        what the clause type has done since the entries left,
        including whether the coverage gap re-opened. A reason is
        required — a retirement nobody explained is how playbooks
        silently thin out."""
        from .playbook_registry import unregister_entries

        if not (reason or "").strip():
            raise ValueError(
                "Retiring a playbook proposal requires a reason — "
                "it is recorded on the proposal, beside the actor."
            )
        store = self._get_proposal_store()
        proposal = store.get(proposal_id)
        if proposal is None:
            raise KeyError(f"Unknown playbook proposal: {proposal_id}")
        if proposal.status != "activated":
            raise ValueError(
                f"Proposal {proposal_id} is {proposal.status} — "
                "only an activated proposal can be retired."
            )
        proposal.status = "retired"
        proposal.retired_by = actor
        proposal.retired_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        proposal.retire_reason = reason
        store.save(proposal)
        unregister_entries([entry.id for entry in proposal.entries])
        self._refresh_retriever()
        return proposal

    def _suspended_pattern_types(self) -> set[str]:
        """Clause types whose pattern term is under an ACTIVE
        suspension — read from the calibration store on every
        analysis, so a suspension (or its lift) takes effect on
        the next run, not the next restart."""
        return {
            s.clause_type
            for s in self._get_calibration_store().all()
            if s.status == "active"
        }

    def pattern_suspensions(self) -> list[PatternTermSuspension]:
        """Every suspension record, active first: the acts counsel
        has taken on the pattern term, with both stamps."""
        suspensions = self._get_calibration_store().all()
        return sorted(
            suspensions, key=lambda s: (s.status != "active", s.clause_type)
        )

    def suspend_pattern_term(
        self, clause_type: str, actor: str, reason: str = "",
        review_by: str = "",
    ) -> PatternTermSuspension:
        """Counsel acts on a calibration candidate: the pattern
        term for this clause type is set aside. From the next
        analysis the scorer does not apply it to the type, and the
        calibration report reads the type as suspended rather than
        re-flagging it. A reason is required — an unexplained
        set-aside is how scorers silently rot — and the act is
        reversible by ``lift_pattern_suspension``, with both acts
        kept on the one record. ``review_by`` (an ISO date) gives
        the set-aside a shelf life: past it, the calibration
        report reads the suspension as stale and the queue carries
        it as a review alert, until counsel re-confirms it
        (``reconfirm_pattern_suspension``) or lifts it."""
        clause_type = (clause_type or "").strip()
        if not clause_type:
            raise ValueError("Suspending a pattern term requires a clause type.")
        if not (reason or "").strip():
            raise ValueError(
                "Suspending a pattern term requires a reason — "
                "it is recorded on the suspension, beside the actor."
            )
        review_by = (review_by or "").strip()
        if review_by:
            from datetime import date as _date

            try:
                _date.fromisoformat(review_by)
            except ValueError:
                raise ValueError(
                    f"review_by {review_by!r} is not an ISO date "
                    "(YYYY-MM-DD) — the review the suspension "
                    "promises has to be a date the report can read."
                ) from None
        store = self._get_calibration_store()
        existing = store.get(clause_type)
        if existing is not None and existing.status == "active":
            raise ValueError(
                f"The pattern term for {clause_type} is already "
                f"suspended (by {existing.suspended_by} on "
                f"{existing.suspended_at}) — lift that suspension "
                "before suspending again."
            )
        suspension = existing or PatternTermSuspension(clause_type=clause_type)
        suspension.status = "active"
        suspension.suspended_by = actor
        suspension.suspended_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        suspension.reason = reason
        suspension.review_by = review_by
        suspension.reconfirmations = []
        suspension.lifted_by = ""
        suspension.lifted_at = ""
        suspension.lift_note = ""
        store.save(suspension)
        return suspension

    def suspension_review_alerts(self, today=None) -> list[dict]:
        """Pattern-term suspensions whose review date is due or
        overdue (``pattern_calibration.suspension_review_alerts``):
        the queue's reminder that a set-aside has reached its
        shelf life. Counsel re-confirms or lifts; the scorer
        keeps honouring the suspension either way — staleness is
        a prompt, not an expiry of counsel's act."""
        from datetime import date as _date

        from .pattern_calibration import suspension_review_alerts

        return suspension_review_alerts(
            self._get_calibration_store().all(), today or _date.today()
        )

    def serial_renewal_alerts(self) -> list[dict]:
        """Pattern-term suspensions renewed into a pattern
        (``pattern_calibration.serial_renewal_alerts``, round
        16): the active set-asides whose re-confirmations have
        crossed the serial threshold, with the series attached.
        The queue carries them beside the due-review alerts —
        a "temporary" set-aside renewed for the fourth time is
        attention of the same kind, and the queue is where the
        next renewal would be signed."""
        from .pattern_calibration import serial_renewal_alerts

        return serial_renewal_alerts(self._get_calibration_store().all())

    def reconfirm_pattern_suspension(
        self, clause_type: str, actor: str, note: str = "",
        review_by: str = "",
    ) -> PatternTermSuspension:
        """Counsel renews a suspension (round 13): the set-aside
        was looked at and deliberately kept, before its review
        date arrived or after it went stale. The act is stamped
        on the record's re-confirmation history — actor, time,
        note, and the review date in force after the act — and
        the review clock resets to that date. It is NOT a lift
        (the term stays set aside; the scorer keeps honouring
        the suspension throughout) and NOT a second suspension
        (the original act's stamps — who set the term aside,
        when, and why — stand untouched, so the record keeps the
        continuity of one set-aside, renewed). ``review_by``
        names the new review date; left empty, the date already
        on the record stands — including no date, for a
        suspension that never carried one. A note is required,
        as the suspension's reason was: a renewal nobody
        explained is the same silent rot as a set-aside nobody
        explained."""
        clause_type = (clause_type or "").strip()
        if not (note or "").strip():
            raise ValueError(
                "Re-confirming a pattern-term suspension requires "
                "a note — it is recorded on the suspension, beside "
                "the actor."
            )
        review_by = (review_by or "").strip()
        if review_by:
            from datetime import date as _date

            try:
                _date.fromisoformat(review_by)
            except ValueError:
                raise ValueError(
                    f"review_by {review_by!r} is not an ISO date "
                    "(YYYY-MM-DD) — the renewed review the "
                    "suspension promises has to be a date the "
                    "report can read."
                ) from None
        store = self._get_calibration_store()
        suspension = store.get(clause_type)
        if suspension is None:
            raise KeyError(
                f"No pattern-term suspension on record for {clause_type!r}."
            )
        if suspension.status != "active":
            raise ValueError(
                f"The pattern term for {clause_type} is not suspended "
                f"(lifted by {suspension.lifted_by} on "
                f"{suspension.lifted_at}) — there is nothing to "
                "re-confirm."
            )
        if review_by:
            suspension.review_by = review_by
        suspension.reconfirmations.append(
            SuspensionReconfirmation(
                actor=actor,
                at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                note=note,
                review_by=suspension.review_by,
            )
        )
        store.save(suspension)
        return suspension

    def lift_pattern_suspension(
        self, clause_type: str, actor: str, note: str = ""
    ) -> PatternTermSuspension:
        """Counsel reverses a suspension: the pattern term applies
        to the clause type again from the next analysis. The lift
        is stamped on the same record — the suspension and its
        reversal stay readable together, so why the scorer treats
        the type as it does is always answerable."""
        clause_type = (clause_type or "").strip()
        store = self._get_calibration_store()
        suspension = store.get(clause_type)
        if suspension is None:
            raise KeyError(
                f"No pattern-term suspension on record for {clause_type!r}."
            )
        if suspension.status != "active":
            raise ValueError(
                f"The pattern term for {clause_type} is not suspended "
                f"(lifted by {suspension.lifted_by} on "
                f"{suspension.lifted_at}) — there is nothing to lift."
            )
        suspension.status = "lifted"
        suspension.lifted_by = actor
        suspension.lifted_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        suspension.lift_note = note or ""
        store.save(suspension)
        return suspension

    def pattern_calibration(self) -> dict:
        """The pattern-term calibration report (``pattern_calibration.py``):
        per clause type, how often the reflection-pattern score term
        was applied, how often it actually decided the packet's
        recommendation, and what counsel did with the escalations
        on decided records. Clause types whose term never decided
        anything are flagged as calibration candidates — for
        counsel; the report changes nothing itself. A type under an
        active suspension is read as suspended, with the act on the
        row, never re-flagged."""
        from .pattern_calibration import pattern_calibration

        return pattern_calibration(
            self._get_store().all_records(),
            suspensions=self._get_calibration_store().all(),
        )

    def proposal_effectiveness(self, proposal_id: str) -> dict:
        """What one playbook proposal has done since counsel decided
        it (``proposal_effectiveness.py``): for an activated
        proposal, the verdicts issued under each of its entries
        since activation, and whether its coverage gap — flagged
        verdicts with no fallback language — has recurred since.
        A retired proposal reports its live window plus the
        post-retirement window. A proposal that was never activated
        reports its status and no window, not zeros."""
        from .proposal_effectiveness import proposal_effectiveness

        proposal = self._get_proposal_store().get(proposal_id)
        if proposal is None:
            raise KeyError(f"Unknown playbook proposal: {proposal_id}")
        return proposal_effectiveness(proposal, self._get_store().all_records())

    def _pattern_calibration_summary(
        self, records: list[ApprovalRecord]
    ) -> dict:
        """The calibration headline for /metrics: totals and the
        candidate clause types. The full report — per-type evidence
        and the follow-through counts — lives at
        ``/options/pattern-calibration``."""
        from .pattern_calibration import pattern_calibration

        report = pattern_calibration(
            records, suspensions=self._get_calibration_store().all()
        )
        return {
            "applications": report["applications"],
            "decisive": report["decisive"],
            "calibration_candidates": report["calibration_candidates"],
            "suspended_types": report["suspended_types"],
            "suspensions_due_review": len(report["suspensions_due_review"]),
            "serially_renewed_types": report["serially_renewed_types"],
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
        outbox_view = self.webhook_outbox()
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
            "webhooks": {
                **outbox_view["counts"],
                "stale_failed": outbox_view["staleness"]["stale_failed"],
                "unannounced_stale_failed": outbox_view["staleness"][
                    "unannounced_stale_failed"
                ],
                "stale_threshold_days": outbox_view["staleness"][
                    "stale_threshold_days"
                ],
                "retention_days": outbox_view["staleness"]["retention_days"],
            },
            "playbook_coverage": self.playbook_coverage(results),
            "proposal_effectiveness": [
                self.proposal_effectiveness(proposal.proposal_id)
                for proposal in self._get_proposal_store().all()
                if proposal.status in ("activated", "retired")
            ],
            "pattern_calibration": self._pattern_calibration_summary(records),
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
