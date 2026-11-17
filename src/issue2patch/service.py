"""Stage 2: the pipeline as a multi-worker background service.

    HTTP API / GitHub webhook --(insert row: status=queued)--> Postgres
                              --(publish task key only)------> RabbitMQ  i2p.tasks (quorum queue)
    worker 1..N  <--(prefetch 1, manual ack)-- RabbitMQ
                 --(state read/write)------------> Postgres        (the queue is never asked about state)

Queue configuration (spec v8, stage 2, the four settings):
  1. manual acknowledgement: a task is acked only after run_task returns;
  2. prefetch 1: a worker holds one task at a time, so N workers = N concurrent tasks (backpressure);
  3. delivery-ack timeout > task timeout: consumer_timeout = 2 h (deploy/service/rabbitmq.conf) vs 1 h;
  4. delivery limit 3 (quorum queue x-delivery-limit), then the dead-letter queue i2p.tasks.dead.
A worker that dies leaves its message unacked; RabbitMQ redelivers it to another worker, which reads
the task's state from Postgres and resumes from the last finished node (pipeline resume logic).
Because a message can be delivered more than once, every step is idempotent: finished tasks are acked
and skipped, nodes restart from checkpoints, and the exit (opening a PR) checks before it acts.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

import pika

from .config import ROOT, Settings
from .state import TERMINAL
from .state_pg import PostgresStateStore, connect, init_schema

QUEUE, DLX, DEAD = "i2p.tasks", "i2p.dlx", "i2p.tasks.dead"
SERVICE_RUN = "svc"
DEFAULT_USER_BUDGET = 200_000_000  # tokens per user; the baseline used ~1.45M per task
log = logging.getLogger("i2p.service")


def service_env() -> dict[str, str]:
    p = ROOT / "var" / "service.env"
    return dict(line.split("=", 1) for line in p.read_text().splitlines() if "=" in line and not line.startswith("#"))


def pg_dsn() -> str:
    return f"postgresql://i2p:{service_env()['I2P_PG_PASSWORD']}@127.0.0.1:13432/i2p"


def mq_params() -> pika.ConnectionParameters:
    return pika.ConnectionParameters("127.0.0.1", 13672, "/",
                                     pika.PlainCredentials("i2p", service_env()["I2P_MQ_PASSWORD"]),
                                     heartbeat=60, blocked_connection_timeout=300)


def declare(ch) -> None:
    ch.exchange_declare(DLX, "direct", durable=True)
    ch.queue_declare(DEAD, durable=True, arguments={"x-queue-type": "quorum"})
    ch.queue_bind(DEAD, DLX, routing_key="dead")
    ch.queue_declare(QUEUE, durable=True, arguments={
        "x-queue-type": "quorum", "x-delivery-limit": 3,
        "x-dead-letter-exchange": DLX, "x-dead-letter-routing-key": "dead"})


def setup() -> None:
    init_schema(pg_dsn())
    conn = pika.BlockingConnection(mq_params())
    declare(conn.channel())
    conn.close()


def publish(key: str, priority: int = 0) -> None:
    conn = pika.BlockingConnection(mq_params())
    try:
        ch = conn.channel()
        ch.confirm_delivery()
        # RabbitMQ >= 4.3 quorum queues: strict priority, levels 0-31 (we use 0 = normal, 9 = high)
        ch.basic_publish("", QUEUE, json.dumps({"key": key}).encode(),
                         pika.BasicProperties(delivery_mode=2, priority=9 if priority > 0 else 0,
                                              content_type="application/json", message_id=key))
    finally:
        conn.close()


# --- tasks ---------------------------------------------------------------------------------------
def user_usage(user_id: str) -> dict[str, Any]:
    with connect(pg_dsn()) as c:
        c.execute("INSERT INTO users (user_id, token_budget, created_at) VALUES (%s,%s,%s) "
                  "ON CONFLICT DO NOTHING", (user_id, DEFAULT_USER_BUDGET, time.time()))
        budget = c.execute("SELECT token_budget FROM users WHERE user_id=%s", (user_id,)).fetchone()[0]
        used, n = c.execute("SELECT COALESCE(SUM(tokens),0), COUNT(*) FROM tasks WHERE user_id=%s",
                            (user_id,)).fetchone()
    return {"user": user_id, "budget": budget, "used": int(used), "remaining": int(budget - used), "tasks": n}


def submit(spec: dict[str, Any], user_id: str, priority: int = 0, task_id: str | None = None) -> dict[str, Any]:
    """Create a queued task row, then publish its key. Raises PermissionError when over quota."""
    usage = user_usage(user_id)
    if usage["remaining"] <= 0:
        raise PermissionError(f"user {user_id} is over the token quota ({usage['used']}/{usage['budget']})")
    task_id = task_id or f"{spec.get('name', 'task')}-{uuid.uuid4().hex[:6]}"
    key = f"{SERVICE_RUN}/{task_id}"
    with connect(pg_dsn()) as c:
        inserted = c.execute(
            """INSERT INTO tasks (key, run_id, task_id, status, user_id, priority, spec, created_at, updated_at)
               VALUES (%s,%s,%s,'queued',%s,%s,%s,%s,%s) ON CONFLICT (key) DO NOTHING RETURNING key""",
            (key, SERVICE_RUN, task_id, user_id, priority, json.dumps(spec), time.time(), time.time())).fetchone()
    if not inserted:  # same task id submitted again (e.g. a redelivered webhook): do not queue it twice
        return {"key": key, "task_id": task_id, "status": "exists"}
    publish(key, priority)
    return {"key": key, "task_id": task_id, "status": "queued"}


def repo_registry() -> dict[str, Any]:
    """Repositories the service accepts issues from, and how to build their environment (data/repos.json)."""
    p = ROOT / "data" / "repos.json"
    return json.loads(p.read_text()) if p.exists() else {}


def submit_github_issue(payload: dict[str, Any], label: str = "i2p") -> dict[str, Any]:
    """`issues` webhook, action `labeled` with the trigger label -> one task per (repo, issue)."""
    if payload.get("action") != "labeled" or (payload.get("label") or {}).get("name") != label:
        return {"ignored": "not the trigger label"}
    repo = payload["repository"]["full_name"]
    reg = repo_registry().get(repo)
    if not reg:
        return {"ignored": f"repository {repo} is not registered"}
    issue = payload["issue"]
    spec = {"kind": "github", "name": f"gh-{repo.split('/')[1]}-{issue['number']}", "repo": repo,
            "issue_number": issue["number"], "issue_url": issue["html_url"], "title": issue["title"],
            "body": issue.get("body") or "", "env_repo": reg["env_repo"], "env_version": reg["env_version"],
            "image": reg["image"],  # the base commit is resolved by the worker when the task starts
            "exit": {"api": "https://api.github.com", "repo": repo, "git_url": f"https://github.com/{repo}.git",
                     "base": reg.get("base", "main"), "token_file": reg.get("token_file", "var/github-token")}}
    user = (payload.get("sender") or {}).get("login", "unknown")
    return submit(spec, user, priority=0, task_id=spec["name"])


def cancel(task_id: str) -> dict[str, Any]:
    key = f"{SERVICE_RUN}/{task_id}"
    with connect(pg_dsn()) as c:
        row = c.execute("UPDATE tasks SET cancel_requested = true, "
                        "status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE status END "
                        "WHERE key = %s RETURNING status", (key,)).fetchone()
    return {"task_id": task_id, "status": row[0] if row else None}


def get_task(task_id: str) -> dict[str, Any] | None:
    with connect(pg_dsn()) as c:
        row = c.execute("SELECT status, current_node, user_id, priority, cancel_requested, spec, nodes, summary, "
                        "tokens, pr_url, attempts, created_at, updated_at, finished_at FROM tasks WHERE key = %s",
                        (f"{SERVICE_RUN}/{task_id}",)).fetchone()
    if not row:
        return None
    keys = ["status", "current_node", "user_id", "priority", "cancel_requested", "spec", "nodes", "summary",
            "tokens", "pr_url", "attempts", "created_at", "updated_at", "finished_at"]
    return {"task_id": task_id, **dict(zip(keys, row))}


def instance_for(spec: dict[str, Any]) -> dict[str, Any]:
    """The pipeline's input for a service task (an instance-shaped dict, hidden fields empty)."""
    from .tasks import load_instances

    if spec["kind"] == "swebench":
        from .tasks import image_for

        inst = dict(load_instances(Settings().data_file)[spec["instance_id"]])
        inst["image"] = image_for(spec["instance_id"])
        for k in ("patch", "test_patch", "hints_text"):
            inst[k] = ""
        inst["FAIL_TO_PASS"] = inst["PASS_TO_PASS"] = "[]"
        return inst
    if spec["kind"] == "github":
        return {"repo": spec["env_repo"], "version": spec["env_version"], "base_commit": spec.get("base_commit", ""),
                "problem_statement": f"{spec['title']}\n\n{spec.get('body') or ''}", "image": spec["image"],
                "patch": "", "test_patch": "", "hints_text": "", "FAIL_TO_PASS": "[]", "PASS_TO_PASS": "[]",
                "environment_setup_commit": spec.get("base_commit", "")}
    raise ValueError(f"unknown task kind {spec['kind']!r}")


