"""Paired, per-task comparison of two sets of runs on the same tasks (spec: version comparisons are
paired per task; report a 95% bootstrap interval; changes within 5 points are not interpreted).

usage: python scripts/compare_paired.py --a base-r1,base-r2,base-r3 --b cl1-repro-r1,cl1-repro-r2,cl1-repro-r3 \
           [--tasks canary] [--node reproduce]
"""
import argparse
import collections
import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"

METRICS = {
    "resolved": lambda r: bool(r["resolved"]),
    "repro_valid": lambda r: bool(r["ok"]["reproduce"]),
    "implement_one_shot": lambda r: bool(r["validate_first"]),
    "repair_recovery": lambda r: bool(r["validate_final"]) if r["validate_first"] is False else None,
    "review_agrees": lambda r: r["ok"]["review"] if r["review"] in ("approve", "request_changes") else None,
}


def load(p):
    return json.loads(p.read_text()) if p.exists() else None


def per_task(runs, tasks, fn):
    out = {}
    for t in tasks:
        vals = []
        for r in runs:
            row = (load(RUNS / r / "evaluation.json") or {}).get("tasks", {}).get(t)
            if row is not None:
                v = fn(row)
                if v is not None:
                    vals.append(float(v))
        if vals:
            out[t] = sum(vals) / len(vals)
    return out


def boot(diffs, n=4000, seed=0):
    rng = random.Random(seed)
    ms = sorted(sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(n))
    return ms[int(0.025 * n)], ms[int(0.975 * n) - 1]


def agent_stats(runs, tasks, node):
    c = collections.Counter()
    tok = []
    idx = ["prepare", "prefetch", "reproduce", "implement", "validate", "repair", "review", "emit_patch"].index(node) + 1
    for r in runs:
        for t in tasks:
            m = load(RUNS / r / "tasks" / t / "nodes" / f"{idx}-{node}" / "metrics.json")
            if not m:
                continue
            for ar in m.get("agent_runs", []):
                c[ar["status"]] += 1
            mu = m.get("model") or {}
            tok.append(mu.get("prompt_tokens", 0) + mu.get("completion_tokens", 0))
    return c, (sum(tok) / len(tok) if tok else 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True)
    ap.add_argument("--b", required=True)
    ap.add_argument("--tasks", default="dev", help="dev | canary | comma list")
    ap.add_argument("--node", default=None, help="also print agent statuses and tokens for this node")
    ap.add_argument("--metrics", default="all")
    args = ap.parse_args()
    a, b = args.a.split(","), args.b.split(",")
    splits = json.loads((ROOT / "data" / "splits.json").read_text())
    tasks = splits[args.tasks] if args.tasks in ("dev", "canary") else args.tasks.split(",")
    names = list(METRICS) if args.metrics == "all" else args.metrics.split(",")
    print(f"A = {a}\nB = {b}\ntasks = {args.tasks} ({len(tasks)})\n")
    print("| metric | A | B | B - A (paired) | 95% interval | tasks | verdict |\n|---|---|---|---|---|---|---|")
    for name in names:
        pa, pb = per_task(a, tasks, METRICS[name]), per_task(b, tasks, METRICS[name])
        common = sorted(set(pa) & set(pb))
        if not common:
            continue
        diffs = [pb[t] - pa[t] for t in common]
        lo, hi = boot(diffs)
        ma, mb = sum(pa[t] for t in common) / len(common), sum(pb[t] for t in common) / len(common)
        d = mb - ma
        verdict = "within 5 points: not interpreted" if abs(d) < 0.05 else ("better" if lo > 0 else
                                                                           "worse" if hi < 0 else "inconclusive")
        print(f"| {name} | {ma:.1%} | {mb:.1%} | {d:+.1%} | [{lo:+.1%}, {hi:+.1%}] | {len(common)} | {verdict} |")
    if args.node:
        for label, runs in (("A", a), ("B", b)):
            c, tok = agent_stats(runs, tasks, args.node)
            tot = sum(c.values()) or 1
            print(f"\n{label} {args.node}: " + ", ".join(f"{k} {v} ({v / tot:.0%})" for k, v in c.most_common())
                  + f"; mean tokens per run {tok:,.0f}")


if __name__ == "__main__":
    main()
