"""State persistence behind one interface (decision 0002).

Stage 0/1 write files; stage 2 swaps in a Postgres implementation of the same
protocol without touching the orchestrator or the nodes.

Layout of one run:
    runs/<run_id>/run.json                       settings + task list
    runs/<run_id>/tasks/<task_id>/state.json     task state machine (atomic rewrite)
    runs/<run_id>/tasks/<task_id>/events.jsonl   append-only event log
    runs/<run_id>/tasks/<task_id>/model_calls.jsonl  one line per model request (gateway)
    runs/<run_id>/tasks/<task_id>/nodes/<n>-<name>/{input,output,check,metrics}.json + agent/ + logs
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Protocol

NODE_ORDER = [
    "prepare",
    "prefetch",
    "reproduce",
    "implement",
    "validate",
    "repair",
    "review",
    "emit_patch",
]

TERMINAL = {"done", "failed", "infra_error", "timeout", "budget_exhausted", "partial", "cancelled"}


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class StateStore(Protocol):
    def init_task(self, task_id: str, meta: dict[str, Any]) -> None: ...
    def node_start(self, task_id: str, node: str, inp: dict[str, Any]) -> None: ...
    def node_finish(
        self,
        task_id: str,
        node: str,
        status: str,
        output: dict[str, Any] | None,
        check: dict[str, Any] | None,
        metrics: dict[str, Any],
        error: str | None = None,
    ) -> None: ...
    def task_finish(self, task_id: str, status: str, summary: dict[str, Any]) -> None: ...
    def event(self, task_id: str, kind: str, **data: Any) -> None: ...
    def load(self, task_id: str) -> dict[str, Any]: ...
    def task_dir(self, task_id: str) -> Path: ...
    def node_dir(self, task_id: str, node: str) -> Path: ...


class FileStateStore:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # paths -----------------------------------------------------------------
    def task_dir(self, task_id: str) -> Path:
        d = self.run_dir / "tasks" / task_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def node_dir(self, task_id: str, node: str) -> Path:
        d = self.task_dir(task_id) / "nodes" / f"{NODE_ORDER.index(node) + 1}-{node}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _state_path(self, task_id: str) -> Path:
        return self.task_dir(task_id) / "state.json"

    # state -----------------------------------------------------------------
    def load(self, task_id: str) -> dict[str, Any]:
        p = self._state_path(task_id)
        return json.loads(p.read_text()) if p.exists() else {}

    def _save(self, task_id: str, st: dict[str, Any]) -> None:
        st["updated_at"] = time.time()
        atomic_write(self._state_path(task_id), _dump(st))

    def init_task(self, task_id: str, meta: dict[str, Any]) -> None:
        st = {
            "task_id": task_id,
            "status": "running",
            "current_node": None,
            "meta": meta,
            "nodes": {n: {"status": "pending"} for n in NODE_ORDER},
            "created_at": time.time(),
        }
        self._save(task_id, st)
        self.event(task_id, "task_started")

    def node_start(self, task_id: str, node: str, inp: dict[str, Any]) -> None:
        atomic_write(self.node_dir(task_id, node) / "input.json", _dump(inp))
        st = self.load(task_id)
        st["current_node"] = node
        st["nodes"][node] = {"status": "running", "started_at": time.time()}
        self._save(task_id, st)
        self.event(task_id, "node_started", node=node)

    def node_finish(self, task_id, node, status, output, check, metrics, error=None) -> None:
        d = self.node_dir(task_id, node)
        if output is not None:
            atomic_write(d / "output.json", _dump(output))
        if check is not None:
            atomic_write(d / "check.json", _dump(check))
        atomic_write(d / "metrics.json", _dump(metrics))
        if error:
            (d / "error.txt").write_text(error)
        st = self.load(task_id)
        entry = st["nodes"].get(node, {})
        entry.update(status=status, finished_at=time.time(), check_passed=(check or {}).get("passed"))
        st["nodes"][node] = entry
        self._save(task_id, st)
        self.event(task_id, "node_finished", node=node, status=status, check_passed=entry["check_passed"])

    def task_finish(self, task_id: str, status: str, summary: dict[str, Any]) -> None:
        st = self.load(task_id)
        st.update(status=status, current_node=None, summary=summary, finished_at=time.time())
        self._save(task_id, st)
        self.event(task_id, "task_finished", status=status)

    def event(self, task_id: str, kind: str, **data: Any) -> None:
        line = json.dumps({"t": time.time(), "kind": kind, **data}, ensure_ascii=False, default=str)
        with self._lock, open(self.task_dir(task_id) / "events.jsonl", "a") as f:
            f.write(line + "\n")
