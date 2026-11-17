"""Task data: SWE-bench Verified instances -> TaskSpec.

The full instance (with reference patch and hidden tests) stays on the host and
is only read by grading/metrics code. The pipeline receives a TaskSpec.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .contracts import TaskSpec

HIDDEN_FIELDS = ("patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS", "hints_text")


def image_for(instance_id: str) -> str:
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_').lower()}:latest"


def export_verified(out: Path) -> int:
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    from datasets import load_dataset

    ds = load_dataset("SWE-bench/SWE-bench_Verified", split="test")
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:
        for row in ds:
            f.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
    return len(ds)


def load_instances(path: str | Path) -> dict[str, dict[str, Any]]:
    with open(path) as f:
        rows = [json.loads(line) for line in f if line.strip()]
    return {r["instance_id"]: r for r in rows}


def local_images() -> set[str]:
    out = subprocess.run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                         capture_output=True, text=True, check=True).stdout
    return {line.strip() for line in out.splitlines() if line.startswith("swebench/sweb.eval")}


def to_task(inst: dict[str, Any]) -> TaskSpec:
    return TaskSpec(
        task_id=inst["instance_id"],
        repo=inst["repo"],
        version=str(inst["version"]),
        base_commit=inst["base_commit"],
        issue=inst["problem_statement"],
        image=inst.get("image") or image_for(inst["instance_id"]),
        setup_patch_file=inst.get("setup_patch_file"),
        expected_tree=inst.get("expected_tree"),
        source=inst.get("source"),
    )


def stratified_sample(ids: list[str], key, n: int, rng) -> list[str]:
    """n items, allocated across strata in proportion to their size (largest remainder), random within."""
    from collections import defaultdict

    strata: dict[Any, list[str]] = defaultdict(list)
    for i in sorted(ids):
        strata[key(i)].append(i)
    total = len(ids)
    exact = {k: n * len(v) / total for k, v in strata.items()}
    quota = {k: int(x) for k, x in exact.items()}
    for k in sorted(strata, key=lambda k: (-(exact[k] - quota[k]), str(k)))[: n - sum(quota.values())]:
        quota[k] += 1
    out: list[str] = []
    for k in sorted(strata, key=str):
        members = strata[k][:]
        rng.shuffle(members)
        out += members[: quota[k]]
    return sorted(out)


def _strata(ids, key):
    from collections import defaultdict

    strata: dict[Any, list[str]] = defaultdict(list)
    for i in sorted(ids):
        strata[key(i)].append(i)
    return strata


def _quotas(strata, n):
    total = sum(len(v) for v in strata.values())
    exact = {k: n * len(v) / total for k, v in strata.items()}
    quota = {k: int(x) for k, x in exact.items()}
    for k in sorted(strata, key=lambda k: (-(exact[k] - quota[k]), str(k)))[: n - sum(quota.values())]:
        quota[k] += 1
    return quota


def dev_plan(instances: dict[str, dict[str, Any]], local_ids: list[str], n_dev: int = 100, seed: int = 0,
             margin: float = 1.3) -> dict[str, Any]:
    """Stratified (repo, difficulty) sampling plan over all local tasks: per stratum a fixed random order
    and a quota. The dev set is the first `quota` *accepted* tasks in each stratum's order, which is a
    stratified random sample of the accepted tasks. Candidates = the head of each order (quota x margin),
    so acceptance can check them first."""
    import math
    import random

    rng = random.Random(seed)
    key = lambda i: f"{instances[i]['repo']}|{instances[i].get('difficulty')}"  # noqa: E731
    strata = _strata(local_ids, key)
    quota = _quotas(strata, n_dev)
    order = {}
    for k in sorted(strata):
        members = strata[k][:]
        rng.shuffle(members)
        order[k] = members
    cands = [i for k in sorted(order) for i in order[k][: math.ceil(quota[k] * margin) + (1 if quota[k] else 0)]]
    return {"seed": seed, "n_dev": n_dev, "quota": quota, "order": order, "candidates": cands}


def pick_dev(plan: dict[str, Any], acceptance: dict[str, dict], instances: dict[str, dict[str, Any]],
             n_canary: int = 20, seed: int = 0) -> dict[str, Any]:
    import random

    dev, short = [], {}
    for k, members in plan["order"].items():
        got = [i for i in members if (acceptance.get(i) or {}).get("accepted")][: plan["quota"][k]]
        dev += got
        if len(got) < plan["quota"][k]:
            short[k] = plan["quota"][k] - len(got)
    rng = random.Random(seed)
    canary = stratified_sample(dev, lambda i: instances[i]["repo"], n_canary, rng)
    return {"seed": plan["seed"], "dev": sorted(dev), "canary": canary, "shortfall": short}


def make_splits(instances: dict[str, dict[str, Any]], acceptance: dict[str, dict], n_dev: int = 100,
                n_canary: int = 20, seed: int = 0) -> dict[str, list[str]]:
    import random

    rng = random.Random(seed)
    accepted = sorted(i for i, v in acceptance.items() if v["accepted"])
    dev = stratified_sample(accepted, lambda i: (instances[i]["repo"], instances[i].get("difficulty")), n_dev, rng)
    canary = stratified_sample(dev, lambda i: instances[i]["repo"], n_canary, rng)
    return {"seed": seed, "dev": dev, "canary": canary,
            "held_out": sorted(set(accepted) - set(dev)),
            "rejected": sorted(i for i, v in acceptance.items() if not v["accepted"])}


def sanitized(inst: dict[str, Any]) -> dict[str, Any]:
    """Instance with every hidden field blanked; safe to build test specs from."""
    clean = dict(inst)
    for k in HIDDEN_FIELDS:
        clean[k] = "[]" if k in ("FAIL_TO_PASS", "PASS_TO_PASS") else ""
    return clean
