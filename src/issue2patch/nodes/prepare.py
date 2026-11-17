"""Node 1 (deterministic): start the sandbox and make the checkout safe to work in."""
from __future__ import annotations

import hashlib
import re
import shlex
from pathlib import Path

from ..contracts import SandboxInfo
from ..sandbox import GIT_PREPARE, DockerSandbox
from .context import TaskContext


def container_name(run_id: str, task_id: str) -> str:
    h = hashlib.sha1(f"{run_id}/{task_id}".encode()).hexdigest()[:8]
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", f"i2p-{task_id}-{h}")[:120]


def run(ctx: TaskContext):
    s, t = ctx.settings, ctx.task
    mounts = [
        (s.node_home, "/opt/node", "ro"),
        (s.pi_dir, "/opt/pi", "ro"),
        (s.tools_dir, "/opt/i2p-tools", "ro"),
        (str(ctx.sock_dir), "/i2p", "rw"),  # holds only the gateway socket
    ]
    ctx.sandbox = DockerSandbox.create(t.image, container_name(ctx.run_id, t.task_id), mounts,
                                       s.container_cpus, s.container_memory, t.workdir)
    r = ctx.sb.exec(GIT_PREPARE, timeout=900, conda=False)
    (ctx.node_dir("prepare") / "git_prepare.log").write_text(r.output)
    kv = dict(re.findall(r"^([A-Z_]+)=(\S*)$", r.output, flags=re.M))
    out = SandboxInfo(container=ctx.sb.container, image_id=ctx.sb.image_id, workdir=t.workdir,
                      head=kv.get("HEAD", ""), refs_removed=int(kv.get("REFS_REMOVED", "0") or 0))
    checks = {
        "git_prepare_ok": r.code == 0,
        # SWE-bench images add one "SWE-bench" environment commit on top of the base commit.
        "head_is_base_commit": out.head == t.base_commit
        or (kv.get("PARENT") == t.base_commit and kv.get("HEAD_SUBJECT") == "SWE-bench"),
        "worktree_clean": kv.get("DIRTY") == "0",
        "no_future_commits": kv.get("ALL_COMMITS_AHEAD") == "0",
    }
    if t.setup_patch_file:
        # Service task from a real repository: bring the image's checkout to the repository's commit.
        patch = Path(t.setup_patch_file).read_text()
        a = ctx.sb.exec("git apply --binary --whitespace=nowarn - && git add -A && "
                        f"git commit -q -m {shlex.quote('sync with ' + (t.source or 'repository'))}",
                        conda=False, input=patch, timeout=600)
        tree = ctx.sb.git("rev-parse HEAD^{tree}").output.strip()
        out.head = ctx.sb.git("rev-parse HEAD").output.strip()
        checks["head_is_base_commit"] = a.code == 0 and tree == t.expected_tree
    return out, {"passed": all(checks.values()), "checks": checks}
