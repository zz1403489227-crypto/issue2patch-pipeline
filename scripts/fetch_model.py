"""Materialize an official Hugging Face model revision under var/models, verified file by file.

Every file is checked against the hash the hub publishes for the pinned revision (sha256 for LFS
files, git blob sha1 for the rest). Bytes already on disk elsewhere may be reused via --reuse-dir,
but only if they match the official hash; they are hard-linked, so nothing here depends on that
directory afterwards. Anything that does not match is downloaded.

usage: python scripts/fetch_model.py Qwen/Qwen3.6-35B-A3B-FP8 --out var/models/Qwen3.6-35B-A3B-FP8 \
           [--revision <sha>] [--reuse-dir DIR] [--endpoint https://hf-mirror.com]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path


UA = {"User-Agent": "issue2patch-model-fetch/0.1"}


def get_json(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return json.loads(r.read())


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(16 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_blob_sha1(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def download(url: str, dest: Path) -> None:
    tmp = dest.with_name(dest.name + ".part")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r, \
                    open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 16 << 20)
            os.replace(tmp, dest)
            return
        except Exception as e:  # network hiccup: retry with backoff
            print(f"  retry {attempt + 1} for {dest.name}: {e!r}", flush=True)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"download failed: {url}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--out", required=True)
    ap.add_argument("--revision", default=None)
    ap.add_argument("--reuse-dir", default=None)
    ap.add_argument("--endpoint", default=os.environ.get("HF_ENDPOINT", "https://hf-mirror.com"))
    a = ap.parse_args()
    ep = a.endpoint.rstrip("/")
    rev = a.revision or get_json(f"{ep}/api/models/{a.repo}")["sha"]
    tree = get_json(f"{ep}/api/models/{a.repo}/tree/{rev}?recursive=true")
    files = [t for t in tree if t["type"] == "file"]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    reuse = Path(a.reuse_dir) if a.reuse_dir else None
    manifest = {"repo": a.repo, "revision": rev, "endpoint": ep, "files": []}
    for t in files:
        name, dest = t["path"], out / t["path"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        lfs = t.get("lfs")
        want = lfs["oid"] if lfs else t["oid"]
        check = sha256 if lfs else git_blob_sha1
        source = "existing"
        if not (dest.exists() and check(dest) == want):
            cand = reuse / name if reuse else None
            if lfs and cand and cand.exists() and cand.stat().st_size == lfs["size"] and check(cand) == want:
                if dest.exists():
                    dest.unlink()
                os.link(cand, dest)
                source = "reused-verified"
            else:
                download(f"{ep}/{a.repo}/resolve/{rev}/{name}", dest)
                source = "downloaded"
        got = check(dest)
        ok = got == want
        print(f"{'OK ' if ok else 'BAD'} {source:16s} {name}", flush=True)
        manifest["files"].append({"path": name, "hash": want, "algo": "sha256" if lfs else "git-blob-sha1",
                                  "source": source, "ok": ok})
        if not ok:
            return 1
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=2))
    print(f"all {len(files)} files verified at revision {rev}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
