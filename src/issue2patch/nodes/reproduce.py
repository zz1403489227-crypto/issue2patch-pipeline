"""Node 3 (agent): write a failing test that reproduces the issue."""
from __future__ import annotations

import shlex
from pathlib import PurePosixPath

from .. import prompts
from ..contracts import ReproOut
from ..repro_check import broken_run
from .context import TaskContext, tail

TOOLS = ["read", "bash", "edit", "write"]
FIX_LIMITS = {"max_requests": 15, "timeout_s": 600}


def _norm(path: str | None) -> str | None:
    if not path:
        return None
    p = path.strip()
    if p.startswith("/testbed/"):
        p = p[len("/testbed/"):]
    return p[2:] if p.startswith("./") else p


def _allowed_files(ctx: TaskContext, test_file: str | None) -> set[str]:
    """Files this node may leave behind: the test and the helpers it created for it.

    Closed-loop case 1, iteration 2: the first version kept only the test file and __init__.py, and
    deleted helper modules the test imports (Django repro tests usually need a models.py next to them),
    which turned working tests into "Failed to import test module". Now: every *new* file in a directory
    the agent created for the test, plus new files named like the repro test. Edits to existing files are
    still reverted.
    """
    if not test_file:
        return set()
    changed = ctx.sb.changed_files()
    untracked = set(ctx.sb.git("ls-files --others --exclude-standard").output.splitlines())
    parent = str(PurePosixPath(test_file).parent)
    new_dir = parent not in ("", ".") and not ctx.sb.git(f"ls-files -- {shlex.quote(parent)}").output.strip()
    allowed = {test_file}
    for f in changed:
        if f not in untracked:
            continue
        inside = f.startswith(parent + "/")
        if (inside and (new_dir or f.endswith("__init__.py"))) or "i2p_repro" in PurePosixPath(f).name:
            allowed.add(f)
    return allowed


def _enforce_contract(ctx: TaskContext, test_file: str | None) -> list[str]:
    allowed = _allowed_files(ctx, test_file)
    others = [f for f in ctx.sb.changed_files() if f not in allowed]
    ctx.sb.revert(others)
    ctx.cache["repro_paths"] = sorted(allowed)
    return others


def _run_on_original(ctx: TaskContext, command: str, name: str):
    r = ctx.sb.exec("\n".join([*ctx.tests.eval_commands, command]), timeout=ctx.settings.test_timeout_s)
    ctx.put_log("reproduce", name, r.output)
    return r


def run(ctx: TaskContext):
    pre = ctx.outputs["prefetch"]
    res = ctx.run_agent("reproduce", prompts.REPRO_SYSTEM,
                        prompts.repro_prompt(ctx.task.issue, ctx.task.repo, pre.source_candidates,
                                             pre.test_candidates, ctx.tests.how_to_run_hint()),
                        TOOLS, prompts.REPRO_SUBMIT)
    sub = res.submitted or {}
    test_file, command = _norm(sub.get("test_file")), (sub.get("command") or "").strip() or None
    reverted = _enforce_contract(ctx, test_file)  # contract: this node may only add test files

    out = ReproOut(submitted=bool(res.submitted), test_file=test_file, command=command,
                   note=sub.get("note", ""), reverted_files=reverted)
    exists = bool(test_file) and ctx.sb.read_file(f"{ctx.task.workdir}/{test_file}") is not None
    broken, fixed_by_agent = None, None
    if exists and command:
        r = _run_on_original(ctx, command, "repro_on_original")
        broken = broken_run(r.output, r.code, ctx.tests.framework) if r.code != 0 else None
        if broken:
            # Retry class 2 for this node's output: the test did not run at all. Send the error back once.
            fix = ctx.run_agent("reproduce", prompts.REPRO_SYSTEM,
                                prompts.repro_fix_prompt(ctx.task.issue, test_file, command, broken,
                                                         tail(r.output, 3000)),
                                TOOLS, prompts.REPRO_SUBMIT, label="fix-test", limits=FIX_LIMITS)
            fsub = fix.submitted or {}
            test_file = _norm(fsub.get("test_file")) or test_file
            command = (fsub.get("command") or "").strip() or command
            reverted += _enforce_contract(ctx, test_file)
            out.test_file, out.command = test_file, command
            r = _run_on_original(ctx, command, "repro_on_original_after_fix")
            broken = broken_run(r.output, r.code, ctx.tests.framework) if r.code != 0 else None
            fixed_by_agent = broken is None
        out.fails_on_original = r.code != 0 and broken is None
        out.run_excerpt = tail(r.output, 2500)
    out.reverted_files = reverted
    check = {"passed": out.usable, "agent_status": res.status, "test_file_exists": exists,
             "fails_on_original": out.fails_on_original, "broken_run": broken,
             "fixed_after_feedback": fixed_by_agent, "reverted_files": reverted}
    return out, check
