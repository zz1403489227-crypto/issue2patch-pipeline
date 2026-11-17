"""Orchestrator: a deterministic state machine over the eight nodes.

Reliability (stage 1, decision 0004):
- Retry classes. (1) infrastructure errors (Docker, container gone, model endpoint unreachable): the
  same node again, after putting the worktree back to the node's starting snapshot, with backoff;
  (2) model output format errors: handled inside the agent session (tool-argument validation errors and
  a missing `submit` go back to the model); (3) failed validation: the repair node.
- Timeouts, three layers: one model call (gateway), one node (agent/test limits), the whole task
  (deadline; agent nodes get min(node limit, time left)).
- Budget: tokens per task (gateway). When time or tokens run out, agent work stops and the task is
  wrapped up: the patch is still emitted and the reason recorded.
- Resume: after every node the worktree is saved as a binary patch next to the node's outputs. A resumed
  task gets a fresh sandbox, re-applies the last saved worktree and continues with the first node that
  did not finish; an agent node that was interrupted runs again from its start.
- State goes through the StateStore interface only.
"""
from __future__ import annotations

import hashlib
import json
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .config import Settings
from .contracts import (ImplementOut, PatchOut, PrefetchOut, RepairOut, ReproOut, ReviewOut, SandboxInfo,
                        TaskSpec, ValidateOut)
from .gateway import ModelGateway
from .harness import make_harness
from .nodes import PIPELINE, prepare
from .nodes.context import StopTask, TaskContext
from .sandbox import InfraError
from .state import NODE_ORDER, TERMINAL, FileStateStore, StateStore
from .tasks import sanitized, to_task
from .testing import RepoTestInfo

STOP_ON_FAIL = {"prepare"}  # nothing downstream can run without a sandbox
WRAP_UP = ["emit_patch"]  # deterministic nodes still run after a time/token stop
OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    "prepare": SandboxInfo, "prefetch": PrefetchOut, "reproduce": ReproOut, "implement": ImplementOut,
    "validate": ValidateOut, "repair": RepairOut, "review": ReviewOut, "emit_patch": PatchOut,
}
DEPS = {
    "prepare": [], "prefetch": ["prepare"], "reproduce": ["prefetch"], "implement": ["prefetch", "reproduce"],
    "validate": ["reproduce", "implement"], "repair": ["validate", "reproduce"], "review": ["implement"],
    "emit_patch": ["reproduce"],
}
CACHE_SKIP = {"py_files"}  # recomputable and large


def node_input(ctx: TaskContext, node: str) -> dict[str, Any]:
    """The contract fields a node reads, recorded for single-node re-runs."""
    base: dict[str, Any] = {"task": ctx.task.model_dump()}
    base.update({d: (ctx.outputs[d].model_dump() if d in ctx.outputs else None) for d in DEPS[node]})
    return base


def _save_checkpoint(ctx: TaskContext, node: str) -> None:
    d = ctx.node_dir(node)
    (d / "worktree.patch").write_text(ctx.sb.worktree_patch())
    (d / "cache.json").write_text(json.dumps({k: v for k, v in ctx.cache.items() if k not in CACHE_SKIP},
                                             default=str))


def _fresh_sandbox(ctx: TaskContext, last_done: str | None) -> None:
    """New container at the base commit, then the worktree as it was after `last_done`."""
    if ctx.sandbox is not None:
        ctx.sandbox.destroy()
        ctx.sandbox = None
    prepare.run(ctx)
    if last_done and last_done != "prepare":
        patch = ctx.node_dir(last_done) / "worktree.patch"
        if patch.exists():
            ctx.sb.apply_worktree_patch(patch.read_text())
    if "py_files" not in ctx.cache:
        ctx.cache["py_files"] = [f for f in ctx.sb.git("ls-files").output.splitlines() if f.endswith(".py")]


def _resume_point(ctx: TaskContext) -> list[str]:
    """Nodes already done (a prefix of NODE_ORDER); their outputs and cache are loaded into ctx."""
    st = ctx.store.load(ctx.task.task_id)
    done: list[str] = []
    for n in NODE_ORDER:
        if (st.get("nodes", {}).get(n) or {}).get("status") != "done":
            break
        out = ctx.node_dir(n) / "output.json"
        if not out.exists():
            break
        ctx.outputs[n] = OUTPUT_MODELS[n].model_validate_json(out.read_text())
        done.append(n)
    if done:
        cache = ctx.node_dir(done[-1]) / "cache.json"
        if cache.exists():
            ctx.cache.update(json.loads(cache.read_text()))
    return done


