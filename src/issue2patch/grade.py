"""Grading outside the pipeline: hidden tests via the official SWE-bench harness (swebench 4.1.0).

Also used for task acceptance: the reference patch must resolve a task and a no-op patch must not.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

MODEL_NAME = "issue2patch"
# A patch that applies cleanly and changes no behaviour (an empty prediction would be skipped by the harness).
NOOP_PATCH = ("diff --git a/i2p_noop.txt b/i2p_noop.txt\nnew file mode 100644\nindex 0000000..e69de29\n"
              "--- /dev/null\n+++ b/i2p_noop.txt\n@@ -0,0 +1 @@\n+noop\n")


def run_swebench(preds: list[dict], out_dir: Path, run_id: str, data_file: str, workers: int,
                 timeout: int = 1800) -> dict[str, bool]:
    out_dir.mkdir(parents=True, exist_ok=True)
    preds_path = out_dir / "predictions.jsonl"
    preds_path.write_text("".join(json.dumps(p) + "\n" for p in preds))
    ids = [p["instance_id"] for p in preds]
    cmd = [sys.executable, "-m", "issue2patch.swebench_main", "--dataset_name", data_file,
           "--split", "test", "--predictions_path", str(preds_path), "--max_workers", str(workers),
           "--run_id", run_id, "--namespace", "swebench", "--cache_level", "instance",
           "--timeout", str(timeout), "--report_dir", str(out_dir), "--instance_ids", *ids]
    with open(out_dir / "harness.log", "w") as log:
        subprocess.run(cmd, cwd=out_dir, stdout=log, stderr=subprocess.STDOUT, check=False)
    resolved: dict[str, bool] = {}
    for p in preds:
        i, model = p["instance_id"], p["model_name_or_path"]
        rep = out_dir / "logs" / "run_evaluation" / run_id / model / i / "report.json"
        resolved[i] = bool(json.loads(rep.read_text()).get(i, {}).get("resolved")) if rep.exists() else False
    (out_dir / "resolved.json").write_text(json.dumps(resolved, indent=2))
    return resolved


def grade(run_dir: Path, data_file: str, workers: int = 4) -> dict[str, bool]:
    preds = []
    for task_dir in sorted((run_dir / "tasks").iterdir()):
        patch = task_dir / "nodes" / "8-emit_patch" / "patch.diff"
        preds.append({"instance_id": task_dir.name, "model_name_or_path": MODEL_NAME,
                      "model_patch": patch.read_text() if patch.exists() else ""})
    return run_swebench(preds, run_dir / "grading", f"i2p-{run_dir.name}", data_file, workers)


def accept(instances: dict[str, dict], ids: list[str], out_dir: Path, data_file: str,
           workers: int, part: str = "all") -> dict[str, dict]:
    """Reference patch must resolve; a no-op patch must not. Returns per-task verdicts.

    part: "gold" or "noop" runs one half (the two can run in parallel; the harness skips instances
    it already evaluated under the same run id), "collect" only reads the reports, "all" does both.
    """
    def preds(model: str) -> list[dict]:
        return [{"instance_id": i, "model_name_or_path": model,
                 "model_patch": instances[i]["patch"] if model == "gold" else NOOP_PATCH} for i in ids]

    if part in ("all", "gold"):
        run_swebench(preds("gold"), out_dir / "gold", "accept-gold", data_file, workers)
    if part in ("all", "noop"):
        run_swebench(preds("noop"), out_dir / "noop", "accept-noop", data_file, workers)
    if part in ("gold", "noop"):
        return {}

    def collected(model: str, run_id: str) -> dict[str, bool]:
        res = {}
        for i in ids:
            rep = out_dir / model / "logs" / "run_evaluation" / run_id / model / i / "report.json"
            res[i] = bool(json.loads(rep.read_text()).get(i, {}).get("resolved")) if rep.exists() else None
        return res

    gold, noop = collected("gold", "accept-gold"), collected("noop", "accept-noop")
    missing = [i for i in ids if gold[i] is None or noop[i] is None]
    if missing:
        print(f"warning: {len(missing)} instances have no report yet (counted as not accepted)")
    verdicts = {i: {"gold_resolves": gold[i], "noop_resolves": noop[i],
                    "accepted": gold[i] is True and noop[i] is False} for i in ids}
    (out_dir / "acceptance.json").write_text(json.dumps(verdicts, indent=2))
    return verdicts
