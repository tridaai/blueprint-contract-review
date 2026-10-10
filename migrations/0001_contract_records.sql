-- 0001: the system of record for review outcomes and counsel decisions.
-- One row per analysed contract; the full AgentResult + source contract
-- payload live in the JSONB document (the same shape the in-memory test
-- double holds), so the store stays a thin, auditable projection.
CREATE TABLE IF NOT EXISTS contract_records (
    contract_id TEXT PRIMARY KEY,
    payload     JSONB NOT NULL,
    decided     BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS contract_records_decided_idx
    ON contract_records (decided);
