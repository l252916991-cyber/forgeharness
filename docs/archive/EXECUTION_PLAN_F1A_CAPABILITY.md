# F1a 执行计划：Capability 组合

> 状态：已完成（F1a，2026-10-18）。
> 上游依据：[UPSTREAM_FRAMEWORK_STUDY.md](../guides/UPSTREAM_FRAMEWORK_STUDY.md)、[FRAMEWORK_STRATEGY.md](FRAMEWORK_STRATEGY.md)。
> 本轮只做 F1a：把“应用能力”从 `ApplicationSpec` 里拆出来，做成可组合的 `Capability`。**不做** `Orchestrator`/`RunKernel`（F1b）。

## 1. 目标

让应用可以这样组合：

```python
spec = compose_application(
    id="legal-case-qa",
    version="1",
    policy_version="1",
    model_factory=...,
    capabilities=(
        CasePageTools(host),
        ChineseLegalInstructions(),
        CitationRules(),
    ),
    policy=ReadOnlyCasePolicy(),
    verifier=AllOfVerifier((LegalAnswerVerifier(store),)),
)
```

而不是把所有工具、指令、上下文继续堆进 `ApplicationSpec` 的字段里。

## 2. 为什么先做这个

- 这是“不同 Agent 项目都能搭”的最小可验证切片：不改 runtime，不引入新依赖，不影响现有应用。
- 它直接检验 `Capability` 是否真的能复用、能拆开；如果这个 API 不成立，`Orchestrator` 也不用急着做。
- 它是 F1b 的前置：Orchestrator 需要“应用由什么组成”先稳定下来。

## 3. 交付物

| 交付物 | 位置 | 完成标准 |
|---|---|---|
| `Capability` 数据契约 | `src/forgeharness/sdk/capability.py` | 名称非空；至少贡献一项；工具名不重复 |
| `compose_application` | 同上 | 合并 tools/instructions/context_providers，顺序稳定；重复工具名/能力名报错 |
| 公共导出 | `src/forgeharness/sdk/__init__.py` | `Capability`、`compose_application` 可从 `forgeharness.sdk` 导入 |
| 单元/集成测试 | `tests/unit/test_capability_composition.py` | 组合顺序、重复名、空能力、Harness 端到端 |
| ADR | `docs/adr/022-capability-composition.md` | 记录边界：policy/verifier/budget 仍归应用 |
| 文档 | `docs/archive/FRAMEWORK_STRATEGY.md` | 标记 F1a 已实现，F1b 只读完成 |

## 4. 明确不做

- 不做 `Orchestrator`/`RunKernel`；`Harness` 仍固定 ReAct。
- 不让 capability 贡献 policy/verifier：多个领域策略的合并语义（deny/approval/allow、`can_approve`）需要单独设计，不能草率 `all()`。
- 不做动态 import/插件加载；capability 是宿主注册的可信 Python 对象。
- 不改现有 `ApplicationSpec` 构造方式；现有应用零改动。
- 不改 LexVault。

## 5. API 草案

```python
@dataclass(frozen=True)
class Capability:
    name: str
    tools: tuple[Tool, ...] = ()
    instructions: tuple[str, ...] = ()
    context_providers: tuple[ContextProvider, ...] = ()
```

```python
def compose_application(
    *,
    id: str,
    version: str,
    policy_version: str,
    model_factory: Callable[[], Model],
    capabilities: Sequence[Capability],
    policy: ContextualPolicy,
    verifier: Verifier,
    budget: RunBudget | None = None,
    instructions: tuple[str, ...] = (),
    context_providers: tuple[ContextProvider, ...] = (),
    evidence_transform: EvidenceTransform | None = None,
    authorize_run: Callable[[RunContext], bool] | None = None,
) -> ApplicationSpec: ...
```

合并顺序：

1. 应用级 `instructions` 在前，capability 指令按声明顺序在后。
2. 工具按 capability 顺序拼接；重复工具名直接失败。
3. 上下文提供者按 capability 顺序拼接；应用级提供者在最前。
4. `policy`、`verifier`、`budget`、`authorize_run` 仍由应用显式提供。

## 6. 测试计划

1. 两个 capability 的 tools/instructions/context_providers 按顺序合并。
2. 应用级 instructions 在 capability instructions 之前。
3. 跨 capability 的重复工具名失败。
4. 重复 capability 名失败。
5. 空 capability（无工具/指令/上下文）失败。
6. 空 capabilities 失败。
7. `Harness` + `compose_application` 端到端：ScriptedModel 调工具后成功。
8. 现有 `ApplicationSpec` 直接构造仍然工作（现有测试覆盖）。

## 7. 验收

- `pytest tests/unit/test_capability_composition.py tests/unit/test_sdk.py -q` 通过。
- `ruff check`、`mypy` 对新增文件通过。
- 全量测试不回归（至少跑 `pytest -q`，允许记录已知慢测）。
- ADR 记录“为什么 policy/verifier 不在这轮组合”。

## 8. 风险

| 风险 | 对策 |
|---|---|
| Capability 变成另一个 god object | 只放 tools/instructions/context；policy/verifier 留在应用 |
| 顺序/重复语义不清 | 明确“应用级在前、capability 按声明顺序”；重复名失败 |
| 破坏现有 API | 只新增，不改 `ApplicationSpec` 字段和构造 |
| 过早抽象 | 先只做三个贡献点；两个真实应用需要时再扩展 |

## 9. 时间盒

0.5–1 个工作日；如果测试暴露 API 设计问题，先改 ADR，不硬推。

---

## 执行记录

状态：**已完成（F1a）**。F1b（`Orchestrator`/`RunKernel`，只读）已完成，见 [F1b 执行计划](EXECUTION_PLAN_F1B_ORCHESTRATOR.md)；F1c（写工具/审批/恢复）未开始。

### 改动文件

| 文件 | 内容 |
|---|---|
| `src/forgeharness/sdk/capability.py` | 新增 `Capability` 与 `compose_application` |
| `src/forgeharness/sdk/__init__.py` | 导出 `Capability`、`compose_application` |
| `tests/unit/test_capability_composition.py` | 8 个测试：顺序、重复名、空能力、治理字段转发、Harness 端到端 |
| `docs/adr/022-capability-composition.md` | 设计边界与“为什么 policy/verifier 暂不组合” |
| `docs/SDK_GUIDE.md` | 新增“能力组合”接入说明 |
| `docs/archive/FRAMEWORK_STRATEGY.md` | 标记 F1a 完成、F1b 只读完成 |

### 验证

```bash
uv run --locked pytest tests/unit/test_capability_composition.py -q
# 8 passed

uv run --locked pytest -q
# exit 0；本地 collect 1059 tests

uv run --locked ruff check src/forgeharness/sdk/capability.py \
  src/forgeharness/sdk/__init__.py tests/unit/test_capability_composition.py
# All checks passed

uv run --locked mypy
# Success: no issues found in 114 source files
```

### 未完成 / 下一步

- F1c：写工具/审批/调用日志/恢复，让自定义编排也能安全地执行有副作用的工具。
- 现有应用（legal/commerce/coding）尚未迁移到 `compose_application`；等 F1c/F2 API 稳定后再迁，避免双重改动。
- LexVault host 适配仍是 F2。
