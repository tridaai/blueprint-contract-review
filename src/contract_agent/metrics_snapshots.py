"""Metrics snapshots: /metrics, kept and trended (round 19).

``/metrics`` answers "how do things stand?" — as of now, only.
The question a service owner actually asks monthly is "how are
things MOVING?": is the queue's breach count growing, is the
outbox retiring its history at a sane pace, did the prune
ledger's totals jump because retention ran, or because nobody
ran it for a quarter and one act took everything?

A snapshot is the metrics payload kept whole under an
operator's name (``MetricsSnapshot``), and the trend is the
kept snapshots read against each other: each row carries the
headline numbers as they stood when it was kept and the
movement since the previous kept snapshot — run growth, queue
and breach movement, outbox totals, and the prune pace:
prunes since the previous snapshot and entries retired since
the previous snapshot, differences of the ledger's own totals,
so the pace is measured, never estimated. The first kept
snapshot has no predecessor: its movements are None, not
zero — nothing moved is a different claim from nothing to
compare against, and the trend does not invent the comparison.

Keeping a snapshot is an act with a name on it, like a prune:
the actor is required, and a label — when given — must be
unique among the kept snapshots, so "the October baseline"
always names one moment.
"""

from __future__ import annotations

import re

from .schemas import MetricsSnapshot

_SNAPSHOT_ID = re.compile(r"^SNAP-(\d+)$")


def next_snapshot_id(existing: list[MetricsSnapshot]) -> str:
    """The next snapshot id in the kept sequence (``SNAP-0001``,
    ``SNAP-0002``, …): one past the highest number in use, the
    prune ledger's PROP-0001 idiom — ids never reuse, so a
    snapshot named in a report names one moment forever."""
    highest = 0
    for snapshot in existing:
        match = _SNAPSHOT_ID.match(snapshot.snapshot_id or "")
        if match:
            highest = max(highest, int(match.group(1)))
    return f"SNAP-{highest + 1:04d}"


def prune_readings(snapshot: MetricsSnapshot) -> dict:
    """The prune ledger readings inside a snapshot's payload —
    the whole payload is kept, so the readings ride along; {}
    for a payload that carries none."""
    webhooks = snapshot.metrics.get("webhooks") or {}
    return webhooks.get("prunes") or {}


def _delta(current, previous):
    """The movement between two readings of the same counter:
    None when either side is missing (nothing invented)."""
    if current is None or previous is None:
        return None
    return current - previous


def snapshot_trend(snapshots: list[MetricsSnapshot]) -> dict:
    """The kept snapshots as a trend, in kept order: each row
    the headline numbers as kept, plus the movement since the
    previous kept snapshot — including the prune pace, read off
    the prune ledger readings each payload carries."""
    rows: list[dict] = []
    previous: MetricsSnapshot | None = None
    for snapshot in snapshots:
        metrics = snapshot.metrics
        queue = metrics.get("queue_sla") or {}
        webhooks = metrics.get("webhooks") or {}
        prunes = prune_readings(snapshot)
        prev_metrics = previous.metrics if previous else {}
        prev_queue = prev_metrics.get("queue_sla") or {}
        prev_webhooks = prev_metrics.get("webhooks") or {}
        prev_prunes = prune_readings(previous) if previous else {}
        rows.append(
            {
                "snapshot_id": snapshot.snapshot_id,
                "label": snapshot.label,
                "actor": snapshot.actor,
                "kept_at": snapshot.kept_at,
                "runs_total": metrics.get("runs_total"),
                "runs_since_previous": _delta(
                    metrics.get("runs_total"), prev_metrics.get("runs_total")
                ),
                "awaiting": queue.get("awaiting"),
                "sla_breached": queue.get("sla_breached"),
                "sla_breached_change": _delta(
                    queue.get("sla_breached"), prev_queue.get("sla_breached")
                ),
                "outbox_total": webhooks.get("total"),
                "outbox_failed": webhooks.get("failed"),
                "outbox_stale_failed": webhooks.get("stale_failed"),
                "total_prunes": prunes.get("total_prunes"),
                "entries_removed": prunes.get("entries_removed"),
                "prunes_since_previous": _delta(
                    prunes.get("total_prunes"), prev_prunes.get("total_prunes")
                ),
                "entries_retired_since_previous": _delta(
                    prunes.get("entries_removed"),
                    prev_prunes.get("entries_removed"),
                ),
                "last_prune": prunes.get("last_prune"),
            }
        )
        previous = snapshot
    return {"count": len(rows), "snapshots": rows}
