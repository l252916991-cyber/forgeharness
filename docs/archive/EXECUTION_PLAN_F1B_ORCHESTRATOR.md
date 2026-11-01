# F1b 执行计划：Orchestrator / RunKernel

> 状态：已完成（2026-10-18）。
> 前置：[F1a Capability 组合](EXECUTION_PLAN_F1A_CAPABILITY.md)。
> 目标：让应用自定义编排（例如 LexVault 的并行 DAG）复用框架的 model/tool/policy/trace/budget/verifier/checkpoint，而不是绕过 `Harness` 自己跑。

## 1. 范围

本轮只做**只读内核**：

- `Orchestrator` Protocol：应用提供 `run(kernel, task)`。
- `RunKernel`：受治理的 `call_model`、`call_tool`（仅只读）、`verify`、`checkpoint`、`emit`、`finish`。
- `Harness(..., orchestrator=...)`：有 orchestrator 时走 kernel；没有时保持原 ReAct 路径。
- 2 节点并行 DAG test double：验证并行 model 调用、trace、budget、verifier、checkpoint。

## 2. 明确不做

- 不做写工具/审批/调用日志/恢复：这些仍走 ReAct；自定义 orchestrator 调写工具会 `KernelToolDenied`。
- 不做 `resume`/`approve` 的自定义路径；`Harness` 明确拒绝。
- 不做动态插件/模型写代码。
- 不迁移 LexVault；只提供可用的内核。
- 不改变 `ApplicationSpec`。

## 3. API

```python
class Orchestrator(Protocol):
    async def run(self, *, kernel: RunKernel, task: str) -> RunResult: ...

kernel.call_model(messages=..., phase="critic") -> ModelResult
kernel.call_tool(ToolCall(...)) -> ToolOutput   # RiskLevel.READ only
kernel.verify(final) -> VerificationResult
kernel.checkpoint() -> RunResult
kernel.emit("dag.joined", {...})
kernel.finish(final=...) -> RunResult
```

约束：

- `call_model` 走 `prepare_request`（上下文预算/压缩）、`decide_prepared_model`、trace、usage。
- `call_tool` 走 `BoundPolicy` + `ToolDispatcher`；非 READ 或 policy 非 ALLOW 直接失败。
- `finish(success)` 必须过 `Verifier`；verifier 拒绝则 FAILED。
- 自定义 run 一次性；`resume`/`approve` 明确 `ScopeError`。
- 内核对 trace/checkpoint 使用与 ReAct 相同的 `_Trace`/`SQLiteCheckpointStore`。

## 4. 测试计划

1. 2 节点并行 DAG：`asyncio.gather` 两次 `call_model`，join 后 `finish`；断言 final、usage.steps、trace 两个 phase、trace 链有效。
2. 只读工具：policy ALLOW，执行一次，evidence 进入结果。
3. 写工具：`KernelToolDenied`，工具未执行，run FAILED。
4. step budget：第二次 `call_model` 抛 `KernelBudgetExceeded`，run FAILED。
5. verifier 拒绝：run FAILED，trace 有 `verification.rejected`。
6. orchestrator 返回但未 `finish`：run FAILED（fail closed）。
7. orchestrator 抛异常：run FAILED，trace 有 `orchestrator.failed`。
8. custom run 的 `resume` 被拒绝。

## 5. 验收

- 新增 8 个测试通过。
- 现有 `test_sdk.py` / `test_capability_composition.py` / `test_request_preflight.py` 不回归。
- `ruff`、`mypy` 通过。
- 全量测试通过。

## 6. 风险

| 风险 | 对策 |
|---|---|
| 内核复制出一套治理 | 复用 `_Model`/`_Trace`/`BoundPolicy`/`ToolDispatcher`/`prepare_request`；不新造策略/审批 |
| 只读限制影响 LexVault | LexVault 的 DAG 节点是确定性 host 代码，Critic 只需 `call_model`；只读工具够用 |
| 自定义 run 恢复语义弱 | 明确 one-shot；`resume` 拒绝；`recovery_semantics_version=0` |
| `Harness` 改动影响 ReAct | 默认分支保持原调用；现有 SDK 测试回归 |
| 内核 API 过早固化 | 只暴露最小方法；写路径/审批另立 ADR |

---

## 执行记录

状态：**已完成（F1b 只读内核）**。

### 改动文件

| 文件 | 内容 |
|---|---|
| `src/forgeharness/sdk/application.py` | 新增 `RunKernel`、`Orchestrator`、异常；`Harness` 增加 `orchestrator` 注入；拆分 `_trace`/`_components`/`_kernel` |
| `src/forgeharness/sdk/__init__.py` | 导出新契约 |
| `tests/unit/test_orchestration_kernel.py` | 8 个测试 |
| `docs/adr/023-orchestrator-run-kernel.md` | 设计边界与只读限制 |
| `docs/SDK_GUIDE.md` | 自定义编排接入说明 |
| `docs/archive/FRAMEWORK_STRATEGY.md` | F1b 进展 |

### 验证

```bash
uv run --locked pytest tests/unit/test_orchestration_kernel.py -q
# 8 passed

uv run --locked pytest tests/unit/test_sdk.py \
  tests/unit/test_capability_composition.py \
  tests/unit/test_request_preflight.py -q
# passed

uv run --locked ruff check ...
# All checks passed

uv run --locked mypy
# Success: no issues found
```

```bash
uv run --locked pytest -q
# exit 0；1067 collected
```

### 未完成 / 下一步

- **F1c**：写工具/审批/调用日志/恢复的自定义路径；需要把 `AgentRuntime` 的审批与 journal 语义抽到 kernel，或让 kernel 复用同一套执行器。
- 把 LexVault 的 `LexVaultDagOrchestrator` 接到 kernel（F2）。
- Capability 的 policy/verifier 贡献仍未做。
