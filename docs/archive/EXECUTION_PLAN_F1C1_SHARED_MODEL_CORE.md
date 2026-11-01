# F1c-1 执行计划：共享 model 执行核心 + 失败计费 + sdk/application 拆分

> 状态：已完成（2026-10-20）。
> 依据：改进审查 P0-4（RunKernel 失败不计费/错误码丢失）、P1-7（`sdk/application.py` 941+ 行）、P1-8（两套 model 调用逻辑漂移）；ADR 026 的第一步。
> 目标：ReAct 与自定义编排共用**同一条** model 调用治理路径；kernel 的协议违规按 ReAct 语义计费；把 941 行的 `sdk/application.py` 拆成职责清晰的模块。

## 1. 现状问题（勘察结论）

1. `AgentRuntime._drive` 与 `RunKernel.call_model` 各自实现 prepare/trace/decide/计费：
   - ReAct 的 `model.failed` 带 `error_code/received_tool_calls/recoverable/usage`，可恢复违规会**计入被拒 token 并 +1 step**（`test_replay.py` 断言 `usage == {80, 12}`）；
   - kernel 版本全部丢弃：异常只留类型名，被拒 token 不计费 → **成本被低估**（审查 P0-4）。
2. `sdk/application.py` 混装 spec、kernel 桥接、orchestrator、Harness 四类职责，963 行。
3. `plan_execute.py` 有独立的 planner 事件流（`plan.created/plan.failed`），本轮**不动**。

## 2. 方案

### 2.1 新核心模块 `runtime/model_call.py`

```python
class ModelCallKind(StrEnum):
    SUCCESS / CONTEXT_OVERFLOW / PREPARATION_FAILED / MODEL_FAILED / PROTOCOL_VIOLATION

@dataclass(frozen=True)
class ModelCallOutcome:
    kind: ModelCallKind
    result: ModelResult | None
    usage: Usage          # 计费后的累计 usage（按现行语义：成功与可恢复违规 +1 step；违规并入被拒 token；不可恢复失败不计费）
    error: str | None
    error_type: str | None
    violation: Exception | None

async def invoke_model_once(*, model, request, budget,
                            assembler, trace, usage, phase) -> ModelCallOutcome
```

> 勘误（执行时确认）：现行代码中**不存在** `tool.schema_stale` 检查（此前审查记忆有误），helper 相应去掉 SCHEMA_STALE 分支与 `expected_tools` 参数。

- 事件序列与 payload 与现行 ReAct 完全一致：`context.overflow` → `context.compacted` → `model.request` → `model.failed`（含 usage payload）→ `model.action`（含 budget remaining）。
- 统一给上述事件补 `phase` 键（ReAct 传 `"executor"`，kernel 传自己的 phase）。已核对：现有测试只断言事件类型序列与 `model.request`/`model.failed` 的特定键，无逐字段字典比对。
- `_usage_payload/_rejected_input_tokens/_rejected_output_tokens` 从 loop.py 移入本模块；`_protocol_feedback` 与违规上限/反馈循环**留在 loop**（那是编排策略，不是调用机制）。

### 2.2 AgentRuntime 改用 helper

`_drive` 中 ~110 行内联块替换为 `invoke_model_once` + outcome 分派；违规 occurrence 计数、上限、反馈消息、checkpoint 循环保留原语义。

### 2.3 RunKernel.call_model 改用 helper（修复 P0-4）

- SUCCESS：`self.usage = outcome.usage`；token 耗尽检查不变。
- PROTOCOL_VIOLATION：**计费**（step+被拒 token）后抛 `OrchestrationError("model protocol violation: {code}")`——kernel 不自动重试，编排者可在预算内自行重试。
- MODEL_FAILED：抛 `OrchestrationError("model call failed: {error_type}")`（不回显 provider 文本）。
- CONTEXT_OVERFLOW → `KernelBudgetExceeded`；PREPARATION_FAILED/MODEL_FAILED/PROTOCOL_VIOLATION → `OrchestrationError`（violation 带稳定 code）。

### 2.4 模块拆分（带兼容 shim）

| 新模块 | 内容 |
|---|---|
| `sdk/spec.py` | `ApplicationComposition`、`ApplicationSpec` |
| `sdk/kernel.py` | 异常×3、`ModelBridge`（原 `_Model`）、`TraceBridge`（原 `_Trace`）、`RunKernel`、`Orchestrator` |
| `sdk/harness.py` | `ScopeError`、`binding_digest`、`Harness` |
| `sdk/application.py` | **纯 re-export shim**：14 个既有 `from forgeharness.sdk.application import …` 调用点（records/sessions/tasks/imports/retrieval/workflows/control/evidence/task_control/session_control/capability/5 个测试文件）零改动 |

- `sdk/__init__.py` 改为从规范模块导入；`capability.py` 改从 `sdk.spec` 导入。
- `_Model/_Trace` 更名为 `ModelBridge/TraceBridge`（已确认无外部引用）。
- 依赖方向：`harness → kernel/spec`，`kernel → spec?（不需要）`，无环。

## 3. 明确不做（F1c-2 另行计划）

