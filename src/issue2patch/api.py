"""HTTP entry point (stage 2). Bound to 127.0.0.1; long-running work is never done in a request:
a request only writes a queued row and publishes the task key, and returns at once.

    POST /tasks                    {"kind": "swebench", "instance_id": ..., "user": ..., "priority": 0|1}
    GET  /tasks/{task_id}          state from Postgres
    POST /tasks/{task_id}/cancel   flag checked by the worker before start and between nodes
    GET  /usage/{user}             tokens used against the user's quota
    GET  /health
"""
from __future__ import annotations

from typing import Any

import hashlib
import hmac
import json

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from . import service

app = FastAPI(title="issue2patch")


class TaskRequest(BaseModel):
    kind: str = "swebench"
    instance_id: str | None = None
    user: str
    priority: int = 0


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/tasks", status_code=202)
def create_task(req: TaskRequest) -> dict[str, Any]:
    if req.kind != "swebench" or not req.instance_id:
        raise HTTPException(400, "only kind=swebench with instance_id is accepted here; GitHub issues come "
                                 "in through the webhook")
    try:
        return service.submit({"kind": "swebench", "instance_id": req.instance_id,
                               "name": req.instance_id.split("__")[-1]}, req.user, req.priority)
    except PermissionError as e:
        raise HTTPException(429, str(e)) from e


@app.get("/tasks/{task_id}")
def read_task(task_id: str) -> dict[str, Any]:
    t = service.get_task(task_id)
    if t is None:
        raise HTTPException(404, "no such task")
    return t


@app.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> dict[str, Any]:
    r = service.cancel(task_id)
    if r["status"] is None:
        raise HTTPException(404, "no such task")
    return r


@app.post("/webhooks/github")
async def github_webhook(request: Request, x_hub_signature_256: str | None = Header(None),
                         x_github_event: str | None = Header(None)) -> dict[str, Any]:
    """GitHub `issues` events. Labelling an issue `i2p` in a registered repository queues a task.
    The payload is authenticated with the webhook secret (HMAC-SHA256), as GitHub documents."""
    body = await request.body()
    secret = service.service_env().get("I2P_WEBHOOK_SECRET", "").encode()
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    if not secret or not x_hub_signature_256 or not hmac.compare_digest(expected, x_hub_signature_256):
        raise HTTPException(401, "bad signature")
    if x_github_event == "ping":
        return {"ok": True}
    if x_github_event != "issues":
        return {"ignored": x_github_event}
    try:
        return service.submit_github_issue(json.loads(body))
    except PermissionError as e:
        raise HTTPException(429, str(e)) from e


@app.get("/usage/{user}")
def usage(user: str) -> dict[str, Any]:
    return service.user_usage(user)
