#!/usr/bin/env bash
# Closed-loop case 1, iteration 2: keep the repro test's helper files; send "the test did not run" back to
# the agent once. Same measurement as iteration 1: single-node re-test of `reproduce` seeded from each
# baseline run, then the full pipeline on the 20 canary tasks, 3 runs each.
set -uo pipefail
cd "$(dirname "$0")/.."
./.venv/bin/i2p evaluate --run-id cl1-repro-r2 --workers 8   # iteration 1, second round (finished)
for r in 1 2 3; do
  ./.venv/bin/i2p rerun-node --from-run "base-r$r" --node reproduce --run-id "cl2-repro-r$r" --parallel 8
  ./.venv/bin/i2p resume --run-id "cl2-repro-r$r" --parallel 8
done
CANARY=$(python3 -c 'import json; print(",".join(json.load(open("data/splits.json"))["canary"]))')
for r in 1 2 3; do
  ./.venv/bin/i2p run --tasks "$CANARY" --run-id "cl2-canary-r$r" --parallel 8
  ./.venv/bin/i2p resume --run-id "cl2-canary-r$r" --parallel 8
done
for r in 1 2 3; do
  ./.venv/bin/i2p evaluate --run-id "cl2-repro-r$r" --workers 8
  ./.venv/bin/i2p evaluate --run-id "cl2-canary-r$r" --workers 8
  ./.venv/bin/i2p export --run-id "cl2-canary-r$r" || true
done
./.venv/bin/i2p funnel --run-ids cl2-canary-r1,cl2-canary-r2,cl2-canary-r3
echo CL2_DONE
