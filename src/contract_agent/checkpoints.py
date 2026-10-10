"""Checkpointed approval gate: LangGraph state persistence over SQLite.

The approval gate is a real pause in the graph, not a status flag:
with a checkpointer attached, a run executes up to the gate and its
graph state persists (thread_id = the submission record id); the
service's approve/reject then *resumes* the thread with the decision,
and the graph completes. A process restart loses nothing — a new
service instance over the same database files sees the store record
AND the paused thread.

The split of responsibilities, kept deliberately:

- the **store** (``store.py``) is the record of decisions — analyses,
  approvals, reasons, dispatch outcomes; every surface reads it;
- the **checkpointer** (this module) holds graph state — where the
  run paused and what it carried. It never decides anything.

Implementation: the pinned LangGraph ships the checkpoint base +
in-memory saver only (the SQLite saver is a separate package), so
:class:`SqliteCheckpointSaver` implements that base over stdlib
``sqlite3`` — one connection per call, opened and closed per call
(thread-safe by construction, like ``SQLiteStore``), checkpoint blobs
serialised with LangGraph's own serde, channel values in a side
table exactly as the reference savers lay them out. The database
runs in WAL mode with ``synchronous=NORMAL``: checkpoint writes are
small and frequent (one per node), and the default journal mode
makes each of them an fsync — the first cut of this saver slowed a
batch run by an order of magnitude before WAL.

``CHECKPOINTS=off`` disables the whole thing: no checkpointer is
attached and the gate flow is the pre-existing service-state flow,
byte for byte.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Sequence

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)

from .config import env_str, load_dotenv

_DDL = """
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type TEXT,
    checkpoint BLOB,
    metadata_type TEXT,
    metadata BLOB,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL,
    channel TEXT NOT NULL,
    version TEXT NOT NULL,
    type TEXT,
    value BLOB,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);
CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    channel TEXT NOT NULL,
    type TEXT,
    value BLOB,
    task_path TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);
