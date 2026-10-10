"""Node resilience policy: timeouts and retries for provider calls.

One central policy table for the pipeline's model work, applied by
wrapping the backend (the same wrapper pattern as the token budget's
``_BudgetGuard`` — the graph wraps budget first, resilience outside
it, so every retry attempt re-checks the budget):

- **Timeout.** Every provider call runs with a per-node timeout
  (``NODE_TIMEOUT_SECONDS``, default 180 — generous, because the SDK
  already has its own ``LLM_TIMEOUT_SECONDS``; this is the ceiling
  that keeps a wedged endpoint from wedging the run). A timed-out
  call raises :class:`~contract_agent.errors.ProviderError` in the
  product's clean style. Deterministic node work is pure in-process
  code and needs no timeout — the provider call is the only part of
  a node that can hang, so it is the part the policy governs.
- **Retry.** Only the idempotent language steps retry, and only on
  ``ProviderError``: extraction, the classification cross-check,
  diagnosis, options, verification, review. Max 2 attempts (the
  initial try + one retry) with small backoff. Retries are recorded
  on the wrapper's ``retry_log`` and land in the trace
  ("attempt 2 after provider error"). Drafting never retries — a
  redraft is a guardrail decision, not a transport retry — and
  nothing at or past the gate retries.

A timed-out call's worker thread cannot be killed (Python threads);
it is abandoned and finishes on its own, bounded by the SDK timeout.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass

from .config import env_int, load_dotenv
from .errors import ProviderError

# Provider capability method -> the pipeline node it serves (mirrors
# graph._PROVIDER_STEP_NODES; the two tables answer different
# questions — budget degradation vs resilience — about the same map).
_METHOD_NODES = {
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

# The idempotent language steps: re-asking costs tokens, changes
# nothing else. Drafting and the gate are deliberately absent.
_RETRYABLE_NODES = {"extract", "classify", "assess", "options", "verify", "review"}

DEFAULT_NODE_TIMEOUT_SECONDS = 180
MAX_ATTEMPTS = 2  # initial try + one retry
BACKOFF_SECONDS = 0.25


@dataclass(frozen=True)
class NodePolicy:
    max_attempts: int
    timeout_seconds: float


def node_timeout_seconds() -> float:
    """``NODE_TIMEOUT_SECONDS`` (env, read at call time)."""
    load_dotenv()
    value = env_int("NODE_TIMEOUT_SECONDS", DEFAULT_NODE_TIMEOUT_SECONDS)
    return float(value) if value > 0 else float(DEFAULT_NODE_TIMEOUT_SECONDS)


def policy_for(method: str, timeout_seconds: float) -> NodePolicy:
    retryable = _METHOD_NODES[method] in _RETRYABLE_NODES
    return NodePolicy(
        max_attempts=MAX_ATTEMPTS if retryable else 1,
        timeout_seconds=timeout_seconds,
    )


class ResilientBackend:
    """Backend wrapper enforcing the resilience policy per provider call.

    Non-provider attributes (``name``, ``usage_totals``, …) pass
    through untouched, as do methods outside the policy table.
    ``retry_log`` accumulates one entry per retry —
    ``{"node", "method", "attempt", "error"}`` — for the trace.
    """

    def __init__(self, backend, timeout_seconds: float | None = None) -> None:
        object.__setattr__(self, "_backend", backend)
        object.__setattr__(
            self,
            "_timeout_seconds",
            timeout_seconds if timeout_seconds is not None else node_timeout_seconds(),
        )
        object.__setattr__(self, "retry_log", [])

    def __getattr__(self, name: str):
        backend = object.__getattribute__(self, "_backend")
        attr = getattr(backend, name)  # AttributeError propagates — getattr defaults work
        if name not in _METHOD_NODES or not callable(attr):
            return attr
        policy = policy_for(name, object.__getattribute__(self, "_timeout_seconds"))

        def guarded(*args, **kwargs):
            return self._call_with_policy(name, policy, attr, args, kwargs)

        return guarded

    def _call_with_policy(self, method, policy: NodePolicy, fn, args, kwargs):
        attempt = 1
        while True:
            try:
                return self._call_timed(method, policy, fn, args, kwargs)
            except ProviderError as exc:
                if attempt >= policy.max_attempts:
                    raise
                attempt += 1
                self.retry_log.append(
                    {
                        "node": _METHOD_NODES[method],
                        "method": method,
                        "attempt": attempt,
                        "error": str(exc),
                    }
                )
                time.sleep(BACKOFF_SECONDS * (attempt - 1))

    def _call_timed(self, method, policy: NodePolicy, fn, args, kwargs):
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(fn, *args, **kwargs)
            return future.result(timeout=policy.timeout_seconds)
        except FuturesTimeoutError:
            backend_name = getattr(
                object.__getattribute__(self, "_backend"), "name", "provider"
            )
            raise ProviderError(
                f"{backend_name} provider call failed (timeout): the {method} "
                f"call did not answer within NODE_TIMEOUT_SECONDS="
                f"{policy.timeout_seconds:g}s. Likely fix: raise "
                "NODE_TIMEOUT_SECONDS, or check the endpoint's health."
            ) from None
        finally:
            executor.shutdown(wait=False)
