# M10-B：Attempts、Retry 决策与分级超时

实现 retry/backoff，**不引入** circuit breaker / adaptive retry / retry queue / background scheduler / 全 run deadline（后者属 M10-C）。

---

## 核心安全不变量

> **"错误可恢复" ≠ "这个工具可以安全重试"。**

是否 retry 必须同时看 **error taxonomy + effect class + attempt budget**。最重要的一格：

> `non_idempotent` + `timeout`（工具体已进入）→ **禁止 retry** → `indeterminate`。

因为 Runtime 不知道副作用到底发生没有。绝不能因为 `timeout.recoverable=true` 就自动 retry。

## 概念拆分（你要求的关键点）

`recoverable` 一词不再承担两个语义：

| 概念 | 含义 | 例子 |
| --- | --- | --- |
| **agent-recoverable** | 模型读了 observation 后能自己换工具/换参数 | `path_not_found`、`invalid_arguments` |
| **runtime-retryable** | Runtime 可以原地重放同一调用 | 只有 `timeout`，且 effect class 允许 |

实现上取后者：`decide_retry()` **不接受 `recoverable` 参数**——从签名上就不可能把两者混为一谈。

## 冻结的 Retry Decision Matrix

单一纯函数 `runtime/retry.py::decide_retry(effect_class, error_code, attempt_no, max_attempts)`：

| 失败 | read_only | idempotent | non_idempotent |
| --- | --- | --- | --- |
| `timeout`（预算内） | **retry** | **retry**（同 key） | **indeterminate** |
| `timeout`（预算耗尽） | fail | fail | **indeterminate** |
| `tool_execution_error`（未分类） | fail | fail | **indeterminate** |
| `path_not_found` | fail | fail | fail |
| `invalid_arguments` | fail | fail | fail |

只有 `timeout` 被视为"可确证的临时失败"；未分类错误不构成临时性证据，因此不重试。全矩阵 23 个测试逐格断言（`tests/unit/test_retry_decision.py`）。

## 三件交付

1. **`tool_attempts` 落地**：logical invocation 与 physical attempt 正式分离（1:N）。
2. **Retry 决策**：上面的纯函数。
3. **分级 timeout**：从 dispatcher 常量移到 `ToolSpec.timeout_seconds`（默认 30s）；`ToolDispatcher(timeout_seconds=...)` 保留为显式组合级覆盖（用于已知慢的套件）。

### `tool_attempts` schema

```
attempt_id / invocation_id / attempt_no
started_at / finished_at / outcome(completed|failed|timed_out)
error_code / timeout_seconds / backoff_before_ms
UNIQUE (invocation_id, attempt_no) · FK → tool_invocations
```

职责只回答：**第几个物理执行？何时发生？为什么失败？有没有重试？**
**不再存一份 ToolOutput**——canonical result 只属于 `tool_invocations`。

### Backoff（确定性，无 jitter）

```
max_attempts_per_call = 3
initial_backoff_ms    = 100
backoff_multiplier    = 2.0
max_backoff_ms        = 1000
```

实测序列：`attempt1 → 100ms → attempt2 → 200ms → attempt3 → terminal`。

**不加 jitter**：核心目标是确定性评测 / Replay / latency evidence；单机单客户端没有需要打散的惊群。测试注入 `sleep_fn`，不让 510 个测试真的等待。

## Journal 状态语义（关键区分）

Attempt 失败**不**settle invocation：

```
invocation: STARTED
  attempt 1: TIMED_OUT
  retry decision = retry
  attempt 2: STARTED → COMPLETED
invocation: COMPLETED
```

> **Attempt 可以失败多次；Invocation 只在最终失败时进入 `failed`。**

否则 `failed` 会退化成"某次尝试失败了"，而恢复逻辑依赖它表示"这次调用没发生"。

终态由**决策**决定而非事后推断：`settle_invocation(..., failed_state=decision.terminal_state)`。我顺手删掉了原先按 `exception_class` 是否存在于 metadata 来推断"是否执行过"的脆弱实现——那是同一规则的第二处真源，会与 `decide_retry` 漂移。

## idempotency key 跨 attempt 恒定

硬测试：一次 logical call 的 3 次 attempt 收到**完全相同**的 key（`task:c1`）。若每次 attempt 重新生成，则"idempotent 可安全 retry"是假的。

同时验证冻结的指标口径：

```
logical_tool_calls = 1
tool_attempts      = 3      （含 attempt 计数与 backoff 序列）
```

## 六个安全测试 + 一条组合测试

