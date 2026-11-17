#!/usr/bin/env bash
# CI gate, run by the self-hosted runner on the lab server for every push to main and every pull request.
# Stage A (minutes, deterministic): unit tests + the prefetch node metric on the canary tasks.
# Stage B (~30 min): the full pipeline on the 20 canary tasks, graded with the hidden tests.
# Gates are chosen so that noise cannot block a change (spec: "CI 怎么避免误拦"):
#   deterministic node metrics must not get worse at all; the noisy end-to-end number only has a floor.
set -euo pipefail
WS=$(pwd)
MAIN=${I2P_MAIN:?set I2P_MAIN to the deployed checkout (venv, model, data, node_modules), e.g. in the .env file of the runner}
export PYTHONPATH="$WS/src"                          # the code under test, ahead of the venv's editable install
export I2P_RUNS_DIR="$MAIN/runs/ci"
ln -sfn "$MAIN/var" "$WS/var"
# node_modules must be real files, not a symlink: harness/pi is bind-mounted into each task container,
# where a symlink to a host path would dangle (first CI run: ERR_MODULE_NOT_FOUND). Hard links are instant.
rm -rf "$WS/harness/pi/node_modules" && cp -al "$MAIN/harness/pi/node_modules" "$WS/harness/pi/node_modules"
PY="$MAIN/.venv/bin/python"
NODE="$("$PY" -c 'from issue2patch.config import Settings; print(Settings().node_home)')/bin/node"
REF=$(cat "$WS/data/ci-reference.json")
SHA=${GITHUB_SHA:-local}; SHORT=${SHA:0:8}

echo "== A1 unit tests"
"$PY" -m pytest -q "$WS/tests"
(cd "$WS/harness/pi" && "$NODE" --test edit_shim.test.mjs output_cap.test.mjs)

echo "== A2 prefetch on the canary tasks (deterministic node metric)"
"$PY" "$WS/scripts/prefetch_eval.py" canary --out "$WS/prefetch-canary.json" | tee prefetch.txt
HITS=$("$PY" -c "import json; print(sum(v['hit10'] for v in json.load(open('$WS/prefetch-canary.json')).values()))")
MIN_HITS=$("$PY" -c "import json; print(json.loads('''$REF''')['canary_prefetch_hits'])")
echo "prefetch hit@10 on canary: $HITS (reference $MIN_HITS)"
[ "$HITS" -ge "$MIN_HITS" ] || { echo "::error::prefetch regression: $HITS < $MIN_HITS"; exit 1; }

if [ "${CI_SKIP_CANARY:-0}" = "1" ]; then echo "canary skipped"; exit 0; fi
echo "== B canary: full pipeline on 20 tasks, graded"
RUN="ci-$SHORT-$(date +%s)"
CANARY=$("$PY" -c "import json; print(','.join(json.load(open('$WS/data/splits.json'))['canary']))")
"$MAIN/.venv/bin/i2p" run --tasks "$CANARY" --run-id "$RUN" --parallel 8
"$MAIN/.venv/bin/i2p" evaluate --run-id "$RUN" --workers 8 > eval.json
RES=$("$PY" -c "import json; d=json.load(open('$I2P_RUNS_DIR/$RUN/evaluation.json'))['tasks']; print(sum(bool(v['resolved']) for v in d.values()))")
INFRA=$("$PY" -c "import json; r=json.load(open('$I2P_RUNS_DIR/$RUN/results.json')); print(sum(x['status']=='infra_error' for x in r))")
FLOOR=$("$PY" -c "import json; print(json.loads('''$REF''')['canary_resolved_floor'])")
echo "canary resolved: $RES/20 (floor $FLOOR), infra errors: $INFRA, run $RUN"
[ "$INFRA" -eq 0 ] || { echo "::error::$INFRA infrastructure errors on the canary"; exit 1; }
[ "$RES" -ge "$FLOOR" ] || { echo "::error::canary resolved $RES/20 is below the floor $FLOOR"; exit 1; }
echo "CI gate passed"
