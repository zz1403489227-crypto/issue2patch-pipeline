# Measuring each step: notes from building an issue-to-patch agent pipeline on a local model

issue2patch takes a GitHub issue and returns a pull request. A local model (Qwen3.6-35B-A3B in FP8 on
two RTX 4090s) does the agent work, through an off-the-shelf harness (Pi). Everything else is ordinary
code: a fixed eight-node pipeline, a gateway that meters every model call, a Postgres + RabbitMQ service, and
a CI gate on the same GPUs. This note is about what the measurements showed, including the parts that did
not go as expected.

## A pipeline, so that each step can be measured

```
prepare -> prefetch -> reproduce* -> implement* -> validate -> repair* -> review* -> emit_patch
```

Four nodes are agents (marked `*`), each a fresh session with one job and a `submit` tool for its
structured result. The other four are deterministic. The order never changes, and no agent decides what
happens next.

The main reason for this shape is measurement. One agent with every tool produces one long transcript, and
when the end-to-end number moves you cannot tell why. Here every node has a metric whose ground truth is
computed from the benchmark's reference patch and hidden tests, which only the evaluation code reads,
never the pipeline:

| Node | Metric | Ground truth |
|---|---|---|
| prefetch | hit@10 | every file the reference patch changes is in the top 10 candidates |
| reproduce | validity | the agent's test fails on the original code and passes with the reference patch |
| implement | one-shot | validation passes on the first try |
| repair | recovery | validation failed first, passed after repair |
| review | agreement | the verdict matches the hidden tests |
| validate | false alarms | validation failed but the hidden tests pass |

A failed task is attributed to the first node whose ground truth is wrong. A change to one node is
re-tested on that node alone, from the inputs stored by an earlier run (`i2p rerun-node`), so it is not
drowned in the noise of the other seven.

## How noisy is "noisy"?

The development set is 100 SWE-bench Verified tasks, each run 3 times. Running the same code on the same
tasks twice gave resolved rates 5 points apart (paired, 95% interval [-2, +12]). So differences under 5
points are not interpreted, every comparison is task-by-task (paired) with a bootstrap interval, and the CI
gate never compares the end-to-end number to a reference value. It only checks a floor that is well
below the measured range.

The baseline resolved 64.7% [55.7, 72.7] of the dev tasks. The node funnel was more informative than that
number:

- Prefetch hit@10: 57%.
- Reproduction tests valid: 42%.
- Implement one-shot: 82.7%.
- Review: 258 of 266 verdicts were "approve", and 77 of those approved a patch that failed the hidden tests.
  The review node was almost no signal at all.

## Case 1: the fix that improved the symptom and not the metric

The reproduce node had the most specific problem: in 87 runs the agent had already written its test and then
ran out of turns without calling `submit`. By contract, an unsubmitted node is rolled back, so the test
was lost. A "please submit now" message five turns before the limit was sent 408 times and almost never
worked.

