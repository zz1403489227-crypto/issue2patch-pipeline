"""Node 8 (deterministic): scope check and the final diff."""
from __future__ import annotations

import base64
import json
import shlex

from ..contracts import PatchOut
from .context import TaskContext

MAX_FILES = 10


def run(ctx: TaskContext):
    src, excluded = ctx.source_changes()
    diff = ctx.sb.diff(src)
    path = ctx.node_dir("emit_patch") / "patch.diff"
    path.write_text(diff)
    added = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    # Full post-change contents too: publishing through the GitHub Git Data API needs files, not a diff.
    manifest = []
    for f in src:
        q = shlex.quote(f)
        r = ctx.sb.exec(f"if [ -e {q} ]; then stat -c %a {q}; else echo deleted; fi", conda=False, timeout=60)
        mode = r.output.strip().splitlines()[-1] if r.output.strip() else "deleted"
        if mode == "deleted":
            manifest.append({"path": f, "deleted": True})
            continue
        body = ctx.sb.exec(f"base64 -w0 {q}", conda=False, timeout=60).output.strip()
        dest = ctx.node_dir("emit_patch") / "files" / f
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(base64.b64decode(body))
        manifest.append({"path": f, "mode": "100755" if mode.endswith(("5", "7")) else "100644"})
    (ctx.node_dir("emit_patch") / "files.json").write_text(json.dumps(manifest, indent=2))
    out = PatchOut(files=src, excluded=excluded, empty=not diff.strip(), patch_file=str(path),
                   lines_added=added, lines_removed=removed)
    checks = {"non_empty": not out.empty, "file_count_ok": len(src) <= MAX_FILES,
              "applies_cleanly": True}
    if diff.strip():
        # the patch must apply to a clean checkout of the base commit
        r = ctx.sb.exec("tmp=$(mktemp -d) && git worktree add -q --detach $tmp HEAD && cd $tmp && "
                        "git apply --check - ; rc=$?; cd /testbed && git worktree remove --force $tmp; exit $rc",
                        conda=False, input=diff, timeout=300)
        checks["applies_cleanly"] = r.code == 0
    return out, {"passed": all(checks.values()), "checks": checks}
