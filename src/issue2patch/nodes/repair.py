"""Node 6 (agent): fix what validation reported, at most N rounds."""
from __future__ import annotations

from .. import prompts
from ..contracts import RepairOut
from .context import TaskContext
from .validate import validate

TOOLS = ["read", "bash", "edit", "write"]


def run(ctx: TaskContext):
    v = ctx.outputs["validate"]
    if v.passed:
        return RepairOut(rounds=0, passed=True), {"passed": True, "rounds": 0, "skipped": True}
    rounds, validations = 0, []
    for i in range(1, ctx.settings.repair_rounds + 1):
        failures = [(c.name, c.detail, c.sandbox_log) for c in v.checks if not c.passed]
        src, _ = ctx.source_changes()
        ctx.run_agent("repair", prompts.REPAIR_SYSTEM,
                      prompts.repair_prompt(ctx.task.issue, ctx.sb.diff(src), failures),
                      TOOLS, prompts.REPAIR_SUBMIT, label=f"round{i}")
        rounds = i
        v = validate(ctx, "repair", f"r{i}")
        validations.append(v)
        if v.passed:
            break
    out = RepairOut(rounds=rounds, passed=v.passed, validations=validations)
    return out, {"passed": v.passed, "rounds": rounds,
                 "failed_checks": [c.name for c in v.checks if not c.passed]}
