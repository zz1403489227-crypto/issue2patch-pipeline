# Design

This document collects the design decisions of issue2patch in one place. The dated originals (in Chinese)
are in [decisions/](decisions/) and the running log in [decision-log.md](decision-log.md); this text is the
consolidated version and links back to them.

## 1. Shape of the system

```
prepare -> prefetch -> reproduce* -> implement* -> validate -> repair* -> review* -> emit_patch
                                         (* = agent node, a fresh agent session each)
```

The order is fixed in code: a deterministic state machine runs the eight nodes, and an agent is only
called inside a node, for one well-defined job. No "manager agent" decides what happens next.

Why not one agent with all the tools: every failure would be one long transcript. With nodes, each step has
its own contract, its own deterministic check, and its own metric whose ground truth can be computed
without a model (section 6), so a regression shows up as a number on a node, not as a vague drop at the end.

| Node | Kind | Job | Deterministic check |
|---|---|---|---|
| prepare | code | start the task container (no network), make the checkout safe, verify the tree | HEAD and tree hash match the task |
| prefetch | code | rank candidate source and test files from the issue text | at least one candidate |
| reproduce | agent | write a test that fails on the current code | the test exists and fails on the original code |
| implement | agent | change the source | the diff is non-empty and does not touch the reproduction test |
| validate | code | lint, reproduction test, related existing tests (baseline-aware) | all pass |
| repair | agent | fix what validation reported, at most two rounds | validation again |
| review | agent | independent review of the final diff (never sees the implement transcript) | structured verdict |
| emit_patch | code | write the source-only patch, check it applies cleanly to the base commit | `git apply --check` |

## 2. Contracts between nodes ([0001](decisions/0001-node-contracts.md))

- Nodes pass only the fields declared in `contracts.py`. Each node's input is stored as `input.json`, which
  is what makes single-node re-tests possible.
- The implement node does not see the reproduce transcript, and the review node does not see the
  implementation reasoning. This is the first layer of context management: no node inherits another
  node's context.
- Agents return structured output through a `submit` tool with a JSON schema, so a malformed answer is
  rejected by tool-argument validation on the spot instead of failing a parser afterwards.
- Code enforces the contracts: the reproduce node may only add test files (anything else is rolled back and
  logged), the implement node cannot edit the reproduction test, and any file change made by the review
  node is reverted.
- Hidden tests, the reference patch and `hints_text` never enter any contract. Only the evaluation code,
  which runs outside the pipeline, reads them.

## 3. Agent harness ([0003](decisions/0003-harness-interface.md))

Agent nodes call one interface, `Harness.run(AgentRequest) -> AgentResult`. The implementation is Pi 1.0.0
(`@earendil-works/pi-coding-agent`, version pinned). Swapping in another harness means writing one adapter;
node code does not change.

```
host                                          task container (--network none)
pipeline -- docker exec -i ----------------->  node /opt/pi/runner.mjs  (config on stdin, events on stdout)
                                                 Pi session: read / bash / edit / write + submit
model gateway (Unix socket) <-- /i2p/gw.sock <-- local TCP forwarder <-- Pi's HTTP client
   |
   +--> vLLM 127.0.0.1 (Qwen3.6-35B-A3B-FP8, thinking on, TP=2 on two RTX 4090)
```

- The whole Pi process runs inside the container, so there is one set of paths (`/testbed`). Running Pi on
  the host and forwarding tools into the container was considered and rejected: read/edit/write would see
  host paths while test output shows container paths.
- The container has no network. Its only way out is the gateway socket, which is where per-request
  accounting, the model-call timeout, the token budget and fault injection live.
- Node.js and Pi are mounted read-only; the SWE-bench images are not modified.
- Each agent node is a new in-memory Pi session. No transcript is left inside the container where a later
  node could read it.
- Thinking is on (Pi's `thinkingFormat: "qwen-chat-template"`, which sends the flags the Qwen3.6 model card
  documents). No other compatibility flags: Pi's docs say to set them only for verified differences.

Pi-specific findings that changed the design:

| Finding | Fix |
|---|---|
| A "please submit now" steer five turns before the limit was ignored in 8 of 8 cases | In the last 3 turns only the `submit` tool stays active (`setActiveToolsByName`) and a submit in the very last turn is no longer aborted. Reproduce-node submit rate 69% -> 95% |
| The model often sent `edit` arguments that failed schema validation (8.7% of edit calls): `edits` as a JSON string with invalid escapes, `path` inside each edit, or flat `oldText/newText` | A shim on Pi's `prepareArguments` hook repairs only what can be checked against the file. Closed-loop case 3 also tried a flat `edit(path, oldText, newText)`: it removed the remaining failures (3.4% -> 0) but improved nothing downstream, so Pi's tool stays the default and the flat one is a switch (`I2P_EDIT_TOOL=flat`) |
| Truncated JSON tool arguments are silently completed by Pi's streaming parser, not rejected | Recorded (fault injection item 5); harmless for `bash`, a lead to check first when an `edit` misbehaves |
| Pi counts an aborted final message as a request | Requests are counted at the gateway, not from the harness's own report |

