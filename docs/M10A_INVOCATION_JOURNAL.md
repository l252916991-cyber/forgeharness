# M10-A：Invocation Journal 与 Effect-Class 恢复

实现 ADR 009。**不含 retry/backoff**（留给 M10-B）。

---

## 两条核心声明

M10-A 的价值不是"多了一张表"，而是这两句话：

> **Checkpoint 丢失不会让已经完成的副作用再次执行。**
> **副作用是否发生无法确认时，Runtime 宁可停止，也不会猜测性重放非幂等调用。**

两条都有确定性测试支撑（`tests/unit/test_invocation_journal.py`，36 个测试）。

---

## 实现落点

| 关注点 | 位置 |
| --- | --- |
| 状态机语义（`decide_recovery` / `terminal_state_for_failure`） | `state/invocation_journal.py` |
| 持久化与 CAS（`SQLiteInvocationJournal`） | 同上 |
| 状态机接线（claim → started → dispatch → settle） | `runtime/loop.py` |
| 工具副作用分级 | `tools/base.py`（`ToolEffectClass`） |
| 崩溃恢复入口（`resume_running`） | `runtime/loop.py` |

**语义与持久化分离**：`loop.py` 不写一行 SQL。这样 M10-B 加 retry 时不会把 `loop.py` 变成"数据库 + 调度器 + agent loop"的混合体。

---

## 权威来源（三源分立）

| 问题 | 权威 |
| --- | --- |
| 控制流状态？ | Checkpoint |
| 这个副作用发生过吗、结果是什么？ | **Invocation Journal** |
| 可观测证据？ | Trace |

Trace **不是**恢复真源：`tool.completed` 缺失**绝不**解释为"工具没执行"。已由 `test_claimed_is_written_before_the_tool_runs` 直接断言写序。

## 副作用分级（默认 fail-closed）

| 工具 | effect_class |
| --- | --- |
| `echo` / `list_files` / `read_file` / `search_code` / `git_diff` | `read_only` |
| `write_file` / `run_tests` / 所有 MCP 工具 / 未声明工具 | **`non_idempotent`** |

`write_file` **刻意不声明 idempotent**：它的 `expected_sha256` 乐观检查让重复**失败**，这不等于"同 key 不产生第二次副作用"。要声明必须先证明后者。

MCP 工具保持默认 `non_idempotent`——外部服务可做任何事。

## 状态机

`claimed`（已受理、**工具尚未开始**）→ `started`（**已持久化提交**，此时才可调用工具）→ `completed` / `failed` / `indeterminate`。

`claimed` 创建于 **policy/approval 通过之后、执行之前**。所以审批挂起或拒绝**不产生 journal 行**，不会积累永远停在 `claimed` 的条目。

### `failed` vs `indeterminate`：按副作用确定性，不按异常

| 执行已开始？ | effect_class | 失败终态 |
| --- | --- | --- |
| 否（schema / policy / 未知工具） | 任意 | `failed` |
| 是 | `read_only` | `failed` |
| 是 | `idempotent` | `failed` |
| 是 | `non_idempotent` | **`indeterminate`** |

写了一半然后 `RuntimeError` → `indeterminate`，因为知道抛了异常但**不知道写到了哪里**。这条边界防止 M10-B 退化成 `except Exception: failed(); retry()`。

## 恢复判定矩阵（已实现并逐行测试）

| Journal state | read_only | idempotent | non_idempotent |
| --- | --- | --- | --- |
| `claimed` | 执行 | 执行 | 执行 |
| `started` | 可重新执行 | 同 key 重放 | **拒绝 → indeterminate** |
| `completed` | 用已存结果 | 用已存结果 | 用已存结果 |
| `failed` | 终止（M10-A 不重试） | 终止 | 终止 |
| `indeterminate` | 需人工 | 需人工 | **禁止重放** |

## 两个关键测试

**Test A — completed 但 checkpoint 丢失**（`test_completed_call_is_reused_when_the_checkpoint_was_lost`）

工具执行 1 次 → journal `completed` → checkpoint 未记录结果 → resume。断言：执行次数仍 **1**、从 journal 取回原 observation、**不二次执行**。

**Test B — `started` 崩溃窗口**，按你的要求拆成两种物理现实：

- **B2**（`..._refuses_replay_after_side_effect`）：副作用**已发生**、`completed` 未写。恢复见 `started` → 拒绝 → 执行次数仍 **1** → run 落 `FAILED` 且带 `indeterminate_side_effect`。
- **B1**（`..._refuses_replay_even_without_a_side_effect`）：副作用**尚未发生**（crash 在 `started` 与工具体之间）。恢复仍见 `started` → **照样拒绝**。这是**有意的假阳性**：可用性换安全性。

B1 让论证完整——它证明设计是**有意保守**于一个它无法观测的窗口，而非碰巧正确。

`settle` 与测试均验证：**不执行第二次**。

## 恢复必须重建"对话效果"

`resume_running` 在 `REUSE_RESULT` 时不仅跳过调用，还把 journal 里的 observation **作为 tool message 追加**。否则模型会看到"有 tool call 没有 result"，而我们自己的契约检查 `admitted_calls_were_executed` 也会面对不一致的 transcript。

## 结果存储

存**完整 canonical `ToolOutput`**（含 observation 文本），不只 digest——否则为取结果又得执行一次。存的是 dispatcher **已规范化之后**的值，恢复**原样重放，不重新截断**：否则同一 logical call 会向模型呈现两种含义。

超过 `MAX_JOURNALED_RESULT_BYTES` **不静默截断**而是报错（截断无法精确重建 transcript）。`result_storage_kind` 字段为将来的 `artifact_ref` 预留，M10-A 只用 `inline`。

## 迁移

