"""A local stand-in for the two GitHub REST endpoints the exit uses, plus a bare git repository.

Only for tests of the service mechanics (idempotent PR creation under worker failure) before a real
test repository and token exist. Behaves like GitHub where it matters: one open PR per head branch,
422 "A pull request already exists" on a duplicate. State is a JSON file, so tests can count PRs.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parents[2] / "var" / "fake-github"
STATE = ROOT / "prs.json"
_lock = threading.Lock()
app = FastAPI(title="fake-github")


def _load() -> list[dict]:
    return json.loads(STATE.read_text()) if STATE.exists() else []


@app.get("/repos/{owner}/{name}/pulls")
def list_prs(owner: str, name: str, state: str = "open", head: str | None = None):
    prs = [p for p in _load() if p["repo"] == f"{owner}/{name}" and p["state"] == state]
    if head:
        prs = [p for p in prs if f"{owner}:{p['head']}" == head]
    return prs


@app.post("/repos/{owner}/{name}/pulls")
async def create_pr(owner: str, name: str, request: Request):
    body = await request.json()
    repo = f"{owner}/{name}"
    with _lock:
        prs = _load()
        if any(p["repo"] == repo and p["head"] == body["head"] and p["state"] == "open" for p in prs):
            return JSONResponse({"message": "Validation Failed",
                                 "errors": [{"message": f"A pull request already exists for {owner}:{body['head']}."}]},
                                status_code=422)
        n = len(prs) + 1
        pr = {"number": n, "repo": repo, "head": body["head"], "base": body["base"], "title": body["title"],
              "state": "open", "html_url": f"http://fake-github.local/{repo}/pull/{n}"}
        prs.append(pr)
        ROOT.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(prs, indent=2))
    return JSONResponse(pr, status_code=201)


@app.get("/health")
def health():
    if not ROOT.exists():
        raise HTTPException(500, "no fake-github dir")
    return {"ok": True}
