"""Node 4 (agent): change the source code."""
from __future__ import annotations

from .. import prompts
from ..contracts import ImplementOut
from .context import TaskContext

TOOLS = ["read", "bash", "edit", "write"]


def run(ctx: TaskContext):
    pre, rep = ctx.outputs["prefetch"], ctx.outputs["reproduce"]
    usable = rep.usable
    before = ctx.sb.snapshot()
    res = ctx.run_agent("implement", prompts.IMPL_SYSTEM,
                        prompts.impl_prompt(ctx.task.issue, ctx.task.repo, pre.source_candidates,
                                            rep.test_file if usable else None,
                                            rep.command if usable else None,
                                            rep.run_excerpt if usable else ""),
                        TOOLS, prompts.IMPL_SUBMIT)
    # The repro test is part of the contract input; restore it if the agent touched it.
    for f in ctx.repro_paths():
        ctx.sb.exec(f"git diff --quiet {before} -- {f} 2>/dev/null || git checkout {before} -- {f}",
                    conda=False, timeout=60)
    src, _ = ctx.source_changes()
    out = ImplementOut(submitted=bool(res.submitted), summary=(res.submitted or {}).get("summary", ""),
                       changed_files=src)
    (ctx.node_dir("implement") / "diff.patch").write_text(ctx.sb.diff(src))
    return out, {"passed": bool(src), "agent_status": res.status, "n_changed": len(src)}
