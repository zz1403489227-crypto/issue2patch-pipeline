"""Node-level metrics with truth computed by code, and failure attribution (spec §5).

Runs entirely outside the pipeline: it is the only code that reads the reference patch and the hidden
test results. Per task and node it computes a boolean "ok" and attributes every unresolved task to the
first node that is not ok.

| node      | ok means                                                                          |
|-----------|-----------------------------------------------------------------------------------|
| prepare   | sandbox prepared (its own check)                                                  |
| prefetch  | every file the reference patch changes is among the source candidates             |
| reproduce | the test fails on the original code and passes once the reference patch is applied |
| implement | the first validation passed (one-shot pass)                                       |
| repair    | the final validation passed                                                       |
| review    | the verdict agrees with the hidden tests (approve iff resolved)                   |
| emit_patch| the patch is non-empty and applies to the base commit                             |
"""
from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .config import Settings
from .sandbox import DockerSandbox, InfraError
from .state import NODE_ORDER
from .tasks import sanitized
from .testing import RepoTestInfo


def _load(p: Path) -> Any:
    return json.loads(p.read_text()) if p.exists() else None


def gold_files(patch: str) -> set[str]:
    return set(re.findall(r"^diff --git a/(\S+) b/", patch, flags=re.M))


def repro_validity(inst: dict, task_dir: Path, settings: Settings) -> dict[str, Any]:
    """Run the pipeline's repro test on the original code and with the reference patch applied."""
    nd = task_dir / "nodes" / "3-reproduce"
    out = _load(nd / "output.json") or {}
    if not (out.get("submitted") and out.get("test_file") and out.get("command")):
        return {"has_test": False, "valid": False}
    patch = (nd / "worktree.patch").read_text() if (nd / "worktree.patch").exists() else ""
    tests = RepoTestInfo.build(sanitized(inst))
    from .tasks import image_for

    name = "i2p-oracle-" + hashlib.sha1(str(task_dir).encode()).hexdigest()[:10]
    sb = DockerSandbox.create(image_for(inst["instance_id"]), name, [], settings.container_cpus,
                              settings.container_memory)
    try:
        sb.apply_worktree_patch(patch)
        cmd = "\n".join([*tests.eval_commands, out["command"]])
        before = sb.exec(cmd, timeout=settings.test_timeout_s)
        r = sb.exec("git apply --whitespace=nowarn -", conda=False, input=inst["patch"], timeout=120)
        if r.code != 0:
            return {"has_test": True, "valid": False, "error": "reference patch did not apply on top of the test"}
        after = sb.exec(cmd, timeout=settings.test_timeout_s)
        return {"has_test": True, "fails_before": before.code != 0, "passes_after": after.code == 0,
                "valid": before.code != 0 and after.code == 0}
    finally:
        sb.destroy()


def evaluate(run_dir: Path, instances: dict[str, dict], settings: Settings, workers: int = 6) -> dict[str, Any]:
    resolved = _load(run_dir / "grading" / "resolved.json") or {}
    task_dirs = sorted(d for d in (run_dir / "tasks").iterdir() if d.is_dir())
    cache_path = run_dir / "repro_validity.json"
    validity: dict[str, Any] = _load(cache_path) or {}

    def one(d: Path):
        if d.name in validity:
            return
        try:
            validity[d.name] = repro_validity(instances[d.name], d, settings)
        except InfraError as e:
            validity[d.name] = {"has_test": None, "valid": None, "error": str(e)[:300]}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, task_dirs))
    cache_path.write_text(json.dumps(validity, indent=2))

    rows = {}
    for d in task_dirs:
        tid, inst = d.name, instances[d.name]
        o = {n: _load(d / "nodes" / f"{i}-{n}" / "output.json") for i, n in enumerate(NODE_ORDER, 1)}
        c = {n: _load(d / "nodes" / f"{i}-{n}" / "check.json") or {} for i, n in enumerate(NODE_ORDER, 1)}
        gold = gold_files(inst["patch"])
        res = resolved.get(tid)
        first_val = (o["validate"] or {}).get("passed")
        final_val = (o["repair"] or {}).get("passed") if o["repair"] else first_val
        verdict = (o["review"] or {}).get("verdict")
        ok = {
            "prepare": bool(c["prepare"].get("passed")),
            "prefetch": bool(gold) and gold <= {x["path"] for x in (o["prefetch"] or {}).get("source_candidates", [])},
            "reproduce": bool(validity.get(tid, {}).get("valid")),
            "implement": bool(first_val),
            "repair": bool(final_val),
            "review": (verdict == "approve") == bool(res) if verdict in ("approve", "request_changes") else False,
            "emit_patch": bool(c["emit_patch"].get("passed")),
        }
        first_fail = next((n for n in NODE_ORDER if n in ok and not ok[n]), None)
        rows[tid] = {"resolved": res, "ok": ok, "attributed_to": None if res else (first_fail or "hidden_tests"),
                     "validate_first": first_val, "validate_final": final_val, "review": verdict,
                     "repair_ran": bool(o["repair"] and (o["repair"].get("rounds") or 0) > 0),
                     "repro": validity.get(tid)}
    n = len(rows) or 1
    failed_first = [r for r in rows.values() if r["validate_first"] is False]
    reviewed = [r for r in rows.values() if r["review"] in ("approve", "request_changes")]
    summary = {
        "tasks": len(rows),
        "resolved_rate": sum(bool(r["resolved"]) for r in rows.values()) / n,
        "prefetch_hit_rate": sum(r["ok"]["prefetch"] for r in rows.values()) / n,
        "repro_valid_rate": sum(r["ok"]["reproduce"] for r in rows.values()) / n,
        "implement_one_shot_rate": sum(bool(r["validate_first"]) for r in rows.values()) / n,
        "repair_recovery_rate": (sum(bool(r["validate_final"]) for r in failed_first) / len(failed_first))
        if failed_first else None,
        "review_false_reject": sum(r["review"] == "request_changes" and bool(r["resolved"]) for r in reviewed),
        "review_false_accept": sum(r["review"] == "approve" and not r["resolved"] for r in reviewed),
        "review_n": len(reviewed),
        "attribution": {k: sum(r["attributed_to"] == k for r in rows.values())
                        for k in [*NODE_ORDER, "hidden_tests"] if any(r["attributed_to"] == k for r in rows.values())},
    }
    result = {"summary": summary, "tasks": rows}
    (run_dir / "evaluation.json").write_text(json.dumps(result, indent=2))
    return result
