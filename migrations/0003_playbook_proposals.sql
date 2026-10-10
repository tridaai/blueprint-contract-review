-- 0003: playbook proposals — candidate playbook entries drafted from
-- coverage gaps (playbook_proposals.py), stored PROPOSED until counsel
-- activates or dismisses them in the console. One row per proposal;
-- the full proposal (draft entries, evidence, decision stamps) lives
-- in the JSONB payload, mirroring the contract_records shape.
CREATE TABLE IF NOT EXISTS playbook_proposals (
    proposal_id TEXT PRIMARY KEY,
    clause_type TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'proposed',
    payload     JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS playbook_proposals_status_idx
    ON playbook_proposals (status);