The first change made the last three turns submit-only (Pi's `setActiveToolsByName`). The submit rate went
from 69% to 95%. Validity did not move (-1.0 points, interval [-9.0, +6.5]). The extra tests were
almost all invalid.

Sorting the new invalid tests by how they failed on the original code showed many that never ran at all:
`Failed to import test module`, mostly Django. The tests did `from .models import ...`, and the agent had in
fact created that `models.py`, and in its own session the test ran and failed as intended. Our contract
deleted it: the reproduce node may only add test files, and the helper module was not recognised as one.
The bug was in our code, not in the model.

The second change kept every new file inside the test directory the agent created, and gave the agent one
round to fix a test that does not even run (detected only from the output on the original code, never from
the reference). Validity: 42.0% -> 51.3%, paired +9.3 [+3.0, +15.7].

The lesson I took from it: the first fix was right but insufficient, and only the ground-truth metric showed
that. A dashboard of submit rates would have called it a success.

## Case 2: a retrieval bug hiding in a heuristic

Prefetch ranks candidate files from the issue text before any agent runs. It was the node most often
"first to go wrong" (56 of 106 failures). Because it is deterministic, it can be evaluated offline on all
tasks in a few minutes, and every miss can be inspected.

The misses had plain causes. Definitions were scored per line, so an issue that mentioned `__init__` gave 60
points to any large file with twenty constructors. A path like `__init__.py` in the issue matched every
`__init__.py` in the repository. Paths pasted with a `site-packages/` prefix matched nothing. Eight issues
got no candidates at all: nothing in them matched a path or a definition, although their words often named
a directory or a module (a database backend, a management command).

The fix borrowed term weighting from retrieval: a signal counts in proportion to how specific it is (a
symbol defined in one file counts fully, one defined in thirty files hardly at all), each (file, symbol)
pair counts once, and words of the issue that name directories or modules count by inverse document
frequency. Dev hit@10 went from 57% to 70%; 15 tasks gained, 2 lost.

End to end, nothing measurable changed, which the baseline had predicted: in 73 of 129 prefetch misses the
implement agent found the right file on its own and the task passed. A better prefetch mostly saves the agent
some searching. And 70% was measured on the tasks I looked at while fixing it; the held-out report set is
where it has to hold up.

## Case 3: tool calls from a local model

In the baseline, 99 of 1,139 `edit` calls (8.7%) failed the tool's schema validation. Most were a format
clash: Qwen3.6 writes tool arguments in an XML style, Pi's `edit` takes a nested array
`edits: [{oldText, newText}]`, and the model often tangled the two, putting JSON inside an XML parameter or
the `path` inside each edit. Usually the next call succeeded, so the cost was a wasted turn.

Two changes: an argument shim that hoists a repeated per-edit `path` to the top level (replaying the 99
recorded failures offline, it would have fixed 24), and a flat `edit(path, oldText, newText)` tool for this
model, with the same execution underneath. Both arms then re-ran the implement node, plus the validation
right after it, from the same stored inputs on the 100 dev tasks, with identical code apart from one switch,
side by side under the same load.

| Implement node, 100 tasks | Pi's edit + shim | Flat edit | Paired difference |
|---|---|---|---|
| Edit calls failing validation | 6 / 175 (3.4%) | 0 / 189 | -0.06 per task [-0.11, -0.02] |
| Implement one-shot | 80% | 75% | -5 points [-12, +2] |
| Submitted | 100% | 96% | |
| Model requests per task | 34.4 | 36.8 | |

The flat tool did exactly what it was meant to do, and nothing else improved. One-shot was 5 points lower,
inside the noise (two of the tasks it "lost" never called `edit` at all), and one change per call cost
about two extra requests per task. The other surprise was the control arm: 3.4% failures, against 9-13% in
the three baseline runs. The new shim only fired 3 times, so it does not explain the gap; baseline failures
were clustered (two tasks produced 15 of 29 in one run), which points to run-to-run variation rather than to
anything I changed.

So the flat tool stays behind a switch and Pi's own tool remains the default: fewer custom parts, and no
measured benefit to pay for them. Same lesson as case 1, from the other side: removing a visible error is
not the same as improving the result, and the ground-truth metric is what tells the two apart.

## Engineering that turned out to matter

- **One choke point for the model.** Task containers have no network. The agent reaches the model only
  through a Unix socket to a gateway on the host. That one place meters every request (the harness's own
  count was off by one whenever it aborted a final message), enforces the call timeout and the per-task
  token budget, and is where faults are injected for testing.
- **Snapshots, not hope.** Every node starts from a git-tree snapshot of the workspace and ends with a
  checkpoint. An infrastructure failure restores the snapshot and re-runs the node; a killed orchestrator
  resumes from the last checkpoint in a new container. Model-format errors are not retried at all: they go
  back to the model in the same session, which is cheaper and works.
- **An exit that can run twice.** The PR branch name is a function of the task id, and opening a PR happens
  under a Postgres advisory lock that checks the database, then GitHub, then writes back with
  `WHERE pr_url IS NULL`. A worker killed right after opening a PR, before recording it, was retried by
  another worker, which found the PR and recorded it. One PR per task.
- **Measure the infrastructure too.** RabbitMQ 4.1 quorum queues mix two priority levels 2:1 rather than
  strictly; a high-priority task was measured waiting behind normal ones, so the deployment uses 4.3. A
  40-second database restart burned a task's whole delivery limit in seconds until the state store learned
  to reconnect with backoff. From the lab server, incremental `git fetch` from GitHub was cut after about
  90 seconds while the REST API answered in about a second, so syncing and publishing go through the REST and
  Git Data APIs.
- **A CI gate that does not cry wolf.** Stage A checks deterministic things that must not get worse at all:
  unit tests and prefetch hit@10 on 20 canary tasks. Stage B runs the full pipeline on those tasks and only
  checks a floor. A pull request that "sped up" prefetch by dropping one grep pass looked harmless and
  lowered canary hit@10 from 11 to 10; stage A failed it in a minute and a half.

## Final numbers

The report set is 100 of the held-out tasks (stratified by repository and difficulty), fixed in a file
before any run and never looked at during development. Each task ran 3 times, once, with the final code.

| Metric | Result | 95% interval |
|---|---|---|
| Resolved (hidden tests) | 68.3% | [60.3, 76.0] |
| Resolved in all 3 runs | 57.0% | |
| Prefetch hit@10 | 70.0% | [61.0, 79.0] |
| Reproduction tests valid | 48.7% | [41.3, 55.7] |
| Implement one-shot | 75.3% | [69.7, 80.7] |
| Review agrees with hidden tests | 73.3% | [65.8, 80.5] |

Prefetch held at 70% on unseen tasks, the same as on the dev set it was tuned on. The review node still
approves almost everything (276 of 288 verdicts, 75 of them wrong). On average a task used 1.4M tokens,
mostly cached prefix, and 11 minutes; 100 tasks at concurrency 8 took about 2.5 hours. Two of 300 runs hit
the one-hour task limit, and none failed on infrastructure. The dev-set baseline (64.7%) was measured on
different tasks with older code, so the two numbers are not a before/after pair.

## What I would do next

- Review is the weakest node: it approves almost everything. The spec's next step is several reviewers,
  each looking at one aspect (out-of-scope changes, regression risk, test coverage), combined by a fixed
  rule, watching the rate of good patches rejected.
- Validation now trusts reproduction tests that may be wrong. Writing several candidate tests and keeping
  only those that really fail on the original code would cut both invalid tests and false alarms.
- The canary is small: most prefetch changes leave its 20-task hit count unchanged. Since prefetch is
  deterministic and cheap, the CI check could run on all 100 dev tasks.
