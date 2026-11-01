# M9-B3 #2：Planner 协议违规 → 有界纠错重试

> M9-B 闭环之一（Planner 协议违规 → 有界纠错重试）。总览见 [`M9B_OVERVIEW.md`](M9B_OVERVIEW.md)。

第二条闭环。与 #1 形成统一设计：**协议违规可恢复，模型内容质量不代为修复。**

---

## 语义（严格限定）

> planner 返回无法通过既定 schema 的输出
> → parser **照旧拒绝**（合法性标准一字未放宽）
> → 记录 `plan.failed`（含 `error_code`、`recoverable=true`、`occurrence`）
> → 不生成、不执行任何 plan
> → 把**结构化纠错信息**反馈给 planner
> → 在 `max_planner_retries` 内重新规划
> → 成功则进入 executor；连续失败则安全终止

**只有协议失败允许重试**：没有 JSON / JSON 语法错误 / schema 不满足 / planner 返回 tool call。
**不重试**：JSON 与 schema 都合法但计划内容差、工具选择不合理、步骤遗漏——这些是 **model quality**，Harness 不偷偷"修好"。

## 实现要点

- `PlanProtocolViolation` 携带稳定 `code`：`missing_plan_json` / `invalid_plan_json` / `plan_schema_violation` / `planner_called_tool`。**只改诊断，不改宽容度**——`_parse_plan` 的提取与严格校验逻辑保持原样。
- **独立预算** `RunBudget.max_planner_retries`，默认 **1**（一次纠错机会）。不与 executor 的 `max_protocol_violations=3` 混用：两者生命周期不同，诊断上必须能分别看到。
- 纠错 prompt **不回传原始异常文本**——只说明"要求是什么、什么都没执行"。避免在 planner 路径重新引入 #3 要修的"原始异常字符串不是可行动反馈"问题。有测试断言 `JSONDecodeError` / `Expecting` / `Traceback` / `line 1 column` 均不出现。
- **成本全额计入**：每次 planner 尝试都计 step 与 tokens。纠错不是免费的。

## 指标口径（按冻结定义）

| 指标 | 每次 planner 失败 |
| --- | --- |
| `model_requests` | +1 |
| `usage.steps` | +1 |
| input/output tokens | 正常计入 |
| `logical_tool_calls` | +0 |
| `tool_attempts` | +0 |
| `planner_attempts` | 计入 `plan.created.payload` |

## Trace 证据链（实测）

```
model.request   phase=planner attempt=1 correction=False
plan.failed     error_code=missing_plan_json  recoverable=True  occurrence=1
model.request   phase=planner attempt=2 correction=True
plan.created    planner_attempts=2  steps=1
run.finished    succeeded
```

连续失败时：

```
plan.failed  occurrence=2
run.finished status=failed  error_type=repeated_planner_protocol_violation
```

保留 `plan.failed` 事件名（不统一改名为通用 `protocol.violation`），以保留 planner 语义；上层分类统一为 `failure_class=protocol_violation, phase=planner`。

## 一个由 replay 暴露的可观测性缺口（已修）

最初 `plan.failed` 只记录一句 `"planner returned an invalid plan: ..."`摘要，**原始响应内容没有落盘**。后果：replay 只能重放"失败"这个终态，无法重放"模型返回了 XML"这个事实——因此**修复后无法变绿**。

这是 replay 通道的直接价值：它逼出了"要能重放就必须记录模型原始输出"这一要求。现 `plan.failed` 额外记录 `response`（`kind` + `content` 或 tool 名与参数 + tokens），`planner_replay_script` 据此重建模型侧响应；旧 trace 无此字段时回退为异常重放（仍可复现失败，但无法验证修复）。

## Red / Green / 负例（同一份冻结的无效响应）

用捕获到的真实 XML 响应作为冻结输入，仅改变 `max_planner_retries`：

| 场景 | retries | 结果 |
| --- | --- | --- |
| **RED**（等价修复前行为） | 0 | `failed`，steps=1 |
| **GREEN**（一次纠错） | 1 | `succeeded`，steps=4 |
| **负例**（planner 始终非法） | 1 | `failed`，steps=2，**不执行任何 tool** |

负例是必需的：只证明"能恢复"不够，还要证明"**不会无限恢复**"。

