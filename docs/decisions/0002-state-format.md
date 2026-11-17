# 0002 状态落盘格式

日期：2026-11-15 · 阶段 0

## 决定

状态读写只走 `state.StateStore` 这个接口。阶段 0 和 1 用 `FileStateStore` 落文件；阶段 2 加一个 Postgres 实现，编排器和节点不动。

一次运行的目录：

```
runs/<run_id>/run.json                        设置快照和题目列表
runs/<run_id>/tasks/<task_id>/state.json      任务状态机：整体状态、当前节点、每个节点的状态（原子重写）
runs/<run_id>/tasks/<task_id>/events.jsonl    只追加的事件日志：节点开始/结束、agent 结束……
runs/<run_id>/tasks/<task_id>/model_calls.jsonl  网关记的每次模型请求：节点、token、耗时、结束原因
runs/<run_id>/tasks/<task_id>/nodes/<序号>-<节点>/
    input.json  output.json  check.json  metrics.json  error.txt
    agent/<label>/{system.md, prompt.md, config.json, events.jsonl, stderr.log}
    *.log                                      该节点跑过的测试和命令的完整输出
```

## 规则

- `state.json` 每次整体写临时文件再 `rename`，进程在任意时刻被杀都不会留下半个 JSON。
- `events.jsonl` 只追加，作为事实记录；`state.json` 是它的当前视图。阶段 1 的断点恢复读 `state.json`，对不上时以事件日志为准。
- 节点状态取值：`pending`、`running`、`done`、`error`。节点“跑完但检查没过”记为 `done` 加 `check_passed=false`，和“节点本身出错”分开，这是失败归因要用的区分。
- 模型请求的计量放在网关一侧记录，不依赖 harness 自报（Pi 会把被中止的最后一条消息也算一次请求，网关只记真实发生的请求）。

## 为什么

- 文件格式在阶段 0 足够，也方便直接用 `jq` 和编辑器排查；接口先定死，换数据库时只换实现。
- 状态和沙箱分开存（在宿主机上，不在容器里），容器被删或重启不丢状态，这是阶段 2 跨 worker 接手的前提。
