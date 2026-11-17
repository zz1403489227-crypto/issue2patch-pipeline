"""Minimal GitHub REST client for the service (sync the repository, publish the fix branch).

Why REST and not `git fetch`/`git push`: measured from the lab server on 2026-11-16, git's smart-HTTP
transfers to github.com fail intermittently (IPv6 route stalls; over IPv4 the TLS connection is cut
after ~90 s, "GnuTLS recv error (-110)"), while api.github.com answers in ~1.3 s every time. Requests
are forced onto IPv4 for the same reason.
"""
from __future__ import annotations

import base64
import time
from typing import Any

import requests
import urllib3.util.connection as _conn

_conn.HAS_IPV6 = False
API = "https://api.github.com"


class GitHub:
    def __init__(self, token: str, repo: str, api: str = API):
        self.repo, self.api = repo, api.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "issue2patch"})

    def call(self, method: str, path: str, accept: str | None = None, ok=(200, 201), **kw) -> requests.Response:
        headers = {"Accept": accept} if accept else {}
        last = None
        for attempt in range(4):
            try:
                r = self.s.request(method, f"{self.api}{path}", headers=headers, timeout=60, **kw)
            except requests.RequestException as e:  # connection reset etc.: retry with backoff
                last = e
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code >= 500:
                last = RuntimeError(f"{method} {path}: {r.status_code}")
                time.sleep(5 * (attempt + 1))
                continue
            return r
        raise RuntimeError(f"GitHub API {method} {path} failed: {last!r}")

    def branch(self, name: str) -> dict[str, Any]:
        r = self.call("GET", f"/repos/{self.repo}/branches/{name}")
        r.raise_for_status()
        b = r.json()
        return {"sha": b["commit"]["sha"], "tree": b["commit"]["commit"]["tree"]["sha"]}

    def compare_diff(self, base: str, head: str) -> str:
        r = self.call("GET", f"/repos/{self.repo}/compare/{base}...{head}", accept="application/vnd.github.diff")
        r.raise_for_status()
        return r.text

    def commit_files(self, base_sha: str, base_tree: str, files: list[dict[str, Any]], message: str) -> str:
        """One commit on top of base_sha with the given full file contents (or deletions). Returns its sha."""
        entries = []
        for f in files:
            if f.get("deleted"):
                entries.append({"path": f["path"], "mode": "100644", "type": "blob", "sha": None})
            else:
                blob = self.call("POST", f"/repos/{self.repo}/git/blobs",
                                 json={"content": base64.b64encode(f["content"]).decode(), "encoding": "base64"})
                blob.raise_for_status()
                entries.append({"path": f["path"], "mode": f.get("mode", "100644"), "type": "blob",
                                "sha": blob.json()["sha"]})
        tree = self.call("POST", f"/repos/{self.repo}/git/trees", json={"base_tree": base_tree, "tree": entries})
        tree.raise_for_status()
        commit = self.call("POST", f"/repos/{self.repo}/git/commits", json={
            "message": message, "tree": tree.json()["sha"], "parents": [base_sha],
            "author": {"name": "issue2patch", "email": "issue2patch@users.noreply.github.com"}})
        commit.raise_for_status()
        return commit.json()["sha"]

    def set_branch(self, branch: str, sha: str) -> None:
        """Create refs/heads/<branch> or force it to sha (idempotent)."""
        r = self.call("POST", f"/repos/{self.repo}/git/refs", ok=(201, 422),
                      json={"ref": f"refs/heads/{branch}", "sha": sha})
        if r.status_code == 201:
            return
        r = self.call("PATCH", f"/repos/{self.repo}/git/refs/heads/{branch}", json={"sha": sha, "force": True})
        r.raise_for_status()
