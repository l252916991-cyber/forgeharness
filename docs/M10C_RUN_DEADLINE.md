# M10-C：Run Deadline（soft / admission deadline）

在 M10-B 的 per-tool timeout 之上，加一个**整轮软截止时间**。

> **命名很重要**：这是 **soft run deadline / admission deadline**，**不是**硬 wall-clock 上限。
> 它在步间检查，**不抢占**正在执行的工具（理由见下）。所以一个 run 可能比
> `max_wall_seconds` 晚结束，最多多出一个工具自身的 timeout。**不得**声称"任务绝不会运行超过 N 秒"。

---

## 语义

`RunBudget.max_wall_seconds`（`None` = 无上限，默认值不变）。

**语义精确表述**：deadline **stops new work but does not preempt in-flight side effects**。

**deadline 在步与步之间检查**，而不是靠取消进行中的工具。理由与 ADR 009 同源：

> 在副作用写入中途打断，会留下**恰好是 invoke journal 无法判定**的那种状态——写了多少不知道。所以 deadline 只停止**新工作**，不撕断**已开始的工作**。

超时落 `FAILED` + `error_type=deadline_exceeded`（机器可读），**绝不**落 `SUCCEEDED`，即使模型已排队一个 final answer。

## 每轮重新计时

deadline 以**本次 invocation** 起算，不是任务创建时刻：

```
deadline = clock() + max_wall_seconds     # _drive 入口处
```

所以崩溃恢复后的 run 拿到**自己的**预算，而不是继承一个已过期的时钟。这正是它与 `tool_attempts` 的 `started_at` 的区别——后者是历史事实，前者是本次执行的允许量。

## 时钟注入

`clock_fn` / `sleep_fn` 可注入，所以测试不会真的等待（六个测试全部瞬时完成，用一个手动推进的 `_Clock`）。

## 六个测试

| 测试 | 断言 |
| --- | --- |
| `max_wall_seconds=None`（默认） | 行为完全不变 |
| 预算内 | `SUCCEEDED`、无 error |
| 超出预算 | `FAILED`、`error` 以 `deadline_exceeded` 开头、`run.finished.error_type` 为该值、`final_output is None` |
| **不撕断进行中的工具** | 工具执行**恰好 1 次**、journal 落 `COMPLETED` 且**有 result**、run 仍以 deadline 结束 |
| 工具执行途中不取消 | journal 落 `COMPLETED`（非停在 `started`）、attempt 记录 1 条 |
| 步数仍有余额时也生效 | `max_steps=12` 但 deadline 在第 1 步后停止 |

第 4、5 条是关键：它们证明 deadline **没有**把 run 留在"副作用状态不明"的境地。

## 真实调用方

`APICodingHandler` 给 HTTP 服务发起的 run 设 `CODING_RUN_DEADLINE_SECONDS = 1800`（30 分钟）。理由：本地 run 本已被 `max_steps × per-call timeout` 界定，这个上限只在病态情形触发；它存在的意义是给 HTTP run 一个**不随 step/timeout 默认值漂移**的时长边界。

**注意**：它同样是 soft 语义——不抢占进行中的工具，实际结束时间可超出该值最多一个工具 timeout。

## 评估 profile 刻意不设 deadline

`equal` 与 `natural` **都不**设 `max_wall_seconds`。理由是冻结规则要求 equal profile 只有**一个主要紧约束**（total steps）；再加一个会让失败无法归因——是撞步数还是撞时间？

`AgentEvaluationReport.wall_time_ms` 的注释已相应更正：它是**测量值，不是限制**（原注释说"runtime 还没有墙钟上限"，在 M10-C 后已不成立）。

## dev 回归（deadline 默认关闭，故无行为变化）

M10-C 后的 dev natural 与 M10-C 前**逐项相同**：

| arm | M10-C 前 | M10-C 后 |
| --- | --- | --- |
| ReAct | 20/34 steps 2.24 | 20/34 steps 2.24 |
| Plan-Execute | 18/34 steps 3.79 | 18/34 steps 3.79 |

按冻结规则重算仍是 **cap=4、绑定 20/68 = 29.4%、`constraint_effective=True`**，与 M10-B 后一致。

**正确解读**：deadline 默认 `None`、评估 profile 刻意不设，所以基准路径不受影响；这次回归**没有**验证 deadline 本身，deadline 的正确性由 `test_run_deadline.py` 的六个测试证明（含"不撕断进行中的工具"）。把两者混为一谈会高估基准覆盖度。

## 未做

circuit breaker / adaptive retry / jitter / retry queue / background scheduler —— 本地单机不需要，且与确定性评测目标冲突。
