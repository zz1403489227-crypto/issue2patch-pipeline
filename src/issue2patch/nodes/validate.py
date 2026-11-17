"""Node 5 (deterministic): lint, the repro test, and related existing tests."""
from __future__ import annotations

import json
import shlex

from ..contracts import CheckResult, ValidateOut
from ..testing import newly_failing, related_test_files
from .context import TaskContext, tail

RUFF_RULES = "E9,F63,F7,F82"  # syntax errors and undefined names only


def _ruff(ctx: TaskContext, files: list[str], at_head: bool) -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for f in files:
        q = shlex.quote(f)
        if at_head:
            cmd = (f"git cat-file -e HEAD:{q} 2>/dev/null && git show HEAD:{q} | /opt/i2p-tools/ruff check --no-cache "
                   f"--select {RUFF_RULES} --output-format json --stdin-filename {q} - || true")
        else:
            cmd = f"/opt/i2p-tools/ruff check --no-cache --select {RUFF_RULES} --output-format json {q} || true"
        r = ctx.sb.exec(cmd, conda=False, timeout=120)
        start = r.output.find("[")
        try:
            items = json.loads(r.output[start:]) if start >= 0 else []
        except ValueError:
            items = []
        for it in items:
            found.add((f, it.get("code") or "", it.get("message") or ""))
    return found


def validate(ctx: TaskContext, node: str, label: str) -> ValidateOut:
    checks: list[CheckResult] = []
    src, _ = ctx.source_changes()
    if not src:
        return ValidateOut(passed=False, checks=[CheckResult(name="has_source_changes", passed=False,
                                                             detail="No source file was changed.")])
    checks.append(CheckResult(name="has_source_changes", passed=True, detail=", ".join(src)))

    # 1. lint: only findings that the change introduced
    py = [f for f in src if f.endswith(".py") and ctx.sb.read_file(f"{ctx.task.workdir}/{f}") is not None]
    new = sorted(_ruff(ctx, py, at_head=False) - _ruff(ctx, py, at_head=True))
    detail = "\n".join(f"{f}: {c} {m}" for f, c, m in new)
    checks.append(CheckResult(name="lint", passed=not new, detail=detail or "no new syntax/undefined-name errors"))

    # 2. the reproduction test should pass now
    rep = ctx.outputs.get("reproduce")
    if rep is not None and rep.usable:
        r = ctx.sb.exec("\n".join([*ctx.tests.eval_commands, rep.command]), timeout=ctx.settings.test_timeout_s)
        host, inside = ctx.put_log(node, f"{label}-repro", r.output)
        checks.append(CheckResult(name="repro_test", passed=r.code == 0, log_file=host, sandbox_log=inside,
                                  detail=f"`{rep.command}` exit {r.code}\n{tail(r.output, 2500)}"))
    else:
        checks.append(CheckResult(name="repro_test", passed=True, skipped=True, detail="no usable repro test"))

    # 3. related existing tests: nothing that passed before may fail now
    if "related_tests" not in ctx.cache:
        pre = ctx.outputs.get("prefetch")
        hinted = [c.path for c in pre.test_candidates[:2]] if pre else []
        ctx.cache["related_tests"] = related_test_files(ctx.cache.get("py_files", []), src, hinted,
                                                        exclude=ctx.repro_paths())
    tests = ctx.cache["related_tests"]
    if not tests:
        checks.append(CheckResult(name="related_tests", passed=True, skipped=True, detail="no related test files found"))
    else:
        if "baseline_statuses" not in ctx.cache:
            tracked = [f for f in src if ctx.sb.exec(f"git cat-file -e HEAD:{shlex.quote(f)}", conda=False).code == 0]
            snap = ctx.sb.snapshot()
            ctx.sb.revert(src)
            base_res, base = ctx.tests.run(ctx.sb, tests, ctx.settings.test_timeout_s)
            ctx.sb.restore(snap)
            ctx.put_log(node, "related-baseline", base_res.output)
            ctx.cache["baseline_statuses"] = base
            ctx.cache["baseline_tracked"] = tracked
        base = ctx.cache["baseline_statuses"]
        res, now = ctx.tests.run(ctx.sb, tests, ctx.settings.test_timeout_s)
        host, inside = ctx.put_log(node, f"{label}-related", res.output)
        broken = newly_failing(base, now)
        if not now and not base:
            # no parsed results before or after: this selection gives no signal either way
            checks.append(CheckResult(name="related_tests", passed=True, skipped=True, log_file=host,
                                      sandbox_log=inside, detail=f"no parsable results from {', '.join(tests)} "
                                                                 f"(exit {res.code}); not counted"))
            return ValidateOut(passed=all(c.passed for c in checks), checks=checks)
        if not now and res.code != 0:
            ok, detail = False, f"tests did not run or could not be parsed (exit {res.code})\n{tail(res.output, 2000)}"
        else:
            ok = not broken
            detail = (f"ran {', '.join(tests)}: {len(now)} tests parsed; newly failing: "
                      f"{', '.join(broken[:20]) or 'none'}")
            if broken:
                detail += "\n" + tail(res.output, 2500)
        checks.append(CheckResult(name="related_tests", passed=ok, detail=detail, log_file=host, sandbox_log=inside))
    return ValidateOut(passed=all(c.passed for c in checks), checks=checks)


def run(ctx: TaskContext):
    out = validate(ctx, "validate", "v0")
    return out, {"passed": out.passed, "failed_checks": [c.name for c in out.checks if not c.passed]}
