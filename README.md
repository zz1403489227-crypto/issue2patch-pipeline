# issue2patch

An agent pipeline that turns an issue into a patch, and a small service that runs it: label an issue on
GitHub, and a pull request comes back with a patch, the validation results and an independent review.

It runs entirely on a local model (Qwen3.6-35B-A3B-FP8 on two RTX 4090s, served by vLLM) and calls an
off-the-shelf agent harness (Pi) for the steps that need an agent. The work is mostly the engineering
around the agent: a fixed pipeline with per-node checks, ground-truth metrics for every node, retries and
resumable checkpoints, a queue-based service with an idempotent PR exit, a CI gate on real GPUs, and fault
injection.

```
prepare -> prefetch -> reproduce* -> implement* -> validate -> repair* -> review* -> emit_patch
                                         (* = agent node, a fresh agent session each)
```

## Results

On 100 held-out SWE-bench Verified tasks (stratified by repository and difficulty, fixed before any run),
each run 3 times with the final code:

| Metric | Result | 95% interval |
|---|---|---|
| Resolved (hidden tests) | **68.3%** | [60.3, 76.0] |
| Resolved in all 3 runs | 57.0% | |
| Prefetch hit@10 | 70.0% | [61.0, 79.0] |
| Reproduction test valid | 48.7% | [41.3, 55.7] |
| Implement one-shot (first validation passes) | 75.3% | [69.7, 80.7] |
| Review verdict agrees with hidden tests | 73.3% | [65.8, 80.5] |

About 1.4M tokens (mostly cached prefix) and 11 minutes per task; 100 tasks at concurrency 8 take about 2.5
hours on two RTX 4090s. Details: [docs/reports/stage-3.md](docs/reports/stage-3.md) (Chinese) and
[docs/article.md](docs/article.md).

Three measured improvements, each re-tested on the node it changed, task by task:

| Case | Node | Change | Measured |
|---|---|---|---|
| 1 | reproduce | submit-only final turns; keep helper files the test imports | valid reproduction tests 42.0% -> 51.3% (paired +9.3 [+3.0, +15.7]) |
| 2 | prefetch | specificity-weighted scoring | hit@10 57% -> 70% on dev, 70% on held-out |
| 3 | implement | flat `edit(path, oldText, newText)` for this model | format errors 3.4% -> 0, nothing downstream improved: **not adopted** |


## How it works

- **A deterministic state machine, not a manager agent.** Code runs the eight nodes in a fixed order. Agents
  are called inside a node for one job and return structured output through a `submit` tool with a schema.
  Nodes pass only declared fields, so no agent inherits another agent's context; the reviewer never sees
  the implementer's reasoning.
- **Every node has a deterministic check and a metric with computed ground truth** (prefetch hit@10,
  reproduction-test validity, implement one-shot, repair recovery, review agreement, validation false
  alarms). A regression shows up on a node, and a change to one node can be re-tested on that node alone
  from stored inputs (`i2p rerun-node`).
- **Sandboxed agents.** Each task runs in its SWE-bench container with networking off. The Pi process runs
  inside the container; its only way out is a Unix socket to a model gateway on the host, which meters
  every request and enforces the timeout and the token budget.
- **Reliability.** Three failure classes (infrastructure: snapshot restore and retry; model format: feedback
  in the same session; validation: the repair node), three timeout layers (model call, node, task), a
  per-task token budget, checkpoints after every node and `resume` after a crash.
- **Service.** HTTP API and a signed GitHub webhook; Postgres as the source of truth; RabbitMQ with manual
  ack, prefetch 1 (backpressure), delivery limit and dead-letter queue, strict priority; per-user quota;
  cancellation; an idempotent PR exit (a worker killed right after opening a PR does not cause a second PR).
- **CI gate** on a self-hosted runner next to the GPUs: unit tests and a deterministic node metric that must
  not get worse, then the full pipeline on 20 canary tasks with a floor on the noisy end-to-end number.
- **Observability.** Self-hosted Langfuse; each task is a trace with nodes, model calls and tool calls, and
  node ground truth as scores.

