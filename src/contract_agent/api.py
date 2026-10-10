"""FastAPI surface for the Contract Review Agent.

Run:  uvicorn contract_agent.api:app    (or `make serve`)
UI:   http://localhost:8000/                (demo console, no build step)
Docs: http://localhost:8000/docs

Production shape: the app is stateless — records and checkpoints live
in PostgreSQL, documents in S3 — so replicas share one state.
``/health`` is liveness (the process answers); ``/readiness`` checks
the dependencies (database first) and returns 503 when the service
cannot take work. Logs are structured JSON with a per-request ID
(also returned as ``X-Request-ID``); configuration is env-only.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import queue
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from .config import env_str, load_dotenv, silence_langchain_deprecation_warnings

# Must precede the service import (which loads the graph/langgraph).
silence_langchain_deprecation_warnings()

from .errors import ProviderError
from .events import CallbackSink, RunEvent
from .playbook_data import PLAYBOOK
from .samples import load_sample_contracts
from .schemas import (
    AgentResult,
    AssignRequest,
    ContractInput,
    DecisionRequest,
    EditOutcomesRequest,
    PatternSuspensionRequest,
    ProposalDecisionRequest,
    WebhookResendRequest,
)
from .wiring import build_service_from_env

# Load the repo-root .env at startup (real environment variables win), so
# MODEL_BACKEND / RETRIEVER / API keys can live in the file — see .env.example.
load_dotenv()


# ---------------------------------------------------------------------------
# Structured JSON logging + request IDs
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """One JSON object per log line: ts, level, logger, message, and
    the request context (request_id / method / path / status /
    duration_ms) when the record carries it."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "method", "path", "status", "duration_ms"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _configure_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    logger = logging.getLogger("contract_agent")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


