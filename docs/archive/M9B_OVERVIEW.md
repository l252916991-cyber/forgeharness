# M9-B：Agent 执行 Bad Case 与恢复语义（总览）

本文件是 M9-B 的入口，串联三个已完成的 red→fix→green 闭环与其证据。详细记录在各分册。

---

## 一句话

**ForgeHarness 不再"碰到模型格式不标准或工具抛异常就崩"，而是具备了分层的 fault-tolerant agent execution semantics，且每一层都有确定性可复现的回归证据。**

三个闭环互补，覆盖三类故障：

| # | 故障层 | 修复语义 | 证据 |
| --- | --- | --- | --- |
| 1 | **Executor 协议** | 多个 tool call → 不执行任何一个 → 可行动反馈 → 模型重试 | [M9B3_RED_FIX_GREEN.md](M9B3_RED_FIX_GREEN.md) |
| 2 | **Planner 协议** | 无效计划 → parser 照旧拒绝 → 结构化纠错 → 有界重新规划 | [M9B3_PLANNER_RECOVERY.md](M9B3_PLANNER_RECOVERY.md) |
| 3 | **工具执行** | 宿主异常 → 分类 → 结构化可行动 observation（**不自动重试**） | [M9B3_TOOL_FAILURE_OBSERVATION.md](M9B3_TOOL_FAILURE_OBSERVATION.md) |

---

## 统一设计原则

三个闭环不是三套特判，而是同一套原则的三次应用：

1. **协议/执行失败可恢复，模型内容质量不代为修复。** Harness 给机会、给可行动反馈、如实记录；不替模型选工具、不替模型改计划内容。
2. **有界恢复。** executor 用 `RunBudget.max_protocol_violations`（默认 3），planner 用 `max_planner_retries`（默认 1）。恢复是礼节，不是无限额度。
3. **两个接口分离。** 给模型的是稳定、可行动、确定性的 observation；给 Harness/人的是分类字段（`error_code` / `exception_class` / `recoverable`）。原始异常文本与宿主路径**不进模型上下文**。
4. **分类按类型/代码，不按消息匹配。** 消息带宿主路径且会改措辞。
5. **纠错不是免费的。** 被拒请求的 token、planner 的每次尝试、失败的 tool 调用都全额计入。`logical_tool_calls` / `tool_attempts` 的三层区分保持不变。
6. **修复必须可确定性复现。** `RecordedModel` 冻结模型侧行为（含违规），因此可以证明"harness 改好了"，而不是"模型碰巧表现更好"。

---

## Replay 通道与它的边界

最小 `RecordedModel`（`evaluation/replay.py`）：

- 从 Bad Case 冻结的模型输出按序重放，**不调用真实模型**；
- 能证明 **Runtime / Schema / 状态机 / 分类** 的修复；
- **不能**证明 prompt 或 planner 策略改动让模型变好——那必须 live 重评。

两层不可互相替代。三个闭环都同时给了 **recorded** 与 **live** 两侧的证据，并明确区分"harness 责任"与"模型质量"。

---

## 这套体系逼出来的三个真实问题

M9-B 的价值不只在修复本身，还在于 replay 与分类机制暴露出原先看不见的问题：

1. **可观测性缺口（planner）**：`plan.failed` 只记录一句摘要，不记录模型原始响应，导致 replay 只能复现失败、无法验证修复。现记录 `response`。
2. **分类 bug**：已恢复的运行（`actual_status=succeeded` 但 trace 里有 `plan.failed`）被硬编码判为 harness 失败，掩盖真实失败原因并污染 Bad Case 集。现按真实状态判定，边界事件只在未恢复时才算原因。
3. **投影漏字段**：`tool.completed` 记录 `error_code` 而投影只看 `error_type`，导致 trace 有分类而诊断视图为 `None`。现回退链为 `error_code` → `error_type` → `error`。

---

## Bad Case 目录的准入规则

`reports/bad-cases/` 只保留**需要 harness 介入**的条目：

- 有 `harness_gap`（Harness 能力缺口）或 `case_design_note`（用例自身缺陷）；
- **纯 `model_quality` 不落盘**——它已完整记录在报告里，且可重跑复现。否则一次全量回归会写入约 30 条，淹没真正可行动的少数条目。

当前保留 5 条：1 条 `protocol_violation`（executor）+ 2 条 `case_design`/`harness_gap` 的模型质量样本。

---

## 指标口径（三案例统一）

| 情形 | model_requests | usage.steps | logical_tool_calls | tool_attempts | tokens |
| --- | --- | --- | --- | --- | --- |
| executor 协议违规 | +1 | +1 | **+0** | **+0** | 计入 |
| planner 协议违规 | +1 | +1 | +0 | +0 | 计入 |
| 工具执行失败 | 不变 | 正常 | **+1** | **+1** | 正常 |

工具失败**不抹掉**这次调用——它为 M10 的 retry 预留了语义（1 logical call → N attempts）。

---

## 明确未做

- **自动 retry / backoff**：属于 M10。`ToolErrorCode` 与 `recoverable` 已是其前置条件。
- **更细的错误分类**：等真实 Bad Case 出现再扩，不预铺几十种。
- **熔断器 / 分布式租约 / `DEAD` 状态**：不在计划内（YAGNI）。
- **holdout 终评**：尚未运行，未参与任何调参。

---

## 残余风险与边界

- 三个闭环是**在具体用例上被证明**，不是通用保证。
- live 结论绑定单个本地模型（`Qwythos-9B-v2-8bit-mlx`），不可外推。
- **Tool-failure observation 形式是可影响模型行为的实验变量**：受控实验显示同用例同预算下，原始字符串 4 步 vs 结构化 11 步。跨版本比较受影响用例时必须固定 observation 形式或标注版本。
- 采样方差真实存在：3 采样、单模型下 ±1 用例落在噪声带内，逐类别受控 A/B 比跨运行报告差值可信。M8-C holdout 终评必须多次采样并报离散度。
