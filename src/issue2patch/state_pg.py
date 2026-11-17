"""StateStore on Postgres (stage 2, decision 0002 said this swap must not touch the orchestrator).

Task and node *state* (status, current node, per-node status, events, summary) lives in Postgres, so it
is shared by every worker and survives a sandbox or worker restart. Node *artifacts* (contract
input/output JSON, logs, agent traces, worktree checkpoints) stay on disk under runs/<run_id>/, the
way an object store would hold them; resume reads checkpoints from there. Status is authoritative in
the database: a worker that picks up a task asks Postgres, never the queue, what state it is in.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .state import NODE_ORDER, FileStateStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  user_id      text PRIMARY KEY,
  token_budget bigint NOT NULL,
  created_at   double precision NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  key              text PRIMARY KEY,          -- run_id/task_id
  run_id           text NOT NULL,
  task_id          text NOT NULL,
  status           text NOT NULL,             -- queued, running, done, failed, infra_error, timeout,
                                              -- budget_exhausted, cancelled, partial
  current_node     text,
  user_id          text,
  priority         int NOT NULL DEFAULT 0,
  cancel_requested boolean NOT NULL DEFAULT false,
  spec             jsonb,                     -- where the task came from (issue, repo, environment)
  meta             jsonb NOT NULL DEFAULT '{}',
  nodes            jsonb NOT NULL DEFAULT '{}',
  summary          jsonb,
  tokens           bigint NOT NULL DEFAULT 0,
  pr_url           text,
  attempts         int NOT NULL DEFAULT 0,    -- how many times a worker started this task
  created_at       double precision,
  updated_at       double precision,
  finished_at      double precision
);
CREATE INDEX IF NOT EXISTS tasks_user ON tasks(user_id);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
CREATE TABLE IF NOT EXISTS events (
  id    bigserial PRIMARY KEY,
  key   text NOT NULL,
  t     double precision NOT NULL,
  kind  text NOT NULL,
  data  jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_key ON events(key);
"""


def connect(dsn: str, patience_s: float = 180) -> psycopg.Connection:
    """Connect, riding out a short database outage (restart, failover) instead of failing the task.
    Without this, a 30-second Postgres restart would make every worker fail and nack its message, and
    each redelivery would fail again immediately, burning the queue's delivery limit (3)."""
    deadline, delay = time.time() + patience_s, 1.0
    while True:
        try:
            return psycopg.connect(dsn, autocommit=True, connect_timeout=10)
        except psycopg.OperationalError:
            if time.time() + delay > deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 15)


def init_schema(dsn: str) -> None:
    with connect(dsn) as c:
        c.execute(SCHEMA)


class PostgresStateStore(FileStateStore):
    def __init__(self, run_dir: Path, dsn: str):
        super().__init__(run_dir)
        self.dsn = dsn
        self.run_id = Path(run_dir).name

    def key(self, task_id: str) -> str:
        return f"{self.run_id}/{task_id}"

    # state -------------------------------------------------------------------
    def load(self, task_id: str) -> dict[str, Any]:
        with connect(self.dsn) as c:
            row = c.execute("SELECT status, current_node, meta, nodes, summary, created_at, updated_at, "
                            "finished_at FROM tasks WHERE key = %s", (self.key(task_id),)).fetchone()
        if not row:
            return {}
        status, cur, meta, nodes, summary, created, updated, finished = row
        return {"task_id": task_id, "status": status, "current_node": cur, "meta": meta or {},
                "nodes": nodes or {}, "summary": summary, "created_at": created, "updated_at": updated,
                "finished_at": finished}

    def _save(self, task_id: str, st: dict[str, Any]) -> None:
        st["updated_at"] = time.time()
        tokens = (st.get("summary") or {}).get("tokens") or 0
        with connect(self.dsn) as c:
            c.execute(
                """INSERT INTO tasks (key, run_id, task_id, status, current_node, meta, nodes, summary, tokens,
                                      created_at, updated_at, finished_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (key) DO UPDATE SET status = EXCLUDED.status, current_node = EXCLUDED.current_node,
                     meta = tasks.meta || EXCLUDED.meta, nodes = EXCLUDED.nodes, summary = EXCLUDED.summary,
                     tokens = GREATEST(tasks.tokens, EXCLUDED.tokens),
                     created_at = COALESCE(tasks.created_at, EXCLUDED.created_at),
                     updated_at = EXCLUDED.updated_at, finished_at = EXCLUDED.finished_at""",
                (self.key(task_id), self.run_id, task_id, st.get("status"), st.get("current_node"),
                 Jsonb(st.get("meta") or {}), Jsonb(st.get("nodes") or {}), Jsonb(st.get("summary")), tokens,
                 st.get("created_at"), st["updated_at"], st.get("finished_at")))

    def event(self, task_id: str, kind: str, **data: Any) -> None:
        with connect(self.dsn) as c:
            c.execute("INSERT INTO events (key, t, kind, data) VALUES (%s,%s,%s,%s)",
                      (self.key(task_id), time.time(), kind, Jsonb(json.loads(json.dumps(data, default=str)))))
        super().event(task_id, kind, **data)  # the per-task events.jsonl artifact stays, for local reading

    # service-only ------------------------------------------------------------
    def cancel_requested(self, task_id: str) -> bool:
        with connect(self.dsn) as c:
            row = c.execute("SELECT cancel_requested FROM tasks WHERE key = %s", (self.key(task_id),)).fetchone()
        return bool(row and row[0])

    def mark_started(self, task_id: str) -> int:
        with connect(self.dsn) as c:
            row = c.execute("UPDATE tasks SET attempts = attempts + 1 WHERE key = %s RETURNING attempts",
                            (self.key(task_id),)).fetchone()
        return row[0] if row else 0

    def fresh_state(self, task_id: str) -> dict[str, Any]:
        """Turn a queued row into the initial running state without losing what the API stored."""
        return {"task_id": task_id, "status": "running", "current_node": None,
                "nodes": {n: {"status": "pending"} for n in NODE_ORDER}, "created_at": time.time()}
