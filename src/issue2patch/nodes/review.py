"""Node 7 (agent): independent scope review in a fresh context."""
from __future__ import annotations

from .. import prompts
from ..contracts import ReviewOut
from .context import TaskContext

TOOLS = ["read", "bash"]


def run(ctx: TaskContext):
    src, _ = ctx.source_changes()
    if not src:
        return ReviewOut(submitted=False, verdict="none", comments="nothing to review"), \
            {"passed": False, "skipped": True}
    diff = ctx.sb.diff(src)
    snap = ctx.sb.snapshot()
    res = ctx.run_agent("review", prompts.REVIEW_SYSTEM, prompts.review_prompt(ctx.task.issue, diff),
                        TOOLS, prompts.REVIEW_SUBMIT)
    after = ctx.sb.snapshot()
    reverted = after != snap
    if reverted:  # the reviewer must not change anything
        ctx.sb.restore(snap)
    sub = res.submitted or {}
    verdict = sub.get("verdict") if sub.get("verdict") in ("approve", "request_changes") else "none"
    out = ReviewOut(submitted=bool(res.submitted), verdict=verdict, out_of_scope=sub.get("out_of_scope") or [],
                    comments=sub.get("comments", ""), reverted=reverted)
    return out, {"passed": verdict == "approve", "verdict": verdict, "agent_status": res.status,
                 "reviewer_modified_files": reverted}
