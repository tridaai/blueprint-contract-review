-- 0005: webhook outbox — the one delivery ledger for every
-- signed dispatch (webhook_outbox.py): approved packets, queue
-- SLA breaches, drift findings. One row per dispatch; the full
-- entry (the exact payload posted, the attempt history) lives
-- in the JSONB payload, mirroring the playbook_proposals shape.
CREATE TABLE IF NOT EXISTS webhook_outbox (
    outbox_id   TEXT PRIMARY KEY,
    family      TEXT NOT NULL DEFAULT '',
    contract_id TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'failed',
    payload     JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS webhook_outbox_status_idx
    ON webhook_outbox (status);

CREATE INDEX IF NOT EXISTS webhook_outbox_family_idx
    ON webhook_outbox (family);
