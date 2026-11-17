#!/usr/bin/env bash
# Stage 1 baseline: every dev task 3 times, then grading, node metrics, Langfuse export and the funnel.
# Usage: scripts/run_baseline.sh [prefix=base] [parallel=8]
set -uo pipefail
cd "$(dirname "$0")/.."
PREFIX=${1:-base}
PAR=${2:-8}
DEV=$(python3 -c 'import json; print(",".join(json.load(open("data/splits.json"))["dev"]))')
for r in 1 2 3; do
  ./.venv/bin/i2p run --tasks "$DEV" --run-id "$PREFIX-r$r" --parallel "$PAR"
  # resume picks up anything an infrastructure failure left unfinished
  ./.venv/bin/i2p resume --run-id "$PREFIX-r$r" --parallel "$PAR"
done
for r in 1 2 3; do
  ./.venv/bin/i2p evaluate --run-id "$PREFIX-r$r" --workers 8
  ./.venv/bin/i2p report --run-id "$PREFIX-r$r" > /dev/null
  ./.venv/bin/i2p export --run-id "$PREFIX-r$r" || echo "export to Langfuse failed for $PREFIX-r$r"
done
./.venv/bin/i2p funnel --run-ids "$PREFIX-r1,$PREFIX-r2,$PREFIX-r3"
echo BASELINE_DONE
