"""Where does the pipeline lose tasks? Node-by-node breakdown over one or more runs.

usage: python scripts/analyze_runs.py base-r1 base-r2 base-r3
"""
import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
inst = {json.loads(l)["instance_id"]: json.loads(l) for l in open(ROOT / "var/data/swebench_verified.jsonl")}


def load(p):
    return json.loads(p.read_text()) if p.exists() else None


def gold_files(i):
    return set(re.findall(r"^diff --git a/(\S+) b/", inst[i]["patch"], flags=re.M))


agent_status = collections.Counter()
repro_modes = collections.Counter()
prefetch = collections.Counter()
review = collections.Counter()
shim = collections.Counter()
nodes_cost = collections.defaultdict(lambda: collections.Counter())
for run in sys.argv[1:]:
    ev = load(RUNS / run / "evaluation.json")["tasks"]
    val = load(RUNS / run / "repro_validity.json") or {}
    for d in sorted((RUNS / run / "tasks").iterdir()):
        t = d.name
        e = ev.get(t, {})
        for nd in d.glob("nodes/*"):
            m = load(nd / "metrics.json") or {}
            node = nd.name.split("-", 1)[1]
            for ar in m.get("agent_runs", []):
                agent_status[(node, ar["status"])] += 1
            mu = m.get("model") or {}
            nodes_cost[node]["prompt"] += mu.get("prompt_tokens", 0)
            nodes_cost[node]["completion"] += mu.get("completion_tokens", 0)
            nodes_cost[node]["seconds"] += m.get("seconds", 0)
            nodes_cost[node]["n"] += 1
            for f in nd.glob("agent/*/events.jsonl"):
                for line in f.read_text(errors="replace").splitlines():
                    if '"arg_repair' in line or '"output_capped"' in line or '"submit_nudge"' in line \
                            or '"budget_warning"' in line:
                        shim[json.loads(line)["type"]] += 1
        # reproduce failure modes
        ro = load(d / "nodes/3-reproduce/output.json") or {}
        v = val.get(t) or {}
        if not ro.get("submitted"):
            repro_modes["no test submitted"] += 1
        elif not ro.get("fails_on_original"):
            repro_modes["test passes on original code"] += 1
        elif v.get("valid"):
            repro_modes["valid"] += 1
        elif v.get("passes_after") is False:
            repro_modes["fails on original but also fails with the reference patch"] += 1
        else:
            repro_modes[f"other: {v}"] += 1
        # prefetch: miss vs. outcome, and whether the implementation touched the gold files anyway
        hit = e.get("ok", {}).get("prefetch")
        impl = load(d / "nodes/4-implement/output.json") or {}
        touched = bool(set(impl.get("changed_files") or []) & gold_files(t))
        prefetch[(("hit" if hit else "miss"), ("resolved" if e.get("resolved") else "unresolved"),
                  ("impl touched gold file" if touched else "impl missed gold files"))] += 1
        review[(e.get("review"), "resolved" if e.get("resolved") else "unresolved")] += 1

print("## Agent status per node")
for (n, s), c in sorted(agent_status.items()):
    print(f"  {n:10s} {s:24s} {c}")
print("\n## Reproduce outcomes")
for k, c in repro_modes.most_common():
    print(f"  {c:4d}  {k}")
print("\n## Prefetch hit/miss vs outcome")
for k, c in sorted(prefetch.items()):
    print(f"  {c:4d}  {k}")
print("\n## Review verdict vs hidden tests")
for k, c in sorted(review.items(), key=str):
    print(f"  {c:4d}  {k}")
print("\n## Harness events (edit shim, output cap, submit nudge, budget warning)")
for k, c in shim.items():
    print(f"  {k}: {c}")
print("\n## Cost per node (mean per task-run)")
for n, c in nodes_cost.items():
    k = c["n"] or 1
    print(f"  {n:10s} prompt {c['prompt']/k:>10,.0f}  completion {c['completion']/k:>8,.0f}  min {c['seconds']/k/60:5.1f}")
