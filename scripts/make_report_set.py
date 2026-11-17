"""Fix the report set before any run on it (stage 3 final report).

100 tasks drawn from the 288 held-out tasks, stratified by repo x difficulty (same rule as the dev set),
seed 0. Written once to data/report-set.json; the final report runs every task 3 times, once.
usage: python scripts/make_report_set.py
"""
import json
import random
from pathlib import Path

from issue2patch.config import Settings
from issue2patch.tasks import load_instances, stratified_sample

ROOT = Path(__file__).resolve().parents[1]
out = ROOT / "data/report-set.json"
if out.exists():
    raise SystemExit(f"{out} exists; the report set is fixed once")
held = json.loads((ROOT / "data/splits.json").read_text())["held_out"]
inst = load_instances(Settings().data_file)
key = lambda i: f"{inst[i]['repo']}|{inst[i].get('difficulty')}"  # noqa: E731
ids = sorted(stratified_sample(held, key, 100, random.Random(0)))
out.write_text(json.dumps({"seed": 0, "from": "held_out (data/splits.json)", "strata": "repo x difficulty",
                           "tasks": ids}, indent=2) + "\n")
by_repo: dict[str, int] = {}
for i in ids:
    by_repo[inst[i]["repo"]] = by_repo.get(inst[i]["repo"], 0) + 1
print(len(ids), "tasks ->", out)
print(json.dumps(by_repo, indent=1))