"""

_SELECT = (
    "SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
    "type, checkpoint, metadata_type, metadata FROM checkpoints"
)


class SqliteCheckpointSaver(BaseCheckpointSaver):
    """A minimal LangGraph checkpoint saver over stdlib sqlite3."""

    def __init__(self, path: Path | str) -> None:
        super().__init__()
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(_DDL)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # -- reads -----------------------------------------------------------

    def _load_channel_values(
        self, conn: sqlite3.Connection, thread_id: str, ns: str, versions: ChannelVersions
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for channel, version in versions.items():
            row = conn.execute(
                "SELECT type, value FROM checkpoint_blobs WHERE thread_id = ? "
                "AND checkpoint_ns = ? AND channel = ? AND version = ?",
                (thread_id, ns, channel, str(version)),
            ).fetchone()
            if row is not None and row["type"] != "empty":
                values[channel] = self.serde.loads_typed((row["type"], row["value"]))
        return values

    def _tuple_from_row(
        self, conn: sqlite3.Connection, row: sqlite3.Row, config: RunnableConfig
    ) -> CheckpointTuple:
        thread_id, ns = row["thread_id"], row["checkpoint_ns"]
        checkpoint: Checkpoint = self.serde.loads_typed((row["type"], row["checkpoint"]))
        checkpoint = {
            **checkpoint,
            "channel_values": self._load_channel_values(
                conn, thread_id, ns, checkpoint["channel_versions"]
            ),
        }
        write_rows = conn.execute(
            "SELECT task_id, channel, type, value FROM checkpoint_writes "
            "WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ? ORDER BY idx",
            (thread_id, ns, row["checkpoint_id"]),
        ).fetchall()
        parent_id = row["parent_checkpoint_id"]
        return CheckpointTuple(
            config=config,
            checkpoint=checkpoint,
            metadata=self.serde.loads_typed((row["metadata_type"], row["metadata"])),
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": ns,
                        "checkpoint_id": parent_id,
                    }
                }
                if parent_id
                else None
            ),
            pending_writes=[
                (r["task_id"], r["channel"], self.serde.loads_typed((r["type"], r["value"])))
                for r in write_rows
            ],
        )

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)
        conn = self._connect()
        try:
            if checkpoint_id:
                row = conn.execute(
                    _SELECT
                    + " WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?",
                    (thread_id, ns, checkpoint_id),
                ).fetchone()
            else:
                row = conn.execute(
                    _SELECT
                    + " WHERE thread_id = ? AND checkpoint_ns = ? "
                    "ORDER BY checkpoint_id DESC LIMIT 1",
                    (thread_id, ns),
                ).fetchone()
            if row is None:
                return None
            effective_config = config
            if not checkpoint_id:
                effective_config = {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": ns,
                        "checkpoint_id": row["checkpoint_id"],
                    }
                }
            return self._tuple_from_row(conn, row, effective_config)
        finally:
            conn.close()

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        thread_id = (config or {}).get("configurable", {}).get("thread_id")
        ns = (config or {}).get("configurable", {}).get("checkpoint_ns")
        before_id = get_checkpoint_id(before) if before else None
        conn = self._connect()
        try:
            query = _SELECT
            clauses, params = [], []
            if thread_id is not None:
                clauses.append("thread_id = ?")
                params.append(thread_id)
            if ns is not None:
                clauses.append("checkpoint_ns = ?")
                params.append(ns)
            if before_id:
                clauses.append("checkpoint_id < ?")
                params.append(before_id)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY checkpoint_id DESC"
            rows = conn.execute(query, params).fetchall()
            tuples = [
                self._tuple_from_row(
                    conn,
                    row,
                    {
                        "configurable": {
                            "thread_id": row["thread_id"],
                            "checkpoint_ns": row["checkpoint_ns"],
                            "checkpoint_id": row["checkpoint_id"],
                        }
                    },
                )
                for row in rows
            ]
        finally:
            conn.close()
        count = 0
        for tup in tuples:
            if limit is not None and count >= limit:
                break
            if filter and any(
                tup.metadata.get(key) != value for key, value in filter.items()
            ):
                continue
            count += 1
            yield tup

    # -- writes ----------------------------------------------------------

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"]["checkpoint_ns"]
        stored = checkpoint.copy()
        values: dict[str, Any] = stored.pop("channel_values")  # type: ignore[misc]
        checkpoint_type, checkpoint_blob = self.serde.dumps_typed(stored)
        metadata_type, metadata_blob = self.serde.dumps_typed(
            get_checkpoint_metadata(config, metadata)
        )
        conn = self._connect()
        try:
            for channel, version in new_versions.items():
                if channel in values:
                    blob_type, blob = self.serde.dumps_typed(values[channel])
                else:
                    blob_type, blob = "empty", b""
                conn.execute(
                    "INSERT OR REPLACE INTO checkpoint_blobs "
                    "(thread_id, checkpoint_ns, channel, version, type, value) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (thread_id, ns, channel, str(version), blob_type, blob),
                )
            conn.execute(
                "INSERT OR REPLACE INTO checkpoints "
                "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
                "type, checkpoint, metadata_type, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    thread_id,
                    ns,
                    checkpoint["id"],
                    config["configurable"].get("checkpoint_id"),
                    checkpoint_type,
                    checkpoint_blob,
                    metadata_type,
                    metadata_blob,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]
        conn = self._connect()
        try:
            for idx, (channel, value) in enumerate(writes):
                mapped = WRITES_IDX_MAP.get(channel, idx)
                blob_type, blob = self.serde.dumps_typed(value)
                # Special channels (negative idx: errors, interrupts,
                # resumes) overwrite; ordinary writes are first-wins,
                # mirroring the reference savers.
                verb = "INSERT OR REPLACE" if mapped < 0 else "INSERT OR IGNORE"
                conn.execute(
                    f"{verb} INTO checkpoint_writes "
                    "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, "
                    "channel, type, value, task_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (thread_id, ns, checkpoint_id, task_id, mapped, channel, blob_type, blob, task_path),
                )
            conn.commit()
        finally:
            conn.close()

    def delete_thread(self, thread_id: str) -> None:
        conn = self._connect()
        try:
            for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (thread_id,))
            conn.commit()
        finally:
            conn.close()

    # -- async delegates (the pipeline runs sync; these keep the saver
    #    usable from async LangGraph callers) -----------------------------

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        for tup in await asyncio.to_thread(
            lambda: list(self.list(config, filter=filter, before=before, limit=limit))
        ):
            yield tup

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.delete_thread, thread_id)


# ---------------------------------------------------------------------------
# Selection from configuration
# ---------------------------------------------------------------------------

def checkpoints_enabled() -> bool:
    """``CHECKPOINTS`` env (read at call time): on unless explicitly off."""
    load_dotenv()
    raw = (env_str("CHECKPOINTS") or "on").strip().lower()
    return raw not in {"off", "0", "false", "no"}


def default_checkpoint_path() -> Path:
    return Path(__file__).resolve().parents[2] / ".data" / "checkpoints.db"


def get_checkpointer() -> SqliteCheckpointSaver | None:
    """The configured checkpointer, or None when CHECKPOINTS=off.

    ``CHECKPOINT_DB_PATH`` overrides the database location (the
    default is the git-ignored ``.data/`` directory, next to the
    approval store's database).
    """
    if not checkpoints_enabled():
        return None
    load_dotenv()
    configured = env_str("CHECKPOINT_DB_PATH")
    return SqliteCheckpointSaver(configured if configured else default_checkpoint_path())
