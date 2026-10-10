-- 0007: metrics snapshots — the /metrics payload kept whole at a
-- moment (metrics_snapshots.py), under an operator's name, so the
-- numbers trend instead of only ever reading as of now. The prune
-- ledger readings ride inside the payload's webhooks block, so
-- retention pace trends with everything else. One row per kept
-- snapshot; the full snapshot lives in the JSONB payload,
-- mirroring the pattern_term_suspensions shape.
CREATE TABLE IF NOT EXISTS metrics_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    label       TEXT NOT NULL DEFAULT '',
    payload     JSONB NOT NULL,
    kept_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS metrics_snapshots_kept_idx
    ON metrics_snapshots (kept_at);