| # | 测试 | 断言 |
| --- | --- | --- |
| 1 | read_only 临时失败 → retry → 成功 | `calls=2`、invocation `COMPLETED`、`attempt_count=2` |
| 2 | idempotent 临时失败 → retry → 成功 | 3 次 attempt 的 key **完全相同** |
| 3 | **non_idempotent timeout** | **`calls=1`**、`indeterminate`、decision=`indeterminate` |
| 4 | retry 预算耗尽 | 恰好执行 3 次、invocation `FAILED`、backoff `[0,100,200]` |
| 5 | agent-recoverable 但不可 retry | `path_not_found` → 1 次 attempt、decision=`fail`、observation 仍带结构化 code |
| 6 | **重启后复用成功结果** | attempt1 失败 → attempt2 成功 → crash → 恢复复用 → **不产生 attempt3** |

第 6 条把 M10-A 与 M10-B 串起来：`tool.calls` 保持 2、`attempts_for()` 长度 2、恢复后 transcript 含 journaled observation。

## Trace（可回答"为什么这个工具执行了 3 次"）

```
tool.attempt.started / tool.attempt.failed / tool.attempt.completed
tool.retry.decided  → invocation_id, attempt_no, error_code, effect_class,
                      decision, max_attempts
tool.retry.backoff  → delay_ms
tool.invocation.completed / failed / indeterminate
```

## 顺带修掉的一个真实缺陷

`settle()` 的 UPDATE 语句**不含 `attempt_count`**，导致 runtime 设的计数没有落库（测试实测 `attempt_count=1` 而实际执行 3 次）。已补列并回归。

## dev 回归与 equal-resource cap 重算（按冻结规则）

### 先确认无退化

M10-B 后的 dev natural 与 M10-B 前**逐项相同**：

| arm | M10-B 前 | M10-B 后 |
| --- | --- | --- |
| ReAct | 20/34 steps 2.24 | 20/34 steps 2.24 |
| Plan-Execute | 18/34 steps 3.79 | 18/34 steps 3.79 |

步数完全相同意味着**本次运行没有触发 retry 路径**（未出现临时失败）。因此 retry 的正确性由单元测试矩阵证明，**不是**由这次基准运行证明的——这一点必须写清，否则会误以为基准覆盖了 retry。

### 按冻结规则重算 cap

| 项 | 值 |
| --- | --- |
| pooled n | 68 |
| P75 | 4.00 |
| ceil → **cap** | **4** |
| 受约束比例 | 20/68 = **29.4%** |
| `constraint_effective` | **True** |

（顺带修正了 `M8C_CALIBRATION_RULES.md` 早先那张表的运算符：它用 `> cap` 得到 11.8%，与冻结规则文字 `steps >= cap` 不一致；已按 `>= cap` 重算为 20/68。规则与实现未变，只是测算口径统一。）

### equal-resource profile 首次真正绑定 —— 并给出结果

这是**第一次** equal 与 natural 产出不同结果（此前 cap=12 从不绑定，两个 profile 完全一致、等于什么都没回答）：

| profile | ReAct | Plan-Execute |
| --- | --- | --- |
| natural（宽预算） | 20/34 steps 2.24 | 18/34 steps 3.79 |
| **equal（cap=4，两 arm 相同）** | **20/34** steps 2.24 | **16/34** steps 3.32 |

**唯一变化的两个用例**，都是 Plan-Execute，都在共享 cap 上耗尽：

```
long-alternate-list-read          natural pass (5 steps) -> equal FAIL (4 steps, exhausted)
recover-notfound-then-alternate   natural pass (5 steps) -> equal FAIL (4 steps, exhausted)
```

**机制清晰**：两个 arm 共享同一个 4 步上限，而 Plan-Execute 固定花掉其中 1 步做规划，只剩 3 步执行；需要 4 个执行决策的任务因此被截停。ReAct 完全不受影响（它从不需要超过 4 步）。

**这就是 planner tax 在等资源下的直接代价** —— 之前因为 cap 从不绑定而无法观测。

**必须谨慎表述**：这是**单模型、34 用例、3 采样**下的 2 个用例差异，只够作为趋势，**不构成显著性主张**。可以说的准确结论是：

> 在共享 4 步上限下，Plan-Execute 因固定支付一次规划决策而损失执行容量，ReAct 未受影响。

而不是"Plan-Execute 更差"。若要更强主张，需要更多采样与多模型。

## 未做（按你的边界）

- 全 run deadline、wall-clock 上限 → M10-C。
- circuit breaker / adaptive retry / retry queue / background scheduler → 本地单机不需要。
- jitter → 确定性优先；将来真有多客户端打远端再加。
- **holdout 不跑**：M10-B 又一次改变了 Runtime 行为，须先重跑 dev natural 并**按冻结规则**重算 equal-resource cap。
