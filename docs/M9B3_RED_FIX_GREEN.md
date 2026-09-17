# M9-B3：Red → Fix → Green 记录

> M9-B 闭环之一（Executor 协议违规 → 可恢复反馈）。总览见 [`M9B_OVERVIEW.md`](M9B_OVERVIEW.md)。

本条记录**第一个完整闭环**：`retr-two-searches`（多 tool call 协议违规）。目标是证明这套体系能支撑 **red → fix → replay → green**，而不只是"能保存 Bad Case"。

---

## Red（修复前，2026-09-17）

**证据**：`reports/bad-cases/equal-react-retr-two-searches-s0.json` + 其哈希链 trace。

| 项 | 值 |
| --- | --- |
| `failure_class` | `protocol_violation` |
| `failure_point` | `model_adapter` |
| `error_type` | `multiple_tool_calls_not_allowed` |
| `received_tool_calls` | 2 |
| `actual_status` | `failed` |
| `usage.steps` | 0 |
| `model_requests` | 1 |
| `logical_tool_calls` | 0 |
| `tool_attempts` | 0 |

原始 trace 只有 4 个事件：`run.started` → `model.request` → `model.failed` → `run.finished`。模型一次返回 `search_code(helper)` + `search_code(run)`，适配器抛错，**整个 run 判死**，未 admission、未 dispatch。

**确定性复现**：`RecordedModel` 重放冻结的违规，不调用真实模型即得到相同结果：

```
status=failed  usage.steps=0  model_requests=1  tool_attempts=0
error=model failed: ModelProtocolError: parallel tool calls are not enabled for this runtime
```

## Fix

**语义（用户冻结）**：检测多个 tool call → **不执行任何一个** → 记录 protocol violation → 生成可行动 observation → 回给模型重新选择。**Runtime 不替模型挑第一个工具。**

改动位置：`runtime/loop.py` 的模型失败路径。适配器继续抛错（它无从判断 Runtime 是否想恢复），**恢复决策在 Runtime**——因为"能否恢复"是运行时策略，不是适配器的知识。

- `ModelProtocolError` 增加 `recoverable`（由 code 集合推断，可显式覆盖）与 `usage`（被拒请求消耗的 tokens）。
- 不可恢复错误：维持原有终态失败（行为不变）。
- 可恢复错误：计一步、计 tokens、记 `protocol.violation` 事件，把反馈作为 user message 追加，`continue` 重试。
- **有界**：`RunBudget.max_protocol_violations = 3`。超过则 `FAILED` + `repeated_protocol_violation`，避免模型持续违规形成新的无限循环。
- 反馈文案只说"错在哪、什么都没执行、该怎么做"，**不指定该调哪个工具**。

## 指标口径（严格按冻结定义）

| 指标 | 违规时 | 说明 |
| --- | --- | --- |
| `model_requests` | **+1** | 请求确实发出 |
| `usage.steps` | **+1** | 推理确实发生 |
| `logical_tool_calls` | **+0** | 未通过 admission gate |
| `tool_attempts` | **+0** | 未 dispatch |
| tokens | **正常计入** | 被拒请求的成本不丢失（修复前随异常丢失） |

这与 `model_decisions ≠ logical_tool_calls ≠ tool_attempts` 的三层区分自洽。

## Green #1 — Recorded replay

同一份冻结违规，换成修复后的 Runtime：

```
status=succeeded  usage.steps=4  model_requests=4
model_decisions=2  logical_calls=2  tool_attempts=2
tool_sequence=('search_code','search_code')
tokens=340/37   (含被拒请求的 80/12)
error=None
```

**关键性质**：违规那一步 `logical_calls` 与 `tool_attempts` 均未增加，证明 Runtime 没有替模型执行。

**一个有意保留的性质**：修复前捕获的旧记录不带 `recoverable` 字段（值为 `None`）。replay 时由**当前** Runtime 依据 `code` 重新判定，而不是回放当时结论——否则同一个记录永远无法变绿。这条有专门测试（`test_recorded_violation_is_rejudged_by_the_current_runtime`）锁定。

## Green #1 — Live 重评

真实模型，`--only-cases retr-two-searches --samples 3`：

| | 修复前 | 修复后 |
| --- | --- | --- |
| `benchmark_pass` | 0/1 | **1/1** |
| `mean_pass_rate` | 0.000 | **1.000** |
| `mean_steps` | 0.00 | 4.00 |

实测轨迹证明模型确实自我纠正：

```
protocol.violation: code=multiple_tool_calls_not_allowed received=2 occurrence=1
model.action: search_code  → tool.completed ok=True
model.action: search_code  → tool.completed ok=True
model.action: final "I searched for both helper and run in the code"
run.finished: succeeded
```

**重要区分**：即便模型第二次仍然违规，也**不**代表 Runtime 修复失败——只要 Harness 正确给出了恢复机会、并把违规如实记录，Runtime 侧的责任就已履行。模型是否纠正属于模型质量，是 live 重评才能回答的问题。这里两者都成立。

## 全量回归

修复后重跑 dev `equal` profile 全量（34 用例 × 3 采样），与修复前基线逐类别比对：

| 类别 | ReAct 前 → 后 | Plan-Execute 前 → 后 |
| --- | --- | --- |
| `retrieval_then_tool` | **2/4 → 3/4** ✅ | 1/4 → 1/4 |
| `single_tool` | 5/6 → 5/6 | 4/6 → 4/6 |
| `multi_tool_serial` | 2/6 → 2/6 | 2/6 → 2/6 |
| `ambiguous_args` | 2/4 → 2/4 | 2/4 → 2/4 |
| `tool_failure_recovery` | 4/5 → 4/5 | 4/5 → 4/5 |
| `long_horizon` | 0/5 → 0/5 | 2/5 → 2/5 |
| `no_tool` | 4/4 → 4/4 | 4/4 → 4/4 |
| **总计** | **19/34 → 20/34** | **19/34 → 19/34** |

- **+1 且仅 +1**：唯一变化是目标用例 `retr-two-searches`（属 `retrieval_then_tool`）。
- **0 退化**：没有任何原本通过的用例变成失败。
- Plan-Execute 保持 19/34：该用例在 Plan-Execute 下修复前即为通过（planner 阶段以 `tools=()` 调用，模型无法返回 tool call，因此该路径不受此缺陷影响）。

这一步是退出标准的关键：只证明"修好了一道题"不足以说明 Runtime 变好；逐类别不变才说明**修复是定向的、没有副作用**。

## 退出标准核对

- [x] 先红：修复前失败可确定性复现（不依赖模型随机性）
- [x] 修复：语义按冻结定义，有界，不替模型做决定
- [x] 后绿（recorded）：同记录在修复后进入恢复路径
- [x] 后绿（live）：真实模型收到反馈后纠正并通过（0/1 → 1/1）
- [x] 指标口径一致，被拒请求成本不再丢失
- [x] **全量 dev 回归：+1 且 0 退化**

## 未做

- Planner 协议失败的有限纠错重试（候选 #2）。
- 结构化 tool-error observation（候选 #3）。

**关于 `usage.steps=0` vs `model_requests=1` 的计量语义 gap**：对**可恢复违规**已关闭——违规现在计一步，实测 `usage.steps == model_requests`（gap=0）。对**不可恢复**错误仍为 `steps=0`，这是正确的：请求发出但响应无法作为动作使用，且 run 不做任何后续步骤，步数不应虚增。因此该 gap 的准确表述是"可恢复路径已对齐；不可恢复路径的差异是预期语义"，不再作为待修项。
