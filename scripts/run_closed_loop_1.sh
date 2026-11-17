#!/usr/bin/env bash
# Closed-loop case 1: finalization turns (only `submit` stays enabled for the last 3 agent turns).
# (a) single-node re-test of `reproduce` on the dev set, seeded from each baseline run (paired per task);
# (b) regression check: the full pipeline on the 20 canary tasks, 3 runs, against the same tasks in the baseline.
set -uo pipefail
cd "$(dirname "$0")/.."
for r in 1 2 3; do
  ./.venv/bin/i2p rerun-node --from-run "base-r$r" --node reproduce --run-id "cl1-repro-r$r" --parallel 8
  ./.venv/bin/i2p resume --run-id "cl1-repro-r$r" --parallel 8
done
CANARY=$(python3 -c 'import json; print(",".join(json.load(open("data/splits.json"))["canary"]))')
for r in 1 2 3; do
  ./.venv/bin/i2p run --tasks "$CANARY" --run-id "cl1-canary-r$r" --parallel 8
  ./.venv/bin/i2p resume --run-id "cl1-canary-r$r" --parallel 8
done
for r in 1 2 3; do
  ./.venv/bin/i2p evaluate --run-id "cl1-repro-r$r" --workers 8
  ./.venv/bin/i2p evaluate --run-id "cl1-canary-r$r" --workers 8
  ./.venv/bin/i2p export --run-id "cl1-canary-r$r" || true
done
./.venv/bin/i2p funnel --run-ids cl1-canary-r1,cl1-canary-r2,cl1-canary-r3
echo CL1_DONE
