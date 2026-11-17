# 0003 调用 harness 的接口

日期：2026-11-15 · 阶段 0

## 决定

Agent 节点只通过 `harness.base.Harness.run(AgentRequest) -> AgentResult` 调 harness。

- `AgentRequest`：节点名、系统提示、用户提示、容器、工作目录、可用工具、submit 的字段定义、请求数上限、墙钟上限、轨迹目录。
- `AgentResult`：状态（submitted / finished_without_submit / budget_exhausted / timeout / error）、submit 的内容、请求数、工具调用数和出错数、用量、耗时、错误信息、轨迹文件。

默认实现 `PiHarness`（Pi 1.0.0，版本在 `harness/pi/package.json` 里锁死）。换 OpenCode 只需要再写一个适配器，并把 `Settings.harness` 改掉，节点代码不动。

## Pi 怎么接进沙箱

```
宿主机                                     容器（--network none）
pipeline ── docker exec -i ──────────────▶ node /opt/pi/runner.mjs（配置走 stdin，事件走 stdout）
                                              Pi 会话：read / bash / edit / write + submit
ModelGateway (Unix socket) ◀── /i2p/gw.sock ◀─ 127.0.0.1 上的本地转发端口 ◀─ Pi 的 HTTP 请求
   │
   └──▶ vLLM 127.0.0.1:18949（Qwen3.6-35B-A3B-FP8，思考模式开）
```

- 容器默认断网（已验证：容器里访问 pypi.org 失败）。模型请求唯一的出口是挂进容器的网关 socket，网关在宿主机上记账，阶段 1 的预算和模型调用超时、阶段 2 的限流和优先级都加在这里。
- Node.js 和 Pi 以只读方式挂进容器，不改 SWE-bench 镜像。
- 每个 Agent 节点是一个全新的 Pi 会话（内存里的会话管理器），配置走 stdin、轨迹走 stdout 存到宿主机，容器里不留任何后续节点能读到的对话记录。
- 本地 vLLM 按 Pi 文档（`docs/models.md`）的“兼容端点”写法，在每个节点的临时 agent 目录里生成 models.json 声明。
- 思考模式用 Pi 自带的 `thinkingFormat: "qwen-chat-template"`，它会发 `chat_template_kwargs.enable_thinking` 和 `preserve_thinking`，和 Qwen3.6 模型卡的用法一致。思考档位固定为 `medium`（用户决定：固定开思考）。其他兼容开关不设，Pi 文档要求只为实测验证过的差异开开关。
- 轮数预算快用完时（剩 5 轮），用 Pi 的 `steer()` 插一条提示让 Agent 收尾提交；到上限仍未提交则中止，状态记为 budget_exhausted。
- Agent 的 bash 工具继承容器里 testbed 环境的 PATH，`python` 和 `pytest` 直接指向题目环境。

## 为什么不让 Pi 跑在宿主机、工具转进容器

考虑过的另一种做法（没有实现）：Pi 在宿主机，bash 用 `docker exec` 转发。问题是 Pi 的 read/edit/write 直接读写宿主机路径，和容器里的 `/testbed` 路径对不上，模型看到的报错路径和它能读的路径不一致。整个 Pi 进程放进容器最省事，路径也只有一套。
