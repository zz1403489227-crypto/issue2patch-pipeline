#!/usr/bin/env bash
# Stage 3 final report: every task of the report set (data/report-set.json, 100 held-out tasks fixed before
# any run) 3 times, once; then grading, node metrics, Langfuse export and the funnel. Same steps as the
# baseline (scripts/run_baseline.sh), different task set.
# Usage: scripts/run_report.sh [prefix=final] [parallel=8]
set -uo pipefail
cd "$(dirname "$0")/.."
PREFIX=${1:-final}
PAR=${2:-8}
TASKS=$(python3 -c 'import json; print(",".join(json.load(open("data/report-set.json"))["tasks"]))')
git rev-parse HEAD > "var/logs/$PREFIX.commit"
for r in 1 2 3; do
  ./.venv/bin/i2p run --tasks "$TASKS" --run-id "$PREFIX-r$r" --parallel "$PAR"
  ./.venv/bin/i2p resume --run-id "$PREFIX-r$r" --parallel "$PAR"
done
for r in 1 2 3; do
  ./.venv/bin/i2p evaluate --run-id "$PREFIX-r$r" --workers 8
  ./.venv/bin/i2p report --run-id "$PREFIX-r$r" > /dev/null
  ./.venv/bin/i2p export --run-id "$PREFIX-r$r" || echo "export to Langfuse failed for $PREFIX-r$r"
done
./.venv/bin/i2p funnel --run-ids "$PREFIX-r1,$PREFIX-r2,$PREFIX-r3"
echo REPORT_DONE
