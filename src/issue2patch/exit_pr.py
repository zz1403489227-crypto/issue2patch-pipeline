"""The exit: open one pull request per task, no matter how often the task is delivered or retried.

Messages can be redelivered (a worker dies after finishing but before acking) and a worker can crash
between creating the PR and recording it. So the exit is written as an idempotent procedure keyed by
the task id, under a Postgres advisory lock so that two workers can never run it for the same task at
the same time:

  1. Postgres already has a PR URL for the task  -> done.
  2. The forge already has an open PR whose head is the task's branch (`i2p/<task_id>`)
     -> record it, done. (This is the crash-after-create case.)
  3. Push the branch (deterministic content: base commit + the task's patch; a re-push is a no-op).
  4. Create the PR; if the forge answers "already exists", go back to 2.
  5. Record the URL with `UPDATE ... WHERE pr_url IS NULL`.

The forge is GitHub's REST API; tests use a local stand-in with the same endpoints (fake_github.py).
Least privilege: the token only needs Contents (write, to push the branch) and Pull requests (write)
on the one test repository.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .state_pg import connect


class Forge:
    def __init__(self, api: str, repo: str, git_url: str, token: str | None = None, base_sha: str | None = None,
                 base_tree: str | None = None, files_dir: Path | None = None):
        self.api, self.repo, self.git_url, self.token = api.rstrip("/"), repo, git_url, token
        self.base_sha, self.base_tree, self.files_dir = base_sha, base_tree, files_dir
        self.gh = None
        if self.api == "https://api.github.com":
            from .github_api import GitHub

            self.gh = GitHub(token or "", repo)

    def _req(self, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        if self.gh is not None:  # real GitHub: requests over IPv4 with retries (github_api.py)
            r = self.gh.call(method, path, json=body) if body is not None else self.gh.call(method, path)
            return r.status_code, (r.json() if r.content else None)
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "issue2patch",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        if data:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{self.api}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def find_open_pr(self, branch: str) -> str | None:
        owner = self.repo.split("/")[0]
        code, prs = self._req("GET", f"/repos/{self.repo}/pulls?state=open&head={owner}:{branch}")
        if code != 200:
            raise RuntimeError(f"listing PRs failed: {code} {prs}")
        return prs[0]["html_url"] if prs else None

    def create_pr(self, branch: str, base: str, title: str, body: str) -> tuple[str | None, bool]:
        """(url, already_existed)."""
        code, pr = self._req("POST", f"/repos/{self.repo}/pulls",
                             {"title": title, "head": branch, "base": base, "body": body})
        if code == 201:
            return pr["html_url"], False
        if code == 422 and "already exists" in json.dumps(pr):
            return None, True
        raise RuntimeError(f"creating the PR failed: {code} {pr}")

    def push_branch(self, branch: str, base_ref: str, patch: str, message: str) -> None:
        if self.gh is not None:
            # Real GitHub: one commit on the task's base commit via the Git Data API, then point the branch
            # at it (create or force-update: idempotent).
            manifest = json.loads((self.files_dir / "files.json").read_text())
            files = [{"path": m["path"], "deleted": True} if m.get("deleted") else
                     {"path": m["path"], "mode": m["mode"], "content": (self.files_dir / "files" / m["path"]).read_bytes()}
                     for m in manifest]
            sha = self.gh.commit_files(self.base_sha, self.base_tree, files, message)
            self.gh.set_branch(branch, sha)
            return
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        url = self.git_url
        if self.token and url.startswith("https://"):
            url = url.replace("https://", f"https://x-access-token:{self.token}@", 1)
        with tempfile.TemporaryDirectory(prefix="i2p-exit-") as d:
            def git(*a: str, inp: str | None = None) -> str:
                for attempt in range(4 if a[0] in ("fetch", "push") else 1):  # network steps are retried
                    p = subprocess.run(["git", *a], cwd=d, input=inp, capture_output=True, text=True, env=env,
                                       timeout=600)
                    if p.returncode == 0:
                        break
                    time.sleep(10 * (attempt + 1))
                if p.returncode != 0:  # never echo the URL: it may carry the token
                    err = p.stderr.replace(self.token, "***") if self.token else p.stderr
                    raise RuntimeError(f"git {a[0]} failed: {err[-800:]}")
                return p.stdout
            git("init", "-q")
            git("remote", "add", "origin", url)
            git("fetch", "-4", "-q", "--depth", "1", "origin", base_ref)  # IPv4: see service.sync_from_repository
            git("checkout", "-q", "-B", branch, "FETCH_HEAD")
            git("apply", "--whitespace=nowarn", "-", inp=patch)
            git("add", "-A")
            git("-c", "user.name=issue2patch", "-c", "user.email=issue2patch@users.noreply.github.com",
                "commit", "-q", "-m", message)
            git("push", "-4", "-q", "-f", "origin", f"HEAD:refs/heads/{branch}")


def open_pr(dsn: str, key: str, task_id: str, forge: Forge, base: str, patch_file: Path,
            title: str, body: str) -> dict[str, Any]:
    branch = f"i2p/{task_id}"
    with connect(dsn) as c:
        c.execute("SELECT pg_advisory_lock(hashtext(%s))", (key,))
        try:
            url = c.execute("SELECT pr_url FROM tasks WHERE key=%s", (key,)).fetchone()[0]
            if url:
                return {"pr_url": url, "action": "already recorded"}
            url = forge.find_open_pr(branch)
            action = "found existing PR"
            if not url:
                patch = patch_file.read_text() if patch_file.exists() else ""
                if not patch.strip():
                    return {"pr_url": None, "action": "empty patch, no PR"}
                forge.push_branch(branch, base, patch, f"{title}\n\n{task_id}")
                url, existed = forge.create_pr(branch, base, title, body)
                action = "created PR"
                if os.environ.get("I2P_CRASH_AFTER_PR_CREATE") == "1":  # fault injection for the stage-2 test
                    os._exit(17)
                if existed:
                    url, action = forge.find_open_pr(branch), "found existing PR after 422"
            c.execute("UPDATE tasks SET pr_url=%s WHERE key=%s AND pr_url IS NULL", (url, key))
            c.execute("INSERT INTO events (key, t, kind, data) VALUES (%s, extract(epoch from now()), 'pr', %s)",
                      (key, json.dumps({"url": url, "action": action})))
            return {"pr_url": url, "action": action}
        finally:
            c.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