## 4. Reliability of the orchestrator ([0004](decisions/0004-orchestrator-reliability.md))

**Three kinds of failure, three treatments.**

| Kind | Examples | Treatment |
|---|---|---|
| Infrastructure | Docker error, container gone, model unreachable, Pi process died | Restore the workspace to the snapshot taken when the node started (a git tree), re-run the node, back off 10 s / 20 s, at most 3 attempts. If the container is gone, create a new one and replay the last checkpoint |
| Model output format | Tool arguments failed validation; the agent ended without `submit` | Fed back inside the same session (Pi returns validation errors; a missing submit is asked for once). Re-running the whole node would waste the work |
| Validation | Lint, reproduction test or related tests fail | The repair node, at most two rounds |

Anything else (a bug in our code) is not retried; it is recorded as `infra_error` so it is not hidden.

**Three timeout layers.** Model call: 900 s at the gateway (504). Node: per agent node (reproduce 1500 s,
implement 2100 s, repair 1200 s, review 900 s), test commands 900 s. Task: 3600 s; an agent node gets
`min(node limit, time left)`, and no agent is started with less than 60 s left. Each layer is recorded
separately, so it is always clear whose timeout it was.

**Budget.** 6M tokens per task (input + output as metered by the gateway; the most expensive stage-0 task
used 3M). Checked before each agent start; the gateway rejects new requests with 429 once it is exceeded.
When time or tokens run out, the remaining agent nodes are skipped, but the deterministic `emit_patch`
still runs.

**Checkpoints, resume, idempotency.** After each node the whole workspace, untracked files included, is
saved as a binary diff plus a small cache file. `resume` loads the longest completed prefix, creates a fresh
container, replays the last checkpoint and continues. Finished tasks are skipped; the time budget carries
over across interruptions. Tested by `kill -9` on the orchestrator mid-run: completed nodes were not re-run,
the interrupted implement node ran again from its start, and both tasks finished normally.

**Why not Temporal or LangGraph.** Eight fixed nodes need "save a checkpoint after each node and continue
from it". That is under 300 lines here, and the parts that matter (snapshot rollback, rebuilding a
container, replaying a workspace) are tied to Docker and git details that a general framework would not
help with.

## 5. State ([0002](decisions/0002-state-format.md))

All state goes through one `StateStore` interface. Stages 0-1 used files (`state.json` rewritten atomically
via rename, an append-only `events.jsonl`, per-node `input/output/check/metrics.json`, agent traces, full
command logs). Stage 2 added a Postgres implementation without touching the orchestrator or the nodes.

A node that finished but whose check failed is `done` with `check_passed=false`, which is different from a
node that crashed (`error`). Failure attribution depends on that distinction.

State lives on the host, never inside the sandbox, so a deleted container loses nothing and another worker
can pick the task up.

## 6. Evaluation

- **Task set.** 393 SWE-bench Verified tasks have a local image. Each was accepted only if the reference
  patch passes and a no-op patch fails the hidden tests: 388 passed. Dev set: 100 tasks, stratified by repo
  x difficulty (seed 0). Canary: 20 of the dev tasks, for CI. Held out: 288, not looked at during
  development. The report set is 100 of the held-out tasks, fixed by a script before any run.
- **Grading** uses the official SWE-bench harness (4.1.0) through a thin wrapper that stubs two download
  functions the harness calls for building environment images (it hung on GitHub from the lab server; we
  only grade tasks whose images exist locally).
- **Node metrics with computed ground truth**: prefetch hit@10 (all files of the reference patch in the top
  10); reproduction validity (the agent's test fails on the original code and passes with the reference
  patch); implement one-shot (validation passes without repair); repair recovery; review agreement with the
  hidden tests; false alarms of validation (validation fails but the hidden tests pass).
- **Failure attribution**: the first node whose ground truth is wrong for a failed task.
- **Statistics**: each task 3 times; 95% intervals by bootstrapping over tasks; versions are compared task
  by task (paired). A measured A/A difference was 5 points with an interval spanning 0, so differences
  under 5 points are not interpreted.
