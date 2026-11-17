"""Funnel and node metrics across one or more runs of the same task set (e.g. baseline = 3 runs).

Statistics follow the spec's three rules: every task is run 3 times and numbers come with an interval
(bootstrap over tasks of the per-task mean, 95%); comparisons between versions are paired per task;
changes within 5 points are not interpreted.
"""
from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

from .state import NODE_ORDER

METRICS = [  # (name, how to compute per task-run from evaluation.json rows; None = not applicable)
    ("resolved", lambda r: bool(r["resolved"])),
    ("prefetch_hit", lambda r: r["ok"]["prefetch"]),
    ("repro_valid", lambda r: r["ok"]["reproduce"]),
    ("implement_one_shot", lambda r: bool(r["validate_first"])),
    ("repair_recovery", lambda r: bool(r["validate_final"]) if r["validate_first"] is False else None),
    ("review_agrees", lambda r: r["ok"]["review"] if r["review"] in ("approve", "request_changes") else None),
]


def bootstrap_ci(per_task: list[float], n: int = 2000, seed: int = 0) -> tuple[float, float]:
    if not per_task:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(per_task) for _ in per_task) / len(per_task) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def load_runs(runs_dir: Path, run_ids: list[str]) -> dict[str, dict[str, Any]]:
    out = {}
    for rid in run_ids:
        p = runs_dir / rid / "evaluation.json"
        out[rid] = json.loads(p.read_text())["tasks"] if p.exists() else {}
    return out


def summarize(runs_dir: Path, run_ids: list[str]) -> dict[str, Any]:
    runs = load_runs(runs_dir, run_ids)
    tasks = sorted(set().union(*[set(r) for r in runs.values()]))
    result: dict[str, Any] = {"runs": run_ids, "tasks": len(tasks), "metrics": {}}
    for name, fn in METRICS:
        per_task = []
        for t in tasks:
            vals = [fn(runs[r][t]) for r in run_ids if t in runs[r]]
            vals = [v for v in vals if v is not None]
            if vals:
                per_task.append(sum(vals) / len(vals))
        lo, hi = bootstrap_ci(per_task)
        result["metrics"][name] = {"mean": sum(per_task) / len(per_task) if per_task else None,
                                   "ci95": [lo, hi], "n_tasks": len(per_task)}
    all3 = [t for t in tasks if all(t in runs[r] for r in run_ids)]
    result["stability_all_runs_resolved"] = (sum(all(runs[r][t]["resolved"] for r in run_ids) for t in all3)
                                             / len(all3)) if all3 else None
    attr: dict[str, int] = defaultdict(int)
    for r in run_ids:
        for row in runs[r].values():
            if row.get("attributed_to"):
                attr[row["attributed_to"]] += 1
    result["attribution"] = dict(sorted(attr.items(), key=lambda kv: NODE_ORDER.index(kv[0])
                                        if kv[0] in NODE_ORDER else 99))
    rev = defaultdict(int)
    for r in run_ids:
        for row in runs[r].values():
            if row["review"] in ("approve", "request_changes"):
                rev[(row["review"], bool(row["resolved"]))] += 1
    result["review_confusion"] = {f"{k[0]}|{'resolved' if k[1] else 'unresolved'}": v for k, v in rev.items()}
    return result


def to_markdown(s: dict[str, Any]) -> str:
    lines = [f"# Funnel over runs {', '.join(s['runs'])} ({s['tasks']} tasks)", "",
             "| metric | mean | 95% interval (bootstrap over tasks) | tasks |", "|---|---|---|---|"]
    for name, m in s["metrics"].items():
        if m["mean"] is None:
            continue
        lines.append(f"| {name} | {m['mean']:.1%} | [{m['ci95'][0]:.1%}, {m['ci95'][1]:.1%}] | {m['n_tasks']} |")
    if s.get("stability_all_runs_resolved") is not None:
        lines += ["", f"Stability (resolved in every run): {s['stability_all_runs_resolved']:.1%}"]
    lines += ["", "Failure attribution (first node whose truth check failed, pooled over runs):", ""]
    lines += [f"- {k}: {v}" for k, v in s["attribution"].items()]
    lines += ["", "Review verdict vs hidden tests (pooled): " +
              ", ".join(f"{k} {v}" for k, v in sorted(s["review_confusion"].items()))]
    return "\n".join(lines) + "\n"
