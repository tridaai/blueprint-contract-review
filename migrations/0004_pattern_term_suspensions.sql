-- 0004: pattern-term suspensions — counsel's act on a calibration
-- candidate (pattern_calibration.py): the reflection-pattern score
-- term for one clause type, set aside with actor + reason, and
-- reversible by a recorded lift. One row per clause type; the full
-- record (both acts' stamps) lives in the JSONB payload, mirroring
-- the playbook_proposals shape.
CREATE TABLE IF NOT EXISTS pattern_term_suspensions (
    clause_type TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'active',
    payload     JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pattern_term_suspensions_status_idx
    ON pattern_term_suspensions (status);
