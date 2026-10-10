"""Run events: the structured stream a run emits as it happens.

The graph emits events through an :class:`~contract_agent.ports.EventSink`
(see ``ports.py``) — the pipeline itself never prints or serialises;
each surface attaches the sink it needs:

- the CLI (``--stream``) attaches :class:`PrintingSink` and shows the
  run live;
- the API's ``POST /contracts/analyze/stream`` attaches a queue-backed
  sink and forwards the events as Server-Sent Events;
- tests attach :class:`CollectingSink` and assert on the sequence.

Event types: ``run_started``, ``node_started``, ``node_finished``
(carries ``duration_ms``), ``tool_called``, ``guardrail_verdict``,
``repair_attempted``, ``run_completed`` (carries the final status),
``run_failed`` (carries the clean error message). Events fire in the
offline fallback exactly as in provider mode — durations are real
wall-clock measurements either way.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Callable, TextIO


@dataclass
class RunEvent:
    """One structured run event (see the module docstring for types)."""

    type: str
    contract_id: str = ""
    node: str | None = None  # pipeline node, for node_* / tool events
    duration_ms: float | None = None  # node_finished only
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "contract_id": self.contract_id,
            "node": self.node,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
        }


class NullSink:
    """The default sink: events go nowhere (non-streaming runs)."""

    def emit(self, event: RunEvent) -> None:
        pass


class CollectingSink:
    """Keeps every event in a list — tests and in-process consumers."""

    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    def emit(self, event: RunEvent) -> None:
        self.events.append(event)


class CallbackSink:
    """Forwards each event to a callable — the API's queue bridge."""

    def __init__(self, callback: Callable[[RunEvent], None]) -> None:
        self._callback = callback

    def emit(self, event: RunEvent) -> None:
        self._callback(event)


class PrintingSink:
    """Human-readable one-line events, printed as they happen (CLI)."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def emit(self, event: RunEvent) -> None:
        print(self._format(event), file=self._stream, flush=True)

    @staticmethod
    def _format(event: RunEvent) -> str:
        parts = [f"[event] {event.type}"]
        if event.node:
            parts.append(f"node={event.node}")
        if event.duration_ms is not None:
            parts.append(f"{event.duration_ms:.2f} ms")
        if event.type == "run_started":
            parts.append(f"contract={event.contract_id}")
        elif event.type == "tool_called":
            parts.append(f"tool={event.detail.get('tool', '')}")
        elif event.type == "guardrail_verdict":
            parts.append(f"passed={event.detail.get('passed')}")
        elif event.type == "repair_attempted":
            parts.append(
                f"attempts={event.detail.get('attempts')} "
                f"repaired={event.detail.get('repaired')}"
            )
        elif event.type == "run_completed":
            parts.append(f"status={event.detail.get('status')}")
        elif event.type == "run_failed":
            parts.append(f"error={event.detail.get('error', '')}")
        return " · ".join(parts)