- **Single-node re-test**: `i2p rerun-node --from-run R --node N [--until M]` re-runs one node from the stored
  inputs of a previous run, so a change to one node is measured on that node without the noise of the
  others.

## 7. Context management

- Node isolation (section 2) keeps each agent's context to its own job.
- Tool output shown to the model is capped at 12,000 characters: the first 2,000 and the last 8,000 (errors
  and test summaries are usually at the end). The full output is written to a file and the path is given to
  the model. In the baseline 53 outputs were capped; the agent never went back to open the full file.
- `read` is not capped further: the model chooses the line range itself.
- Prefix caching in vLLM hit 92-93% of input tokens, which is what makes many short-context agent turns
  affordable.

## 8. Service ([0005](decisions/0005-service.md))

```
HTTP API / GitHub webhook --(row with status=queued)--> Postgres (tasks, node states, events: the source of truth)
                          --(task id only)-----------> RabbitMQ i2p.tasks (quorum queue)
workers 1..N <--(prefetch 1, manual ack)-- RabbitMQ
             --(read/write state)--> Postgres
             --(logs, traces, workspace checkpoints)--> disk (stands in for object storage)
```

- **Queue settings**: manual ack after the task returns; prefetch 1 (N workers = N concurrent tasks, which
  is the backpressure); `consumer_timeout` 2 h, above the 1 h task limit; `x-delivery-limit` 3 with a
  dead-letter queue. Only infrastructure errors are requeued; every other outcome is acked.
- **Worker threads**: the task runs in its own thread, the main thread only does RabbitMQ I/O so heartbeats
  continue during a 30-minute task; the ack goes back through `add_callback_threadsafe`.
- **Concurrency**: 8 workers = vLLM `max_num_seqs` 8. At 8 concurrent agents vLLM did not preempt and the KV
  cache stayed around 37%.
- **Priority**: strict priority on quorum queues needs RabbitMQ 4.3; on 4.1 it was measured to be a 2:1 mix,
  so the deployment uses 4.3.
- **Cancel** is a flag in Postgres, checked between nodes. **Quota** is per user, summed from gateway
  metering; over quota, submission returns 429.
- **Postgres outages**: the state store reconnects with backoff for up to 3 minutes. Without this, a short
  database restart burned the delivery limit in seconds and dead-lettered the task.
- **The PR exit is idempotent**: the branch name is a function of the task id; under a Postgres advisory
  lock: PR already recorded -> done; an open PR for that branch exists -> record it; otherwise push the
  branch and open the PR, and write back with `WHERE pr_url IS NULL`. A worker killed right after opening a
  PR, before recording it, did not produce a second PR.
- **GitHub from the lab server**: IPv6 does not route and incremental `git fetch` over IPv4 is cut after
  about 90 s, while the REST API is stable. Code sync uses the compare API, publishing uses the Git Data API
  (blob, tree, commit, ref), all over IPv4 with retries.
- **Least privilege**: a fine-grained token for the sandbox repository only (contents and PRs write, issues
  read), in a mode-600 file, scrubbed from error output. Webhooks are HMAC-SHA256 signed; all service ports
  bind to 127.0.0.1, and the only way in is the outbound smee.io client.

Why RabbitMQ: Kafka is a log, not a task queue (no per-message ack, no priority); Redis Streams would need
hand-written claim timeouts and dead-lettering; Celery or Temporal would take over retries and state, which
the orchestrator already owns. Per-message manual ack covered "a worker died" exactly, with no visibility
timeout to guess.

## 9. CI gate

GitHub Actions with a self-hosted runner on the lab server (the GPUs are there). No marketplace actions: the
commit under test is fetched as a tarball through the REST API, for the same network reason as above.

- **Stage A (minutes, deterministic)**: unit tests (Python and the Node edit shim), then prefetch hit@10 on
  the canary tasks, which must not be lower than the reference in `data/ci-reference.json`.
- **Stage B (about 30 min)**: the full pipeline on the 20 canary tasks, graded with the hidden tests. Fails on
  any infrastructure error, or if fewer tasks are resolved than a floor.

How it avoids false blocks: deterministic node metrics must not get worse at all, while the noisy end-to-end
number only has a floor set well below its measured level, so run-to-run noise cannot block a change.

## 10. Observability

Langfuse (self-hosted, v4). Runs are exported after the fact as OTLP traces: one trace per task, with nodes,
model calls and tool calls as nested spans, and node ground truth as scores. The pipeline does not depend on
Langfuse being up; exporting again after evaluation fills in the scores. Ids are deterministic, so
re-exporting overwrites instead of duplicating.