只有带 journal 的 run 使用新语义。旧 checkpoint 无 journal 行时**不能**从 trace 推测 journal，对可能涉及副作用的未完成调用 fail-closed。`recovery_semantics_version` 已 `0 → 1`。

---

## 真实恢复入口（M10-A.1）

`state/recovery.py` 新增**很薄**的 `RecoveryCoordinator`，只做恢复**编排**：

1. 枚举 mid-flight run（`CheckpointStore.pending_runs()`，仅 `RUNNING`）；
2. 版本检查（`recovery_semantics_version < 1` → fail-closed，不从 trace 推测）；
3. 委托既有 `resume_running()`；
4. 记录结果，**单个 run 失败不影响其他 run**。

**它不重新实现 journal 判定表**——安全决策只有一份真源（journal 恢复矩阵）。否则很快会出现"`resume_running` 认为不能重放，协调器却认为可以"。

**并发启动竞争**不引入租约，而是复用既有的两把锁：

- journal 的 `UNIQUE (run_id, logical_call_id)`：第二个进程 claim 同一调用即失败 → 报 `conflict`；
- checkpoint 的乐观 revision：第二个写入即失败 → 报 `conflict`。

输掉竞争的一方退出，不会执行第二遍。

### 结构化闭环：Task Registry + API startup recovery（M10-A.2）

之前缺的最后一环是：

> 知道有遗留 RUNNING task → **找到它属于哪个 workspace** → 打开对应 checkpoint/journal → 恢复。

CLI 能恢复只是因为这个信息由用户显式给出；服务重启后没有。

`state/task_registry.py` 只回答一个问题：**给定 `task_id`，到哪个 workspace 找恢复真源**。

| 层 | 职责 |
| --- | --- |
| **Task Registry** | 这个 task 在哪里 |
| **Checkpoint** | task 执行到哪里 |
| **Invocation Journal** | 副作用发生到哪里 |
| **Trace** | 发生过什么 |

Registry **不参与任何恢复安全判定**，也**不维护权威 status**（否则立即出现 registry=RUNNING 而 checkpoint=FAILED 的双真源问题）。它的字段只有 `task_id / workspace / created_at / updated_at`。

两条约束：

1. **入库前规范化**：存的是**相对 configured root** 的路径（不是绝对路径，也不接受请求里的 `../../foo`、`~/project`），避免把某台机器的目录布局冻结进持久状态。
2. **恢复时不盲信**：registry 只是 locator，不是授权凭证。读取时**重新**经 `resolve_workspace_path` 校验；损坏或篡改的条目会被拒绝并跳过，不会让 startup 打开任意目录。

**扫描只在启动阶段、开始服务之前执行一次**（`recover_on_startup`，默认 **关闭**）。这样"此进程当前不可能拥有旧 RUNNING task"成立，无需 lease 即可避免把活任务误判为崩溃。**不实现** heartbeat / distributed lease / leader election / worker ownership。

**入口**：

- `forge recover <workspace> [--run-id ID]` —— 手动/诊断入口。
- API startup —— `create_app(..., recover_on_startup=True)`，`APICodingHandler` 在创建 task 时 `registry.bind(task_id, workspace)`。

**边界声明**：面向**单进程重启后的遗留 RUNNING run**，不声称多 worker 高可用接管。

## 毕业测试（composition 级）

`tests/unit/test_recovery_coordinator.py::test_full_composition_recovers_without_repeating_the_side_effect`

流程：真实工具产生真实副作用 → 进程"死亡"于 `started` 与 `completed` 之间 → **重建整个 `CodingAgent` composition（仅依赖持久状态）** → 经真实恢复入口 `RecoveryCoordinator.recover_pending()` 恢复。

断言：副作用执行次数仍为 **1**、run 落 `FAILED` 且带 `indeterminate_side_effect`、journal 状态为 `INDETERMINATE`。

这条让 M10-A 不再只是"单元测试证明状态机"，而是**证明重启后系统的真实行为**。

## 其余边界

1. **journal 已在真实写入路径生效**：CLI 与 API 构造的 `CodingAgent` 会传入 `SQLiteInvocationJournal`，`claimed → started → settle` 在真实运行中执行。
3. **`tool_attempts` 表未创建**（按冻结决定）。M10-A 的 `tool_invocations.attempt_count` **不是**物理执行日志；M10-B 引入 retry 时再建独立 attempt 记录。
4. **未实现 exactly-once**，也**不笼统写成 at-most-once**。准确表述：非幂等副作用采用 fail-closed recovery；read-only 与真正支持幂等键的工具允许安全重放。
5. **假阳性 `indeterminate` 是预期代价**，已被 B1 测试固定。

## 退出标准核对

1. [x] 副作用恢复的 source of truth = Invocation Journal（trace 不作真源）
2. [x] 四状态语义明确，`started` 先于任何副作用持久化（有写序断言）
3. [x] 三类 effect_class 的恢复方式实现并逐行测试
4. [x] logical call 与 physical attempt 语义分离（表结构按冻结决定不提前建）
5. [x] 允许重放的条件：read_only 总是；idempotent 同 key；non_idempotent 在 `started` 后从不
6. [x] 必须 fail-closed 的情形：non_idempotent 的 `started`/`indeterminate`；无 journal 的旧 run
7. [x] `completed` 后从 journal 取回原 observation 并重建 tool message
8. [x] `started` 崩溃窗口由 Test B2 证明不重复副作用，B1 证明有意保守
9. [x] `recovery_semantics_version: 0 → 1`
10. [x] 全量 gate 通过

## 未做（M10-B）

retry / backoff、分级 timeout、执行 deadline、`tool_attempts` 表。`ToolErrorCode` + `recoverable` 已是其前置条件。
