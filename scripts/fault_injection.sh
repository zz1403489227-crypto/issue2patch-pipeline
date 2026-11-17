#!/usr/bin/env bash
# Stage 3 fault injection, pipeline part. Each scenario runs one task with one injected fault.
# (Killing the orchestrator and killing a worker were tested in stages 1 and 2; the database outage is
#  a service test, see docs/reports/fault-injection.md.)
set -uo pipefail
cd "$(dirname "$0")/.."
T=pytest-dev__pytest-6202
# 1. the 3rd model request of every agent node hangs past the gateway's 30 s timeout
I2P_FAULT=model_timeout@3 I2P_REQUEST_TIMEOUT_S=30 ./.venv/bin/i2p run --tasks $T --run-id fi-timeout &
# 2. the 2nd model request "returns" a bash tool call whose arguments are not valid JSON
I2P_FAULT=bad_tool_call@2 ./.venv/bin/i2p run --tasks $T --run-id fi-badtool &
# 3. a task that runs out of its token budget (300k instead of 6M)
I2P_TASK_TOKEN_BUDGET=300000 ./.venv/bin/i2p run --tasks django__django-11433 --run-id fi-budget &
wait
echo FI_DONE