def _used_tokens(log: Path) -> int:
    if not log.exists():
        return 0
    total = 0
    for line in log.read_text().splitlines():
        u = json.loads(line).get("usage") or {}
        total += (u.get("prompt_tokens") or 0) + (u.get("completion_tokens") or 0)
    return total


def _run_node(ctx: TaskContext, name: str, fn, last_done: str | None) -> tuple[str, Any, dict, str | None]:
    """Run one node with retry class 1. Returns (status, output, check, error)."""
    s, tid = ctx.settings, ctx.task.task_id
    for attempt in range(1, s.node_max_attempts + 1):
        snap = None
        if ctx.sandbox is not None and name != "prepare":
            snap = ctx.sb.snapshot()  # idempotency: a retry starts from exactly this worktree
        try:
            out, check = fn(ctx)
            return "done", out, check, None
        except StopTask:
            raise
        except InfraError as e:
            ctx.store.event(tid, "node_retry", node=name, attempt=attempt, retry_class="infra", error=str(e)[:500])
            if attempt == s.node_max_attempts:
                return "error", None, None, traceback.format_exc()
            time.sleep(s.retry_backoff_s * attempt)
            try:
                if name == "prepare":
                    pass  # prepare recreates its container itself
                elif snap and ctx.sb.is_running():
                    ctx.sb.restore(snap)
                else:
                    _fresh_sandbox(ctx, last_done)
            except InfraError as e2:
                ctx.store.event(tid, "sandbox_recovery_failed", node=name, error=str(e2)[:500])
        except Exception:  # a bug, not a transient failure: do not retry
            return "error", None, None, traceback.format_exc()
    return "error", None, None, "unreachable"


def run_task(inst: dict[str, Any], run_dir: Path, settings: Settings, run_id: str,
             stop_after: str | None = None, store: StateStore | None = None) -> dict[str, Any]:
    task: TaskSpec = to_task(inst)
    store = store or FileStateStore(run_dir)
    sock_dir = Path(settings.sock_root) / hashlib.sha1(f"{run_id}/{task.task_id}".encode()).hexdigest()[:10]
    ctx = TaskContext(run_id=run_id, task=task, settings=settings, store=store, harness=make_harness(settings),
                      tests=RepoTestInfo.build(sanitized(inst)), sock_dir=sock_dir)
    prev = store.load(task.task_id)
    if prev.get("status") in TERMINAL:
        return {"task_id": task.task_id, "status": prev["status"], "skipped": "already finished"}
    resumed = bool(prev) and prev.get("status") != "queued"  # a queued row (service) starts fresh
    done = _resume_point(ctx) if resumed else []
    if resumed:
        store.event(task.task_id, "task_resumed", completed=done)
    else:
        store.init_task(task.task_id, {"run_id": run_id, "harness": settings.harness, "model": settings.model,
                                       "thinking": settings.thinking, "image": task.image})
    # time already spent before an interruption still counts against the task deadline
    elapsed_before = max(0.0, prev.get("updated_at", 0) - prev.get("created_at", 0)) if resumed else 0.0
    t_start = time.time()
    ctx.deadline = t_start + settings.task_timeout_s - elapsed_before
    log = store.task_dir(task.task_id) / "model_calls.jsonl"
    gw = ModelGateway(sock_dir / "gw.sock", settings.vllm_url, log, task.task_id,
                      request_timeout_s=settings.request_timeout_s, token_budget=settings.task_token_budget,
                      used_tokens=_used_tokens(log))
    gw.start()
    ctx.gateway = gw
    status, stop_reason = "done", None
    try:
        if done:  # resumed: rebuild the sandbox as it was after the last finished node
            _fresh_sandbox(ctx, done[-1])
        last_done = done[-1] if done else None
        for name, fn in PIPELINE:
            if name in done:
                continue
            if not stop_reason and getattr(store, "cancel_requested", None) and store.cancel_requested(task.task_id):
                stop_reason = "cancelled"  # checked between nodes; a cancelled task does not wrap up
                store.event(task.task_id, "task_stopping", reason="cancelled", at_node=name)
            if stop_reason == "cancelled" or (stop_reason and name not in WRAP_UP):
                store.node_finish(task.task_id, name, "skipped", None, {"passed": None, "skipped": stop_reason},
                                  {"seconds": 0})
                continue
            gw.current_node = name
            store.node_start(task.task_id, name, node_input(ctx, name))
            t0 = time.time()
            try:
                st, out, check, err = _run_node(ctx, name, fn, last_done)
            except StopTask as e:
                stop_reason = e.reason
                store.node_finish(task.task_id, name, "stopped", None, {"passed": None, "stopped": e.reason},
                                  {"seconds": round(time.time() - t0, 2), "model": gw.node_usage(name),
                                   "agent_runs": ctx.agent_runs.get(name, [])})
                store.event(task.task_id, "task_stopping", reason=e.reason, at_node=name)
                continue
            metrics = {"seconds": round(time.time() - t0, 2), "model": gw.node_usage(name),
                       "agent_runs": ctx.agent_runs.get(name, [])}
            if st != "done":
                store.node_finish(task.task_id, name, "error", None, None, metrics, error=err)
                status = "infra_error"
                break
            ctx.outputs[name] = out
            _save_checkpoint(ctx, name)
            store.node_finish(task.task_id, name, "done", out.model_dump(), check, metrics)
            last_done = name
            if name in STOP_ON_FAIL and not check.get("passed"):
                status = "failed"
                break
            if name == stop_after:  # single-node re-test: stop right after the node under test
                status = "partial"
                break
        if stop_reason and status == "done":
            status = stop_reason
    finally:
        gw.stop()
        if ctx.sandbox and not settings.keep_containers:
            ctx.sandbox.destroy()
    patch = ctx.outputs.get("emit_patch")
    summary = {"seconds": round(elapsed_before + time.time() - t_start, 1), "resumed": resumed,
               "tokens": gw.total_tokens(), "patch_empty": getattr(patch, "empty", True),
               "review": getattr(ctx.outputs.get("review"), "verdict", None),
               "validated": getattr(ctx.outputs.get("repair"), "passed", None)}
    store.task_finish(task.task_id, status, summary)
    return {"task_id": task.task_id, "status": status, **summary}


