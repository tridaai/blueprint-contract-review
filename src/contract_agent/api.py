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
from .schemas import AgentResult, AssignRequest, ContractInput, DecisionRequest
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


@app.get("/queue", dependencies=_AUTH)
def counsel_queue() -> dict:
    """The counsel worklist: contracts awaiting a decision — SLA
    breaches first, then worst-first (posture severity, worst clause
    position, age) — each with its autonomy flag, queue owner, and
    SLA state (budget by severity tier, business days waited)."""
    items = service.queue()
    return {"count": len(items), "queue": items}


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


@app.post("/contracts/{contract_id}/approve", response_model=AgentResult, dependencies=_AUTH)
def approve(contract_id: str, request: DecisionRequest) -> AgentResult:
    try:
        return service.approve(
            contract_id, approver=_decision_actor(request), reason=request.reason or ""
        )
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