log = _configure_logging()


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Optional API-key gate, enabled by setting ``API_KEY``.

    When ``API_KEY`` is set, every data endpoint (read + mutating)
    requires the matching ``X-API-Key`` header. When it is unset the
    API is open — the local-dev default, stated here and in the docs
    rather than implied. The console page and the health/readiness
    probes stay open either way; the console carries an API-key field
    for the gated calls.
    """
    expected = env_str("API_KEY")
    if not expected:
        return
    if x_api_key != expected:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key (send the X-API-Key header).",
        )


_AUTH = [Depends(require_api_key)]


@asynccontextmanager
async def _lifespan(app: FastAPI):
    log.info("contract review agent starting (stateless app; state in PostgreSQL + S3)")
    yield
    log.info("contract review agent shutting down (graceful: in-flight requests finish, no state held in-process)")


app = FastAPI(
    title="Trida AI Blueprint — Contract Review Agent",
    description="Reference prototype. Synthetic data only. Drafts stop at a counsel gate; the agent never states a legal conclusion, never approves a contract, and never sends redlines.",
    version="0.1.0",
    lifespan=_lifespan,
)

service = build_service_from_env()


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Attach a request ID and emit one structured access log line."""
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    started = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    response.headers["X-Request-ID"] = request_id
    log.info(
        "request",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    return response


@app.exception_handler(ProviderError)
def provider_error_handler(request: Request, exc: ProviderError) -> JSONResponse:
    """A provider failure is a clean 502 with the actionable, translated
    message (backend, endpoint, likely fix) — never a stack dump."""
    return JSONResponse(status_code=502, content={"detail": str(exc)})


_STATIC_DIR = Path(__file__).parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"
_BRAND_DIR = _STATIC_DIR / "brand"

# Branded static assets (logo, favicon) for the demo console.
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/favicon.svg", include_in_schema=False)
def favicon_svg() -> FileResponse:
    return FileResponse(_BRAND_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico() -> FileResponse:
    return FileResponse(_BRAND_DIR / "favicon.ico", media_type="image/x-icon")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _INDEX_HTML.read_text(encoding="utf-8")


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is up and answering."""
    return {"status": "ok", "prototype": "trida-blueprint-contract-review"}


@app.get("/readiness")
def readiness() -> JSONResponse:
    """Readiness: the dependencies answer. The database is the system
    of record — when it is unreachable the service must not take work
    (503). With no DATABASE_URL configured (offline demo), readiness
    reports the in-memory mode honestly instead of claiming health."""
    checks: dict[str, str] = {}
    store = service._get_store()
    if hasattr(store, "ping"):
        checks["database"] = "ok" if store.ping() else "unreachable"
    else:
        checks["database"] = "in-memory (DATABASE_URL not set)"
    ready = checks["database"] in ("ok", "in-memory (DATABASE_URL not set)")
    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not ready", "checks": checks},
    )


@app.get("/evals/results", dependencies=_AUTH)
def eval_results() -> dict:
    """Latest eval summary, as emitted by ``evals/run_evals.py``.

    Looked up in the packaged copy first (present in installs/Docker),
    then in a repo checkout's ``evals/results.json``. 404 when no eval
    run has been recorded yet — the console shows a 'run evals' state.
    """
    candidates = [
        Path(__file__).parent / "data" / "eval_results.json",
        Path.cwd() / "evals" / "results.json",
        Path(__file__).resolve().parents[2] / "evals" / "results.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise HTTPException(
        status_code=404,
        detail="No eval results yet. Run: python evals/run_evals.py",
    )


@app.get("/metrics", dependencies=_AUTH)
def metrics() -> dict:
    """Operational summary over the stored runs: counts by status and
    posture, decision tallies, guardrail/repair/reviewer outcomes, and
    telemetry aggregates (calls, tokens, estimated cost, mean latency).
    Aggregates only — no draft text."""
    return service.metrics()


@app.get("/options/pattern-calibration", dependencies=_AUTH)
def pattern_calibration() -> dict:
    """The pattern-term calibration report: per clause type, how
    often the reflection-pattern score term was applied to a scored
    option, how often it actually decided the packet's
    recommendation, and what counsel did with those escalations on
    decided records. Types whose term never decided anything are
    flagged as calibration candidates — the report proposes for
    counsel; it changes nothing itself. A type under an active
    suspension is read as suspended, with the act on its row."""
    return service.pattern_calibration()


@app.get("/options/pattern-calibration/suspensions", dependencies=_AUTH)
def pattern_suspensions() -> dict:
    """The pattern-term suspensions on record: clause types whose
    term counsel has set aside (active), and the ones since lifted
    — each with the actor, the reason, and both stamps."""
    suspensions = service.pattern_suspensions()
    return {
        "count": len(suspensions),
        "suspensions": [s.model_dump(mode="json") for s in suspensions],
    }


@app.post("/options/pattern-calibration/suspend", dependencies=_AUTH)
def suspend_pattern_term(request: PatternSuspensionRequest):
    """Counsel acts on a calibration candidate: the pattern term
    for the clause type is suspended — the scorer stops applying
    it to that type, and the calibration report reads the type as
    suspended. Requires the actor's name and a reason (the
    request's 'note') — both go on the record. Reversible via the
    lift endpoint. The agent never calls this."""
    actor = (request.actor or "").strip()
    if not actor:
        raise HTTPException(
            status_code=422,
            detail="Provide 'actor' — the counsel member suspending this term.",
        )
    if not (request.note or "").strip():
        raise HTTPException(
            status_code=422,
            detail="Provide 'note' — the reason for suspending this term.",
        )
    try:
        suspension = service.suspend_pattern_term(
            request.clause_type or "", actor=actor, reason=request.note or "",
            review_by=request.review_by or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return suspension.model_dump(mode="json")


@app.post("/options/pattern-calibration/lift", dependencies=_AUTH)
def lift_pattern_suspension(request: PatternSuspensionRequest):
    """Counsel lifts a pattern-term suspension: the term applies
    to the clause type again from the next analysis. Requires the
    actor's name; the lift is stamped on the suspension record,
    beside the act it reverses."""
    actor = (request.actor or "").strip()
    if not actor:
        raise HTTPException(
            status_code=422,
            detail="Provide 'actor' — the counsel member lifting this suspension.",
        )
    try:
        suspension = service.lift_pattern_suspension(
            request.clause_type or "", actor=actor, note=request.note or ""
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return suspension.model_dump(mode="json")


@app.post("/options/pattern-calibration/reconfirm", dependencies=_AUTH)
def reconfirm_pattern_suspension(request: PatternSuspensionRequest):
    """Counsel re-confirms a pattern-term suspension: the
    set-aside was reviewed and deliberately kept, and its review
    clock resets. A distinct act from lifting (the term stays
    set aside) and from re-suspending (the original act's stamps
    stand). Requires the actor's name and a note — both go on
    the record's re-confirmation history; 'review_by' may name
    the new review date. The agent never calls this."""
    actor = (request.actor or "").strip()
    if not actor:
        raise HTTPException(
            status_code=422,
            detail="Provide 'actor' — the counsel member re-confirming this suspension.",
        )
    if not (request.note or "").strip():
        raise HTTPException(
            status_code=422,
            detail="Provide 'note' — why the set-aside still stands.",
        )
    try:
        suspension = service.reconfirm_pattern_suspension(
            request.clause_type or "", actor=actor, note=request.note or "",
            review_by=request.review_by or "",
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return suspension.model_dump(mode="json")


@app.get("/queue", dependencies=_AUTH)
def counsel_queue() -> dict:
    """The counsel worklist: contracts awaiting a decision — SLA
    breaches first, then worst-first (posture severity, worst clause
    position, age) — each with its autonomy flag, queue owner, and
    SLA state (budget by severity tier, business days waited). The
    payload also carries ``suspension_review_alerts``: pattern-term
    suspensions whose review date has come due — the queue is where
    counsel works, so the set-asides reaching their shelf life are
    surfaced there, beside the contracts."""
    items = service.queue()
    return {
        "count": len(items),
        "queue": items,
        "suspension_review_alerts": service.suspension_review_alerts(),
    }


@app.post("/queue/sla-sweep", dependencies=_AUTH)
def sla_breach_sweep() -> dict:
    """The SLA breach sweep: announce every queued contract that
    has crossed its SLA budget — one ``sla_breach`` event per
    contract the first time it is observed in breach (deduped on
    the record), plus a signed webhook per breach when
    ACTION_WEBHOOK_URL is configured. Returns the sweep report."""
    return service.dispatch_sla_breach_sweep()


@app.get("/obligations/due", dependencies=_AUTH)
def obligations_due(days: int = 90) -> dict:
    """Obligation deadlines across all stored contracts: register
    entries carrying a date that fall due within ``days`` (overdue
    included), soonest first — the operational view of the register."""
    if days < 0:
        raise HTTPException(status_code=422, detail="days must be >= 0.")
    return service.obligations_due(days=days)


@app.post("/obligations/sweep", dependencies=_AUTH)
def obligations_sweep(days: int = 90) -> dict:
    """The deadline dispatch sweep: emit one ``obligation_due`` event
    per stored-contract deadline due within ``days`` and return the
    dispatch report. Report-only — the sweep sends nothing itself;
    delivery belongs to the event consumer."""
    if days < 0:
        raise HTTPException(status_code=422, detail="days must be >= 0.")
    return service.dispatch_obligation_sweep(days=days)


@app.get("/certificates/expiring", dependencies=_AUTH)
def certificates_expiring(window_days: int | None = None) -> dict:
    """The certificate renewal watch: relied-upon certificates of
    insurance (their stored expiry check came back consistent)
    expiring within the watch window — ``CERTIFICATE_EXPIRY_WATCH_DAYS``,
    default 90, or ``window_days`` when given — soonest first,
    overdue included. The deadline sweep announces the same watches
    as ``certificate_expiring`` events; this is the read surface."""
    if window_days is not None and window_days < 0:
        raise HTTPException(status_code=422, detail="window_days must be >= 0.")
    return service.certificate_watches(window_days=window_days)


@app.get("/audit/export", dependencies=_AUTH)
def audit_export(format: str = "json"):
    """The audit trail — one row per analysed contract with counsel's
    decision (who, when, why) and the safety outcomes. JSON by
    default; ``?format=csv`` downloads a CSV for a compliance review."""
    rows = service.audit_rows()
    if format == "csv":
        buffer = io.StringIO()
        fieldnames = list(rows[0].keys()) if rows else [
            "contract_id", "counterparty_name", "doc_type", "posture",
            "worst_position", "status", "decision", "decided_by",
            "decision_reason", "decided_at", "validation_passed",
            "repair_attempted", "repaired", "reviewer_blocked",
            "dispatch_status", "external_action_taken",
        ]
        writer = csv.DictWriter(buffer, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        return PlainTextResponse(
            buffer.getvalue(),
            media_type="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=contract-audit.csv"
            },
        )
    if format != "json":
        raise HTTPException(status_code=422, detail="format must be 'json' or 'csv'.")
    return {"count": len(rows), "records": rows}


@app.get("/playbook", dependencies=_AUTH)
def list_playbook() -> list[dict]:
    return PLAYBOOK


@app.get("/playbook/proposals", dependencies=_AUTH)
def list_playbook_proposals(status: str = "") -> dict:
    """The playbook proposal queue: candidate entries drafted from
    coverage gaps (clause types that keep arriving flagged with no
    fallback language), each PROPOSED until counsel activates or
    dismisses it. Nothing here is active playbook. Activated
    proposals carry their effectiveness view (verdicts issued under
    each entry since activation; whether the gap recurred)."""
    proposals = service.list_playbook_proposals(status=status or None)
    rendered = []
    for proposal in proposals:
        row = proposal.model_dump(mode="json")
        if proposal.status in ("activated", "retired"):
            row["effectiveness"] = service.proposal_effectiveness(
                proposal.proposal_id
            )
        rendered.append(row)
    return {"count": len(rendered), "proposals": rendered}


@app.get("/playbook/proposals/{proposal_id}/effectiveness", dependencies=_AUTH)
def playbook_proposal_effectiveness(proposal_id: str) -> dict:
    """What one proposal has done since counsel's decision: for an
    activated proposal, verdicts issued under each of its entries
    since activation, the clause type's verdicts and positions over
    the same window, and whether flagged-without-fallback verdicts
    for the type recurred (the gap the activation was meant to
    close). A never-activated proposal reports its status instead."""
    try:
        return service.proposal_effectiveness(proposal_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/playbook/proposals/generate", dependencies=_AUTH)
def generate_playbook_proposals() -> dict:
    """Draft proposals for the coverage gaps the stored runs support
    (idempotent — gaps with a proposal on record, in any status, are
    not re-drafted). Returns what this call created."""
    created = service.generate_playbook_proposals()
    return {
        "created": len(created),
        "proposals": [p.model_dump(mode="json") for p in created],
    }


@app.post("/playbook/proposals/{proposal_id}/activate", dependencies=_AUTH)
def activate_playbook_proposal(proposal_id: str, request: ProposalDecisionRequest):
    """Counsel activates a proposal: its entries join the active
    playbook. Requires the actor's name — an activation is a human
    decision with a name on it. The agent never calls this."""
    actor = (request.actor or "").strip()
    if not actor:
        raise HTTPException(
            status_code=422,
            detail="Provide 'actor' — the counsel member activating this proposal.",
        )
    try:
        proposal = service.activate_playbook_proposal(
            proposal_id, actor=actor, note=request.note or ""
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return proposal.model_dump(mode="json")


@app.post("/playbook/proposals/{proposal_id}/dismiss", dependencies=_AUTH)
def dismiss_playbook_proposal(proposal_id: str, request: ProposalDecisionRequest):
    """Counsel dismisses a proposal: it leaves the queue and is never
    re-drafted. Requires the actor's name, like activation."""
    actor = (request.actor or "").strip()
    if not actor:
        raise HTTPException(
            status_code=422,
            detail="Provide 'actor' — the counsel member dismissing this proposal.",
        )
    try:
        proposal = service.dismiss_playbook_proposal(
            proposal_id, actor=actor, note=request.note or ""
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return proposal.model_dump(mode="json")


@app.post("/playbook/proposals/{proposal_id}/retire", dependencies=_AUTH)
def retire_playbook_proposal(proposal_id: str, request: ProposalDecisionRequest):
    """Counsel retires an activated proposal: its entries leave the
    active playbook, the lifecycle closes, and the effectiveness
    record is preserved. Requires the actor's name and a reason
    (the request's 'note') — both go on the record."""
    actor = (request.actor or "").strip()
    if not actor:
        raise HTTPException(
            status_code=422,
            detail="Provide 'actor' — the counsel member retiring this proposal.",
        )
    if not (request.note or "").strip():
        raise HTTPException(
            status_code=422,
            detail="Provide 'note' — the reason for retiring this proposal.",
        )
    try:
        proposal = service.retire_playbook_proposal(
            proposal_id, actor=actor, reason=request.note or ""
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return proposal.model_dump(mode="json")


@app.get("/webhooks/outbox", dependencies=_AUTH)
def webhook_outbox(status: str | None = None) -> dict:
    """The webhook outbox: every signed dispatch attempted —
    approved packets, SLA breaches, drift findings — with its
    family, target contract, latest outcome and attempt history
    (payloads are not listed; they carry drafts). ``?status=failed``
    is the repair worklist."""
    return service.webhook_outbox(status=status)


@app.post("/webhooks/outbox/{outbox_id}/resend", dependencies=_AUTH)
def resend_webhook(outbox_id: str, body: WebhookResendRequest) -> dict:
    """Re-send one failed outbox entry: the stored payload,
    posted again through the signed seam, the attempt recorded
    under the operator's name. 404 on an unknown entry, 422
    when the entry is not failed, the actor is missing, or the
    webhook endpoint is not configured."""
    try:
        return service.resend_webhook(
            outbox_id, actor=body.actor or "", note=body.note or ""
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/counterparties/{counterparty}/profile", dependencies=_AUTH)
def counterparty_profile(counterparty: str) -> dict:
    """The counterparty concession profile: clause types they
    concede vs hold across their stored negotiation chains,
    re-trade counts, rounds to clear walk-away positions, and
    counsel's decision tallies — computed in code from the records.
    404 when the store holds nothing for this counterparty."""
    profile = service.counterparty_profile(counterparty)
    if profile["contracts"] == 0:
        raise HTTPException(
            status_code=404,
            detail=f"No stored contracts for counterparty: {counterparty}",
        )
    return profile


@app.get("/samples", dependencies=_AUTH)
def list_samples() -> list[dict]:
    return load_sample_contracts()


def _decision_actor(request: DecisionRequest) -> str:
    actor = request.actor_name()
    if not actor:
        raise HTTPException(
            status_code=422,
            detail="Provide 'actor' — counsel's name ('approver'/'reviewer' are accepted legacy aliases).",
        )
    return actor


@app.post("/contracts/analyze", response_model=AgentResult, dependencies=_AUTH)
def analyze(contract: ContractInput) -> AgentResult:
    return service.analyze(contract)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@app.post("/contracts/analyze/stream", dependencies=_AUTH)
def analyze_stream(contract: ContractInput) -> StreamingResponse:
    """The same analysis as ``POST /contracts/analyze``, streamed.

    The run executes on a worker thread; its structured events (see
    ``events.py``) are forwarded as Server-Sent Events as they happen.
    The final event is ``run_completed`` and carries the full result
    JSON under ``result`` — the same payload the non-stream endpoint
    returns. A failed run ends the stream with a ``run_failed`` event
    carrying the clean, translated error message (no stack dump).
    """
    events_queue: queue.Queue = queue.Queue()
    sink = CallbackSink(events_queue.put)
    outcome: dict = {}

    def work() -> None:
        try:
            outcome["result"] = service.analyze(contract, event_sink=sink)
        except Exception as exc:  # surfaced as the run_failed event below
            outcome["error"] = exc
        finally:
            events_queue.put(None)  # sentinel: the run is over

    threading.Thread(target=work, daemon=True).start()

    def generate():
        completed: RunEvent | None = None
        while True:
            event = events_queue.get()
            if event is None:
                break
            if event.type == "run_completed":
                completed = event  # held back: it goes out last, with the result
                continue
            yield _sse(event.to_dict())
        if "error" in outcome:
            yield _sse(
                {
                    "type": "run_failed",
                    "contract_id": contract.contract_id,
                    "node": None,
                    "duration_ms": None,
                    "detail": {"error": str(outcome["error"])},
                }
            )
            return
        final = (
            completed.to_dict()
            if completed is not None
            else {
                "type": "run_completed",
                "contract_id": contract.contract_id,
                "node": None,
                "duration_ms": None,
                "detail": {"status": outcome["result"].status},
            }
        )
        final["result"] = outcome["result"].model_dump(mode="json")
        yield _sse(final)

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.get("/contracts/{contract_id}", response_model=AgentResult, dependencies=_AUTH)
def get_result(contract_id: str) -> AgentResult:
    result = service.get(contract_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Contract not analyzed yet.")
    return result


@app.get("/contracts/{contract_id}/brief", dependencies=_AUTH)
def negotiation_brief(contract_id: str):
    """The negotiation brief as a markdown download: chain history,
    re-trades, open positions by tier, proposed edits, exhibit
    findings, and obligations — assembled in code from the stored
    records and exported via the object-store port. Draft for
    counsel review; the agent sends it nowhere."""
    try:
        brief = service.negotiation_brief(contract_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PlainTextResponse(
        brief["markdown"],
        media_type="text/markdown",
        headers={
            "Content-Disposition": (
                f"attachment; filename={contract_id}-negotiation-brief.md"
            ),
            "X-Object-Key": brief["key"],
        },
    )


@app.get("/matters/brief", dependencies=_AUTH)
def matter_brief(counterparty: str, matter: str):
    """The matter brief as a markdown download: every chain sharing
    this counterparty + matter tag — combined open positions,
    exhibit findings across the documents, and all obligations
    sorted by deadline — assembled in code from the stored records
    and exported via the object-store port. Draft for counsel
    review; the agent sends it nowhere."""
    try:
        brief = service.matter_brief(counterparty, matter)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PlainTextResponse(
        brief["markdown"],
        media_type="text/markdown",
        headers={
            "Content-Disposition": "attachment; filename=matter-brief.md",
            "X-Object-Key": brief["key"],
        },
    )


@app.post("/contracts/{contract_id}/approve", response_model=AgentResult, dependencies=_AUTH)
def approve(contract_id: str, request: DecisionRequest) -> AgentResult:
    try:
        return service.approve(
            contract_id,
            approver=_decision_actor(request),
            reason=request.reason or "",
            edit_outcomes=request.edit_outcomes,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/contracts/{contract_id}/edits", response_model=AgentResult, dependencies=_AUTH)
def record_edit_outcomes(contract_id: str, request: EditOutcomesRequest) -> AgentResult:
    """Record counsel's per-edit outcomes (accepted / modified /
    rejected, with a note) on a stored record. Bookkeeping for the
    negotiation — the follow-up round's reflection check reads the
    accepted/modified outcomes and flags agreed language the new
    draft does not show."""
    actor = request.actor_name()
    if not actor:
        raise HTTPException(
            status_code=422,
            detail="Provide 'actor' — counsel's name ('approver'/'reviewer' are accepted legacy aliases).",
        )
    if not request.outcomes:
        raise HTTPException(status_code=422, detail="Provide at least one outcome.")
    try:
        return service.record_edit_outcomes(contract_id, request.outcomes, actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/contracts/{contract_id}/reject", response_model=AgentResult, dependencies=_AUTH)
def reject(contract_id: str, request: DecisionRequest) -> AgentResult:
    try:
        return service.reject(
            contract_id, reviewer=_decision_actor(request), reason=request.reason or ""
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/contracts/{contract_id}/assign", dependencies=_AUTH)
def assign(contract_id: str, request: AssignRequest) -> dict:
    """Record queue ownership: which member of counsel's team holds
    this awaiting contract. Bookkeeping only — assignment decides
    nothing and routes nothing. Returns the updated queue item."""
    actor = (request.actor or "").strip()
    assignee = (request.assignee or "").strip()
    if not actor or not assignee:
        raise HTTPException(
            status_code=422,
            detail="Provide 'actor' (who is assigning) and 'assignee' (who holds the contract).",
        )
    try:
        return service.assign(contract_id, assignee=assignee, actor=actor)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
