"""Checkpointers: state snapshots at superstep boundaries.

A checkpoint holds everything needed to resume: the merged state, the set
of nodes that are about to run, and the pending interrupt (if any). The
SQLite flavor is the stand-in for LangGraph's Postgres saver -- swap the
backend, keep the semantics.

Not implemented (on purpose, documented in README): LangGraph 1.2's
DeltaChannel delta-compression. We always store the full state, which is
fine at demo scale and honest about the tradeoff.
"""

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Checkpoint:
    thread_id: str
    step: int
    state: Dict[str, Any]
    next_nodes: List[str]
    interrupt: Optional[Dict[str, Any]] = None   # {"node": ..., "payload": ...}
    created_at: float = field(default_factory=time.time)

    def to_json(self):
        return {
            "thread_id": self.thread_id, "step": self.step,
            "state": self.state, "next_nodes": self.next_nodes,
            "interrupt": self.interrupt, "created_at": self.created_at,
        }

    @classmethod
    def from_json(cls, raw):
        return cls(**raw)


class BaseCheckpointer:
    def put(self, checkpoint: Checkpoint): ...
    def get_latest(self, thread_id: str) -> Optional[Checkpoint]: ...
    def delete(self, thread_id: str): ...


class MemoryCheckpointer(BaseCheckpointer):
    def __init__(self):
        self._store: Dict[str, List[Checkpoint]] = {}

    def put(self, checkpoint):
        self._store.setdefault(checkpoint.thread_id, []).append(checkpoint)

    def get_latest(self, thread_id):
        rows = self._store.get(thread_id) or []
        return rows[-1] if rows else None

    def delete(self, thread_id):
        self._store.pop(thread_id, None)


class SqliteCheckpointer(BaseCheckpointer):
    """File-backed saver. One row per superstep -- the audit trail of what
    the graph believed at every boundary, which is exactly what you want
    when debugging 'why did it loop'."""

    def __init__(self, path="checkpoints.sqlite"):
        self.path = path
        fresh = not os.path.exists(path)
        self._db = sqlite3.connect(path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS checkpoints ("
            " thread_id TEXT NOT NULL,"
            " step INTEGER NOT NULL,"
            " payload TEXT NOT NULL,"
            " created_at REAL NOT NULL,"
            " PRIMARY KEY (thread_id, step))"
        )
        self._db.commit()

    def put(self, checkpoint):
        self._db.execute(
            "INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?)",
            (checkpoint.thread_id, checkpoint.step,
             json.dumps(checkpoint.to_json(), ensure_ascii=False, sort_keys=True),
             checkpoint.created_at),
        )
        self._db.commit()

    def get_latest(self, thread_id):
        row = self._db.execute(
            "SELECT payload FROM checkpoints WHERE thread_id=? "
            "ORDER BY step DESC LIMIT 1", (thread_id,),
        ).fetchone()
        return Checkpoint.from_json(json.loads(row[0])) if row else None

    def list_steps(self, thread_id):
        return [json.loads(r[0]) for r in self._db.execute(
            "SELECT payload FROM checkpoints WHERE thread_id=? ORDER BY step",
            (thread_id,),)]

    def delete(self, thread_id):
        self._db.execute("DELETE FROM checkpoints WHERE thread_id=?", (thread_id,))
        self._db.commit()

    def close(self):
        self._db.close()