- 不动 `plan_execute.py` planner 路径。
- 不共享 tool 执行器、不开放 kernel 写工具/审批/恢复（ADR 026 的 gate 原样保留）。
- 不做 README 全面重写。
- 不改任何公开 API 语义；`ApplicationSpec` 构造签名不变。

## 4. 测试计划

1. 新 `tests/unit/test_model_call.py`：五种 outcome（成功计费、overflow、不可恢复失败不计费、泛型异常、可恢复违规计被拒 token + step）+ 事件名/键断言。
2. `test_orchestration_kernel.py` 新增：协议违规模型 → run FAILED 且 `usage.input_tokens/output_tokens` 等于被拒 token、steps=1、trace 有 `model.failed` 带 usage。
3. 全量回归 1117+：`test_runtime/test_replay/test_request_preflight/test_plan_execute` 是 trace 语义的金标准。
4. ruff / mypy / 边界测试（新模块在 runtime 包内，自动被扫描）。

## 5. 风险

| 风险 | 对策 |
|---|---|
| trace payload 漂移破坏 replay/preflight 测试 | 事件名与键逐一对照搬移；先跑 loop 相关套件再拆分 |
| 拆分造成循环 import | shim 单向依赖；`sdk/__init__` 从规范模块导入 |
| kernel 计费语义与 ReAct 不一致 | 计费规则集中在 helper 一处 |
| dataclass `__module__` 变化影响序列化 | ApplicationSpec 不参与按名序列化；全量回归验证 |

## 6. 时间盒

1 个工作日。若 loop 重构后 `test_replay`/`test_request_preflight` 出现无法逐键对齐的 payload 差异，回退 helper 的“统一 phase 键”项，只保留计费共享。

---

## 执行记录

状态：**已完成（F1c-1）**。ADR：[029](../adr/029-shared-model-invocation.md)。

### 改动文件

| 文件 | 内容 |
|---|---|
| `src/forgeharness/runtime/model_call.py`（新，237 行） | `invoke_model_once` + `ModelCallOutcome/ModelCallKind`；计费规则唯一权威（成功 +1 step+tokens；可恢复违规 +1 step+被拒 tokens；不可恢复/准备失败不计费）；`usage_payload/rejected_*_tokens` 自 loop.py 移入 |
| `src/forgeharness/runtime/loop.py` | ~110 行内联 model 块替换为 helper 调用 + outcome 分派；违规反馈/上限/checkpoint 循环保留；1403→1305 行 |
| `src/forgeharness/sdk/spec.py`（新，66 行） | `ScopeError`、`ApplicationComposition`、`ApplicationSpec` |
| `src/forgeharness/sdk/kernel.py`（新，489 行） | kernel 异常×3、`ModelBridge/ToolBridge/TraceBridge`（原 `_Model/_Tool/_Trace` 更名）、`RunKernel`（call_model 改用 helper，违规计费+稳定 code）、`Orchestrator` |
| `src/forgeharness/sdk/harness.py`（新，423 行） | `binding_digest`、`Harness` |
| `src/forgeharness/sdk/application.py` | 缩为 30 行纯 re-export shim（14 个既有 import 点零改动，API v2 移除） |
| `src/forgeharness/sdk/__init__.py`、`sdk/capability.py` | 改从规范模块导入；`ScopeError` 规范源为 `sdk.spec` |
| `tests/unit/test_model_call.py`（新） | 5 个 outcome 单测 + phase/usage payload 断言 |
| `tests/unit/test_orchestration_kernel.py` | +1：违规模型 → FAILED、`model protocol violation: {code}`、usage=(1,80,12) 与 ReAct `test_replay` 同额、trace `model.failed.usage` |

### 勘误

现行代码不存在 `tool.schema_stale` 检查（审查记忆有误）；helper 无 SCHEMA_STALE 分支，计划书正文已同步修正。

### 验证

```bash
uv run --locked pytest tests/unit/test_runtime.py tests/unit/test_replay.py \
  tests/unit/test_request_preflight.py tests/unit/test_plan_execute.py \
  tests/unit/test_subagent.py tests/unit/test_sdk.py -q
# 117 passed（trace 金标准，未改任何断言）

uv run --locked pytest tests/unit/test_model_call.py tests/unit/test_orchestration_kernel.py -q
# 15 passed

uv run --locked pytest -q
# 原记录：exit 0；1123 collected（+6）。
# 2026-10-21 勘误：新采集为 1116，旧 pytest nodeids 缓存恰多 7 条；
# 1123 不再作为经绑定的采集数字。详情见 reports/framework-next/r0/collection-reconciliation.json。

uv run --locked ruff check src tests   # All checks passed
uv run --locked mypy                    # Success: 123 source files
```

### 未完成 / 下一步（F1c-2）

- 共享 **tool 执行器**：审批准入、invocation journal、重试/backoff 从 `AgentRuntime._dispatch_journalled` 抽出；kernel 获得受治理的写工具。
- 自定义 run 的挂起/恢复语义 + 故障注入测试（对齐 ReAct 的 crash windows）。
- 完成前 ADR 026 的写路径 gate 不解除。
- `plan_execute.py` planner 路径是否并入 helper：F1c-2 评估（事件流不同，暂不动）。
