"""Schema migrations: numbered SQL files, applied by a tiny runner.

No ORM, no framework: ``migrations/`` at the repo root holds
``NNNN_name.sql`` files; this runner applies the ones the database has
not seen yet, in filename order, each in its own transaction, and
records them in ``schema_migrations``. It runs at startup (the
Postgres store and the pgvector retriever call it before first use)
and can also be run by hand:

    DATABASE_URL=postgresql://... python -m contract_agent.migrations

The LangGraph Postgres checkpointer manages its OWN tables through
the saver's ``setup()`` (see ``checkpoints.py``); these migrations own
only the application's tables (records, playbook embeddings).
"""

from __future__ import annotations

import os
from pathlib import Path

_TRACKING_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def migrations_dir() -> Path:
    """Where the SQL files live: ``MIGRATIONS_DIR`` when set, else the
    repo-root ``migrations/`` next to ``src/`` (the Docker image copies
    it alongside the package and sets the variable)."""
    override = os.environ.get("MIGRATIONS_DIR", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "migrations"


def pending_files(directory: Path | None = None) -> list[Path]:
    directory = directory or migrations_dir()
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.sql") if p.name[:4].isdigit())


def run_migrations(conn, directory: Path | None = None) -> list[str]:
    """Apply every unapplied migration on an open psycopg connection.

    Returns the versions applied by this call. Idempotent: a second
    run applies nothing.
    """
    applied: list[str] = []
    with conn.cursor() as cur:
        cur.execute(_TRACKING_DDL)
        cur.execute("SELECT version FROM schema_migrations")
        seen = {row[0] for row in cur.fetchall()}
    conn.commit()
    for path in pending_files(directory):
        version = path.name
        if version in seen:
            continue
        sql = path.read_text(encoding="utf-8")
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s)", (version,)
            )
        conn.commit()
        applied.append(version)
    return applied


def main() -> None:
    import psycopg

    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("DATABASE_URL is not set — nothing to migrate.")
    with psycopg.connect(url) as conn:
        applied = run_migrations(conn)
    print(f"migrations applied: {applied or 'none (already up to date)'}")


if __name__ == "__main__":
    main()
