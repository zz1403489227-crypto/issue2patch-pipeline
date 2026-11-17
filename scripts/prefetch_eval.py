"""Offline evaluation of the prefetch node (deterministic): hit@10 and gold-file ranks on a task set.

usage: python scripts/prefetch_eval.py [dev|canary] [--out FILE]
Starts each task's container (no network), runs nodes.prefetch.rank on the issue, compares with the files
the reference patch changes. Only this script reads the reference patch.
"""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from issue2patch.config import Settings
from issue2patch.nodes.prefetch import rank
from issue2patch.sandbox import DockerSandbox
from issue2patch.tasks import image_for, load_instances

ROOT = Path(__file__).resolve().parents[1]
which = sys.argv[1] if len(sys.argv) > 1 else "dev"
out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
ids = json.loads((ROOT / "data/splits.json").read_text())[which]
inst = load_instances(Settings().data_file)
s = Settings()


def one(i):
    gold = set(re.findall(r"^diff --git a/(\S+) b/", inst[i]["patch"], flags=re.M))
    sb = DockerSandbox.create(image_for(i), f"i2p-pfe-{i.replace('__', '-')}"[:120], [], "2", "4g")
    try:
        files = [f for f in sb.git("ls-files").output.splitlines() if f.endswith(".py")]
        src, _, symbols, paths = rank(sb, inst[i]["problem_statement"], files)
    finally:
        sb.destroy()
    order = [c.path for c in src]
    ranks = {g: (order.index(g) + 1 if g in order else None) for g in gold}
    return i, {"hit10": all(r is not None and r <= 10 for r in ranks.values()), "ranks": ranks,
               "n_candidates": len(order), "symbols": symbols, "paths": paths}


with ThreadPoolExecutor(8) as pool:
    res = dict(pool.map(one, ids))
hit = sum(r["hit10"] for r in res.values())
print(f"{which}: hit@10 {hit}/{len(res)} = {hit / len(res):.1%}")
miss = {k: v for k, v in res.items() if not v["hit10"]}
cats = {"no candidates at all": 0, "gold file not ranked": 0, "gold file ranked below 10": 0}
for v in miss.values():
    if v["n_candidates"] == 0:
        cats["no candidates at all"] += 1
    elif any(r is None for r in v["ranks"].values()):
        cats["gold file not ranked"] += 1
    else:
        cats["gold file ranked below 10"] += 1
print("misses:", cats)
if out:
    Path(out).write_text(json.dumps(res, indent=2))