`max_planner_retries=0` 时的行为与修复前**完全一致**，所以 RED 不是模拟出来的，而是同一代码路径在预算为零时的真实行为。

## Live 重评（真实模型）

两条 planner 用例的**协议格式都成功纠正**：

```
multi-echo-then-read:    missing_plan_json  → plan.created (attempts=2, 1 step) ✅
multi-search-then-read:  invalid_plan_json  → plan.created (attempts=2, 2 steps) ✅
mean_steps: 1.00 → 4.50   （证明纠错确实发生）
contract_ok: false → true
```

但用例**仍判失败**，原因是**内容质量**，与协议无关：

- `multi-echo-then-read`：`missing_tools:echo`、`sequence:read_file!=expected:echo/read_file` —— 计划漏了 echo 步骤
- `multi-search-then-read`：`arguments:accuracy=0.500` —— 工具参数写错

**这必须如实区分**：

> **Runtime 的职责是"不再因一次可恢复的协议错误立即判死"，并给出可用的纠错机会。实测模型能够在反馈后纠正协议格式。计划内容质量不足属于 model quality，不是 Runtime 修复失败。**

## 全量回归

修复后重跑 dev `natural` profile 全量（34 用例 × 3 采样）：

| arm | 修复前基线 | 修复后 |
| --- | --- | --- |
| ReAct | 19/34 | **20/34** |
| Plan-Execute | 19/34 | 18/34 |

Plan-Execute 少 1 条，落在 `long_horizon`（2/5 → 1/5）。**这需要解释，不能当作噪声带过。** 因此做了受控 A/B。

### 受控 A/B（同模型、同预算，仅切换 planner 重试）

对 `long_horizon` 全类别（5 用例 × 3 采样），唯一变量是 `max_planner_retries`：

| 用例 | retries=0（修复前等价） | retries=1（修复后） |
| --- | --- | --- |
| `long-search-read-write-verify` | `failed` ×3 | **`succeeded` ×3** ✅ |
| `long-five-step` | succeeded ×3 | succeeded ×3 |
| `long-serial-reads` | succeeded ×3 | succeeded ×3 |
| `long-six-decisions` | succeeded ×3 | succeeded ×3 |
| `long-alternate-list-read` | succeeded ×3 | succeeded ×3 |

**结论：修复让一条从失败变成功，没有让任何一条退化。**

### 那 `long-serial-reads` 的失败是什么？

全量报告里它显示 `steps:12>max:6`（0/3）。逐条排查：

1. 该 run 的 `plan.created` 记录 `planner_attempts=1`，**没有触发 planner 重试**，所以与本次修复无关。
2. 在 `retries=0`（修复前等价）下重复 6 次采样，**6/6 都是 4 步成功**。
3. natural profile 给 25 步预算，模型某次采样自行跑了 12 步，超过用例的 `max_steps=5`（planner 偏移后为 6）。

因此这是**模型行为方差**（在宽松预算下偶发长跑），修复前后都会出现，**不是本次改动引入的退化**。它的正确归属是 `model_quality`——报告已如实记录，且不会因为放宽用例断言而"修好"。

**方法论提醒**：在 3 采样、单模型的条件下，±1 个用例的差异落在噪声带内。因此**逐类别 A/B（固定变量）比跨运行的报告差值更可信**；本文档两种都给出，但结论以 A/B 为准。这也说明 M8-C 的 holdout 终评必须多次采样并报离散度。

## 退出标准核对

1. [x] parser 合法性标准**未放宽**（提取与严格校验逻辑不变，仅新增错误分类）
2. [x] invalid planner output 能稳定 RecordedModel red（`retries=0` 复现修复前行为）
3. [x] Runtime 提供一次有界纠错
4. [x] RecordedModel red → green
5. [x] 连续非法输出在预算内安全失败，且不执行任何 tool
6. [x] planner retry 的 step/token 成本完整计入
7. [x] live 重评记录：模型**能够**纠正协议格式；内容质量仍不足
8. [x] 全量 dev regression + 受控 A/B 证明无退化（A/B 显示 +1 且 0 退化）

## 未做

- #3：结构化 tool-error observation（`FileNotFoundError` → 可行动 observation）。planner 纠错已采用同样的"不泄露原始异常"原则，但 #3 处理的是 **tool** 侧的原始异常回流，范围不同。
