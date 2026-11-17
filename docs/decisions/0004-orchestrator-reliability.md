# 0004 编排器的可靠性

日期：2026-11-16 · 阶段 1 · 代码：`src/issue2patch/pipeline.py`、`nodes/context.py`、`gateway.py`、`harness/pi/runner.mjs`

## 三类失败，三种处理

| 类别 | 例子 | 处理 | 在哪里 |
|---|---|---|---|
| 1 基础设施出错 | Docker 命令失败、容器没了、连不上模型、Pi 进程异常退出 | 把工作区恢复到这个节点开始时的快照（git tree），原样重跑该节点，退避 10s、20s，最多 3 次；容器已经没了就新建容器并重放上一个节点结束时保存的工作区 | `pipeline._run_node` |
| 2 模型输出格式错 | 工具参数没过校验；结束时没调 `submit` | 在同一个会话里把错误回给模型：参数错误由 Pi 回给模型；没调 submit 时追问一次。另外对 `edit` 工具一种已知的格式问题做确定性修正（见决策记录） | `runner.mjs` |
| 3 校验没过 | lint、复现测试、相关测试失败 | 走修复节点，最多两轮 | `nodes/repair.py` |

区分的依据：类 1 是重跑能好的、和模型无关的；类 2 是模型能自己改的，重跑整个节点反而浪费；类 3 是结果不对，要带着报错去改。代码里的 bug（其他异常）不重试，直接记成 `infra_error`，免得掩盖问题。

## 三层超时

1. 一次模型调用：网关设 900 秒总时限，超时返回 504，Pi 按自己的瞬时错误重试策略处理。
2. 一个节点：Agent 节点有各自的墙钟上限（复现 1500s、实现 2100s、修复 1200s、评审 900s），测试命令 900s。
3. 一道题：3600 秒。Agent 节点实际拿到的时限是 min(节点上限, 这道题剩下的时间)；剩不到 60 秒就不再启动 Agent。

超时算谁的：1 记在网关的请求日志里（status 504），2 记为节点的 Agent 状态 `timeout`，3 记为整道题的 `timeout`。

## 预算

每道题 600 万 token（输入加输出，按网关的计量）。阶段 0 最费的一道是 300 万，取两倍。每次启动 Agent 前检查；网关在超额后对新请求直接返回 429 作为兜底。时间或 token 用完后：后面的 Agent 节点标为 `skipped`，确定性的“出补丁”节点照常执行，这道题的状态记为 `timeout` 或 `budget_exhausted`。

## 断点恢复和幂等

- 每个节点成功结束后，把整个工作区（含未跟踪文件）存成二进制 diff `worktree.patch`，节点的缓存（复现测试路径、相关测试、基线结果）存成 `cache.json`。
- 恢复：读 `state.json`，取已完成节点的最长前缀，载入它们的输出；新建容器，重放最后一个完成节点的 `worktree.patch`，从第一个没完成的节点继续。中途断掉的 Agent 节点整个重跑，旧的轨迹保留，新的一次记为 `run-attempt2`。
- 已经结束的题（done、failed、infra_error、timeout、budget_exhausted）在恢复时直接跳过。
- 时间预算跨中断累计：恢复后的截止时间扣掉中断前已经用掉的时间。

## 实测：跑到一半杀进程

运行 `kill-test`：3 道题并发，约 3 分钟时 `kill -9` 编排器进程。当时 scikit-learn 已完成，sphinx 和 django 都在实现节点中途。执行 `i2p resume --run-id kill-test`：

- scikit-learn 被跳过（已完成）。
- sphinx 和 django 的事件日志都是：prepare、prefetch、reproduce、implement（被杀）→ `task_resumed`（已完成：prepare、prefetch、reproduce）→ implement、validate、repair、review、emit_patch。前三个节点没有重跑。
- 实现节点的轨迹目录里同时有 `run`（被杀的那次）和 `run-attempt2`（恢复后重跑）。
- 两道题都正常走完，校验通过，评审 approve，补丁非空。

## 为什么不用 Temporal 或 LangGraph

流水线只有 8 个节点、顺序固定，需要的是“每个节点结束存一次检查点、从检查点继续”。自己写的状态机加检查点不到 300 行，节点的重试、快照回滚和沙箱重建都要贴着 Docker 和 git 的细节写，交给通用框架反而要绕。阶段 2 的多 worker 场景下，状态搬到 Postgres，恢复逻辑不变。
