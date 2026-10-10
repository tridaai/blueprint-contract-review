"""Graph-state checkpoints behind the counsel gate.

The pipeline pauses at the counsel gate with LangGraph's ``interrupt()``;
the run's graph state persists so a decision hours later resumes the
exact run (thread_id = the contract id). Production persistence is
**PostgreSQL**: the LangGraph ecosystem's ``PostgresSaver``
(``langgraph-checkpoint-postgres``) over the same database as the
record store. The saver manages its own tables through its ``setup()``,
called once here at construction; the application's tables are owned
by ``migrations/``.

Selection (``get_checkpointer``):

- ``CHECKPOINTS=off`` → no checkpointer; the service then refuses to
  park runs at the gate (a decision must come in the same call).
- ``DATABASE_URL`` set → ``PostgresSaver`` over a psycopg connection
  pool — the production default.
- otherwise → LangGraph's in-memory saver: the offline demo and the
  hermetic tests only. It is a test double, never the documented
  runtime.
"""

from __future__ import annotations

from .config import env_str, load_dotenv


def get_checkpointer():
    """Build the checkpointer for this process (see module docstring)."""
    load_dotenv()
    if env_str("CHECKPOINTS", "on").strip().lower() in {"off", "0", "false", "no"}:
        return None
    url = env_str("DATABASE_URL", "")
    if url:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(url, max_size=4, kwargs={"autocommit": True})
        saver = PostgresSaver(pool)
        saver.setup()
        return saver
    from langgraph.checkpoint.memory import MemorySaver

    return MemorySaver()