def sync_from_repository(spec: dict[str, Any], task_dir: Path) -> dict[str, Any]:
    """Resolve the base branch and compute the patch that turns the image's checkout (`image_base_ref`)
    into it. Done once per task and kept with the task, so a resumed task works on the same commit."""

    done = task_dir / "source.json"
    if done.exists():
        return json.loads(done.read_text())
    reg = repo_registry()[spec["repo"]]
    token = (ROOT / reg.get("token_file", "var/github-token")).read_text().strip()
    from .github_api import GitHub

    # REST, not git: see github_api.py for the measurements behind this.
    gh = GitHub(token, spec["repo"])
    base = gh.branch(reg.get("base", "main"))
    base_sha, tree = base["sha"], base["tree"]
    patch = gh.compare_diff(reg["image_base_ref"], base_sha)
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "setup.patch").write_text(patch)
    info = {"base_commit": base_sha, "expected_tree": tree, "base_tree": tree,
            "setup_patch_file": str(task_dir / "setup.patch"), "source": f"{spec['repo']}@{base_sha[:12]}"}
    done.write_text(json.dumps(info))
    return info


# --- worker --------------------------------------------------------------------------------------
class Worker:
    def __init__(self, name: str | None = None):
        self.name = name or f"{socket.gethostname()}-{os.getpid()}"
        self.settings = Settings()
        self.store = PostgresStateStore(Path(self.settings.runs_dir) / SERVICE_RUN, pg_dsn())
        self.exit_hook = self.open_pr  # stage 2 exit: one PR per task (exit_pr.py)

    def open_pr(self, task_id: str, row: dict[str, Any] | None) -> dict[str, Any] | None:
        from .exit_pr import Forge, open_pr

        ex = ((row or {}).get("spec") or {}).get("exit")
        if not ex:
            return None
        token = None
        if ex.get("token_file"):
            token = (ROOT / ex["token_file"]).read_text().strip()
        task_dir = Path(self.settings.runs_dir) / SERVICE_RUN / "tasks" / task_id
        src = json.loads((task_dir / "source.json").read_text()) if (task_dir / "source.json").exists() else {}
        forge = Forge(ex["api"], ex["repo"], ex["git_url"], token, base_sha=src.get("base_commit"),
                      base_tree=src.get("base_tree"), files_dir=task_dir / "nodes" / "8-emit_patch")
        summary = (row or {}).get("summary") or {}
        validated = bool(summary.get("validated"))
        spec = row["spec"]
        title = f"[issue2patch] {spec.get('title') or task_id}"
        body = "\n".join([
            f"Automated patch for {spec.get('issue_url') or task_id}. **Needs human review before merging.**", "",
            f"- validation (lint, reproduction test, related tests): {'passed' if validated else 'NOT passed'}",
            f"- independent review: {summary.get('review')}",
            f"- task: `{task_id}` (state and full trace in the issue2patch service)"])
        patch = Path(self.settings.runs_dir) / SERVICE_RUN / "tasks" / task_id / "nodes" / "8-emit_patch" / "patch.diff"
        r = open_pr(pg_dsn(), f"{SERVICE_RUN}/{task_id}", task_id, forge, ex.get("base", "main"), patch, title, body)
        log.info("%s: exit for %s -> %s", self.name, task_id, r)
        return r

    def process(self, key: str) -> str:
        """Run one task. Returns 'ack' or 'requeue'."""
        from .pipeline import run_task

        task_id = key.split("/", 1)[1]
        st = self.store.load(task_id)
        row = get_task(task_id)
        if not st or row is None:
            log.warning("unknown task %s, dropping", key)
            return "ack"
        if st["status"] in TERMINAL:  # duplicate delivery of a finished task
            self.store.event(task_id, "duplicate_delivery_skipped", worker=self.name, status=st["status"])
            if self.exit_hook and st["status"] == "done":
                self.exit_hook(task_id, row)  # idempotent: opens the PR only if it is not open yet
            return "ack"
        if row["cancel_requested"]:
            self.store.task_finish(task_id, "cancelled", {"cancelled_before_start": True})
            return "ack"
        attempt = self.store.mark_started(task_id)
        self.store.event(task_id, "worker_picked_up", worker=self.name, attempt=attempt)
        inst = instance_for(row["spec"])
        inst["instance_id"] = task_id
        if row["spec"]["kind"] == "github":
            inst.update(sync_from_repository(row["spec"], self.store.task_dir(task_id)))
        result = run_task(inst, self.store.run_dir, self.settings, SERVICE_RUN, store=self.store)
        if result["status"] == "infra_error":
            return "requeue"  # delivery limit 3, then dead-letter
        if self.exit_hook and result["status"] in ("done",):
            self.exit_hook(task_id, get_task(task_id))
        return "ack"

    def serve(self) -> None:
        conn = pika.BlockingConnection(mq_params())
        ch = conn.channel()
        declare(ch)
        ch.basic_qos(prefetch_count=1)

        def finish(tag: int, verdict: str) -> None:
            if verdict == "ack":
                ch.basic_ack(tag)
            else:
                ch.basic_nack(tag, requeue=True)

        def handle(ch_, method, props, body) -> None:
            key = json.loads(body)["key"]
            log.info("%s: got %s (redelivered=%s)", self.name, key, method.redelivered)

            def work() -> None:
                verdict = "requeue"
                try:
                    verdict = self.process(key)
                except Exception:
                    log.error("task %s crashed the worker thread:\n%s", key, traceback.format_exc())
                # RabbitMQ channels are not thread-safe: ack from the connection's own thread
                conn.add_callback_threadsafe(lambda: finish(method.delivery_tag, verdict))

            # run the task off the I/O thread so heartbeats keep flowing during a 10-minute task
            threading.Thread(target=work, name=f"task-{key}", daemon=True).start()

        ch.basic_consume(QUEUE, handle, auto_ack=False)
        log.info("worker %s consuming %s", self.name, QUEUE)
        ch.start_consuming()
