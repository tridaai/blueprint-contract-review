-- 0006: webhook outbox prune ledger — one row per prune act
-- (webhook_outbox.py), so the act that removes delivery history
-- itself leaves a record: who pruned, when, under what retention
-- age, and the exact entry ids removed. The full entry lives in
-- the JSONB payload, mirroring the webhook_outbox shape.
CREATE TABLE IF NOT EXISTS webhook_outbox_prunes (
    prune_id   TEXT PRIMARY KEY,
    actor      TEXT NOT NULL DEFAULT '',
    payload    JSONB NOT NULL,
    pruned_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS webhook_outbox_prunes_at_idx
    ON webhook_outbox_prunes (pruned_at);