The full design, with the reasons for each choice, is in [docs/design.md](docs/design.md). How the numbers
were produced, and what changed them, is in [docs/article.md](docs/article.md).

## Layout

| Path | What |
|---|---|
| `src/issue2patch/pipeline.py` | Orchestrator: node order, retries, timeouts, budget, checkpoints, resume |
| `src/issue2patch/nodes/` | The eight nodes |
| `src/issue2patch/contracts.py` | Node input and output contracts |
| `src/issue2patch/gateway.py` | Model gateway: Unix socket to vLLM, metering, budget, fault injection |
| `src/issue2patch/sandbox.py` | Docker sandbox (network off), snapshots |
| `src/issue2patch/harness/`, `harness/pi/` | Harness interface; the Pi adapter and the runner that runs inside the sandbox |
| `src/issue2patch/evaluate.py`, `funnel.py` | Grading, node metrics, failure attribution, bootstrap intervals |
| `src/issue2patch/state.py`, `state_pg.py` | State store: files, Postgres |
| `src/issue2patch/service.py`, `api.py`, `exit_pr.py` | Worker, HTTP API and webhook, PR exit |
| `deploy/` | Docker Compose for Postgres + RabbitMQ and for Langfuse |
| `scripts/` | Model server, CI gate, fault injection, paired comparison, prefetch evaluation |
| `data/` | Task splits, acceptance results, report set, CI reference values |
| `docs/` | Design, decision records, stage reports (Chinese), article |

The stage reports and decision records refer to the project's original plan (方案), which is not
published; [docs/design.md](docs/design.md) covers what it decided.

## Running it

Requirements: Linux with Docker, two 24 GB GPUs for the model, the SWE-bench Verified images for the tasks
you want to run, Python 3.12 and Node.js 22.

```bash
python -m venv .venv && .venv/bin/pip install -e .
(cd harness/pi && npm ci)
scripts/serve_vllm.sh                                # vLLM, Qwen3.6-35B-A3B-FP8, TP=2, thinking on
.venv/bin/i2p export-data                            # SWE-bench Verified -> var/data
.venv/bin/i2p smoke --task pytest-dev__pytest-6202   # one task end to end
.venv/bin/i2p run --tasks <id,id,...> --run-id r1 --parallel 8
.venv/bin/i2p resume --run-id r1                     # after an interruption
.venv/bin/i2p evaluate --run-id r1                   # hidden tests, node metrics, failure attribution
.venv/bin/i2p funnel --run-ids r1,r2,r3              # metrics with 95% intervals across runs
.venv/bin/i2p rerun-node --from-run r1 --node implement --until validate --run-id r1-impl
```

The service:

```bash
docker compose -f deploy/service/docker-compose.yml up -d
.venv/bin/i2p service-setup                          # schema and queues
.venv/bin/i2p worker &                               # one per concurrent task
.venv/bin/i2p api                                    # 127.0.0.1; POST /tasks, GET /tasks/{id}, POST /webhooks/github
```

Secrets (database and queue passwords, the webhook secret, the GitHub token) live in files under `var/`,
which is not in the repository. Machine-specific settings go in `var/local.env` as `KEY=VALUE` lines, for
example `I2P_NODE_HOME` (the Node.js install mounted into task containers; defaults to the `node` on
`PATH`). The CI runner needs `I2P_MAIN`, the path of the deployed checkout, in its `.env` file. The CI job runs only
in the private development repository: a self-hosted runner with lab GPUs must not run code from public
pull requests.

## Limitations

- The tasks are single-repository Python issue fixes (SWE-bench Verified). That says little about
  multi-repository work or proprietary codebases.
- One local model only. The numbers would move with a different model, and some fixes here (the flat edit
  tool) are specific to how this model calls tools.
- 100 tasks x 3 runs: end-to-end numbers show the broad level, not fine differences. Differences under 5
  points are not interpreted.
- No controlled comparison with other systems, so no claim that this pipeline beats anything.
- The service was verified on one machine with several workers, not under real multi-user load. The
  sandbox repository used for the GitHub demo is a demo: the commit that planted its bugs is visible in its
  history.
- Each repository needs a prepared environment image (`data/repos.json`) before the service can take issues
  for it.
