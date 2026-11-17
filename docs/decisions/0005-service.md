# 0005 服务化：状态库、队列、worker、出口

日期：2026-11-16 · 阶段 2 · 代码：`state_pg.py`、`service.py`、`api.py`、`exit_pr.py`、`deploy/service/`

## 结构

```
HTTP 接口 / GitHub webhook ──(写一行 status=queued)──▶ Postgres（任务和节点状态，唯一可信来源）
                          ──(只发任务编号)───────────▶ RabbitMQ i2p.tasks（quorum 队列）
worker 1..N ◀──(预取 1，手动确认)── RabbitMQ
            ──(读写状态)──────────────▶ Postgres
            ──(大件：日志、轨迹、工作区检查点)──▶ 磁盘 runs/svc/（相当于对象存储）
```

## 决定

- **状态库换成 Postgres，编排器只改三处**：可以传入状态库；排队中的任务从头开始；节点之间检查取消标志。`PostgresStateStore` 继承文件版：状态（任务、节点状态、事件、汇总）进 Postgres，节点的输入输出 JSON、日志、Agent 轨迹和工作区检查点仍落在磁盘上。决策 0002 里“换数据库时不动别处”的预期兑现了。
- **队列只传任务编号**，任务状态以 Postgres 为准。worker 拿到消息先查数据库：已结束的任务直接确认并跳过（重复投递），已请求取消的直接标为取消。
- **RabbitMQ 的四项配置**（方案原文）：
  1. 手动确认：`run_task` 返回后才确认；
  2. 预取数 1：一个 worker 同时只拿一个任务，N 个 worker 就是 N 路并发，这就是背压；
  3. 确认超时大于任务上限：`consumer_timeout` = 2 小时，单题上限 1 小时（`deploy/service/rabbitmq.conf`）；
  4. 投递次数上限：quorum 队列 `x-delivery-limit` = 3，超过进死信队列 `i2p.tasks.dead`。基础设施错误（`infra_error`）的任务退回队列重投，其余结果都确认。
- **worker 线程模型**：任务在独立线程里跑，主线程只负责 RabbitMQ 的 I/O，这样十几分钟的任务期间心跳不断；确认通过 `add_callback_threadsafe` 回到连接所在的线程执行（pika 的通道不是线程安全的）。
- **并发上限**：worker 数 = 8，等于 vLLM 的 `max_num_seqs`。阶段 1 实测并发 8 时 vLLM 没有抢占、KV 缓存占用约 37%。超过的任务在队列里排队。
- **优先级**：RabbitMQ 4.3 起 quorum 队列支持严格优先级（0 到 31）；4.1 只有两档、按 2:1 混发。实测 4.1 下高优先级任务排在了普通任务后面，于是升级到 4.3，普通任务用 0、高优先级用 9。
- **取消**：数据库里的标志位。排队中的任务直接标为取消，worker 拿到后跳过；运行中的任务由编排器在节点之间检查，当前节点结束后停下，不再收尾出补丁。
- **配额和计量**：每个用户一份 token 额度（`users` 表，默认 2 亿），用量是该用户所有任务经网关计量的 token 之和；超额时提交返回 429。每道题自己的 600 万 token 上限照旧由网关执行。
- **出口幂等**（同一个任务只开一次 PR）：分支名由任务号决定（`i2p/<task_id>`）；在 Postgres 咨询锁里依次检查：数据库里已有 PR 链接 → 结束；GitHub 上已有这个分支的打开的 PR → 记下来、结束；否则推分支（内容确定，重复推送不产生变化）、建 PR（GitHub 回“已存在”时回到上一步），最后用 `WHERE pr_url IS NULL` 写回。
- **入口**：GitHub `issues` 事件，带 HMAC-SHA256 签名校验；只认登记过的仓库（`data/repos.json`）和触发标签 `i2p`；任务号由仓库和 issue 号决定，webhook 重投不会建出第二个任务。
- **最小权限**：开 PR 用的 token 只对测试仓库生效，只给 Contents 和 Pull requests 写权限、Issues 读权限；token 放在服务器上权限 600 的文件里，git 报错时会把它从输出里抹掉。

## 为什么不用别的队列（方案原文，这里记实测）

- Kafka 是日志不是任务队列，没有逐条确认和优先级；Redis Streams 的认领超时和死信要自己写；Celery 或 Temporal 会接管重试和状态，和自己写的编排器重复。
- RabbitMQ 的逐条手动确认在实测里正好覆盖了“worker 死掉”：消息自动回到队列，不需要猜一个可见性超时。

## 实测

| 场景 | 结果 |
|---|---|
| 3 个任务、2 个 worker | 每个 worker 各拿 1 个，第 3 个在队列里等（ready 1，unacked 2） |
| 运行中 kill -9 一个 worker | 消息重投（redelivered=True）给新 worker；它从 Postgres 读到 prepare、prefetch、reproduce 已完成，从实现节点继续，最终正常完成 |
| worker 开完 PR 立即崩溃、还没写数据库 | 消息重投给另一个 worker；任务已结束，只补做出口：找到已有 PR 并记下；模拟 GitHub 上该任务只有 1 个 PR，队列清空 |
| 排队中取消 | worker 拿到后直接跳过，`attempts` = 0 |
| 运行中取消 | 复现节点结束后停下，其余节点标为跳过，状态 cancelled |
| 额度为 0 的用户提交 | 429 |
| 优先级（4.3） | worker 做完手上的任务后，先拿后提交的高优先级任务，再拿先提交的普通任务 |

“开 PR”目前是对本地模拟的 GitHub 测的（`fake_github.py`，实现了 GitHub 的两个接口和“同一分支只能有一个打开的 PR”的 422 行为）；接真实测试仓库要等用户的决定和 token。