def seed_from(src_run: Path, dst_run: Path, task_id: str, node: str) -> None:
    """Copy a task's checkpoints up to (not including) `node` from another run, so that run_task resumes
    exactly at `node` with the same inputs: the basis of single-node re-tests."""
    import shutil

    src, dst = src_run / "tasks" / task_id, dst_run / "tasks" / task_id
    if dst.exists():
        return
    upto = NODE_ORDER[: NODE_ORDER.index(node)]
    st = json.loads((src / "state.json").read_text())
    for n in NODE_ORDER:
        if n not in upto or st["nodes"][n].get("status") != "done":
            st["nodes"][n] = {"status": "pending"}
    st.update(status="running", current_node=None, summary=None, seeded_from=str(src_run.name))
    st["meta"] = {**st.get("meta", {}), "seeded_from": src_run.name, "rerun_node": node}
    st["created_at"] = st["updated_at"] = time.time()  # the time budget starts fresh
    (dst / "nodes").mkdir(parents=True, exist_ok=True)
    for i, n in enumerate(NODE_ORDER, 1):
        if n in upto:
            shutil.copytree(src / "nodes" / f"{i}-{n}", dst / "nodes" / f"{i}-{n}")
    (dst / "state.json").write_text(json.dumps(st, indent=2))
    with open(dst / "events.jsonl", "w") as f:
        f.write(json.dumps({"t": time.time(), "kind": "seeded", "from": src_run.name, "node": node}) + "\n")


def run_many(instances: list[dict[str, Any]], run_id: str, settings: Settings, parallel: int = 1,
             stop_after: str | None = None) -> list[dict]:
    run_dir = Path(settings.runs_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    meta_path = run_dir / "run.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {"run_id": run_id, "invocations": []}
    import subprocess

    from .config import ROOT

    git = lambda *a: subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True).stdout.strip()  # noqa: E731
    meta.setdefault("invocations", []).append({"started": time.time(), "tasks": [i["instance_id"] for i in instances],
                                               "parallel": parallel, "settings": settings.to_dict(),
                                               "code": {"commit": git("rev-parse", "HEAD"),
                                                        "dirty": bool(git("status", "--porcelain", "--", "src",
                                                                          "harness/pi/runner.mjs"))}})
    meta_path.write_text(json.dumps(meta, indent=2))
    results = []
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futs = {pool.submit(run_task, inst, run_dir, settings, run_id, stop_after): inst["instance_id"]
                for inst in instances}
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception:
                results.append({"task_id": futs[fut], "status": "infra_error", "error": traceback.format_exc()})
            print(json.dumps(results[-1]), flush=True)
    res_path = run_dir / "results.json"
    previous = json.loads(res_path.read_text()) if res_path.exists() else []
    finished = {r["task_id"] for r in results}
    res_path.write_text(json.dumps([r for r in previous if r["task_id"] not in finished] + results, indent=2))
    return results
