# ForgeHarness 框架化路线

> 状态：推进中（2026-10-21）；F1c-1 已实现，F1c-2 待实现。当前剩余工作以 [完善计划](REMAINING_WORK_PLAN_2026-10-20.md) 为执行入口；文中原始估算与历史清单不代表当前未完成项。
> 目标：让 ForgeHarness 成为**框架**，LexVault（律所）和电商 Agent 都是建立在其上的**应用**。
> 关系：[LEXVAULT_INTEGRATION_PLAN.md](LEXVAULT_INTEGRATION_PLAN.md) 从“主计划”降级为“LexVault host 验证子计划”；本文件回答更根本的问题：怎样才算框架、现在缺什么、先做什么。
> 上游研究：[UPSTREAM_FRAMEWORK_STUDY.md](UPSTREAM_FRAMEWORK_STUDY.md) 记录了 PydanticAI Harness、ADK、LangGraph、Haystack、OpenAI Agents、CrewAI、Agent Framework、AutoGen 的可借鉴模式。

## 0. 结论

1. **你的目标比“换不换 LexVault 框架”更根本。** 正确的问题是：ForgeHarness 能不能在不改核心的前提下承载 LexVault 和电商两个应用？如果能，LexVault 的运行时底座可以逐步迁过来；如果不能，先补框架，不要先迁应用。
2. **当前 ForgeHarness 是“Agent Harness SDK + 两个参考应用”，还不是“多应用框架”。** 它已经有很好的底座：公开 SDK v1、`ApplicationSpec`、工具/策略/审批/预算/checkpoint/hash trace/verifier、`RevisionStore`、`SessionService`、`TaskRunner`。但框架地位不是靠目录里的 `applications/` 证明的，而是靠**两个真实应用共用同一核心、核心不为应用改动、扩展点有契约和测试**。
3. **最大缺口曾是编排扩展点。** 2026-10-18 已实现只读 `RunKernel`/`Orchestrator`（ADR 023）：应用可以自定义控制流，通过 kernel 调 model、只读工具、budget、trace、checkpoint、verifier。剩余缺口是**写工具/审批/调用日志/恢复**仍只能走 ReAct；LexVault 的只读 DAG 可以接，写路径要等 F1c。原问题（`ApplicationSpec` 没有 runner、`Harness` 固定 ReAct、`WorkflowRunner` 顺序执行）保留在 §3.2 作为历史证据。
4. **第二缺口是边界泄漏。** `runtime`/`context` 仍然通过懒加载兼容 `coding` 垂直应用；`ARCHITECTURE.md` 的系统图仍以 Coding Agent 收尾。核心还不是应用中立的。
5. **第三缺口是真实 host 验证。** legal/commerce 都基于合成数据（legal 只读；commerce 有模拟工单写入）；LexVault 还没有作为一个应用跑在 ForgeHarness 上。两个“参考应用”不等于两个“真实应用”。
6. **建议顺序：先 F0 边界，再 F1 编排与组合，再 F2 LexVault 只读应用，再 F3 电商应用 + conformance，最后 F4 写路径/UI。** 不要先做 LexVault 全量迁移，也不要先把 A1/A3/A4 当成框架主线。
7. **“替换”仍然会发生，但替换的是运行时底座，不是领域实现。** LexVault 的专家节点、检索、页级引用、律师复核保留；ForgeHarness 接管 scope/policy/approval/budget/checkpoint/trace/verification。LexVault 的 DAG 作为应用级 Orchestrator 继续存在。

---

## 1. 什么叫“框架”

不是“有很多模块”，也不是“有 applications 目录”。对本项目，框架的最低标准是：

| 标准 | 可检验的含义 |
|---|---|
| 两个真实应用 | LexVault + 电商都用同一核心跑起来，不是只有样例 |
| 核心不因应用改 | 新增应用只写 app/port/adapter，不改 `runtime/`、`sdk/` 的领域语义 |
| 扩展点有契约 | 模型、工具、策略、验证器、检索、记录、编排都有 Protocol 和失败语义 |
| 治理不可绕过 | 任何编排方式都必须经过 scope、policy、approval、budget、trace、verification |
| 契约有测试 | 同一套 conformance suite 对两个应用都跑；import boundary 测试防止核心反向依赖应用 |
| 版本与兼容 | SDK API 版本、弃用策略、迁移指南；不是“能跑就行” |
| 文档可接入 | 新应用接入指南、最小示例、扩展点说明；不是只有作者能懂 |

### 1.1 三层职责

| 层 | 负责 | 不负责 |
|---|---|---|
| **Core** | scope/tenant/resource、工具 schema/策略/审批、预算/截止时间、checkpoint/resume、hash-chain trace、verification gate、evidence transform、幂等/调用日志 | 法律/电商领域规则、领域 prompt、UI、宿主数据库 |
| **Ports** | Model、Tool、Context、Retrieval、Records、Auth、**Orchestrator** 的 Protocol | 具体实现 |
| **Apps** | 工具实现、领域策略、验证器组合、instructions、工作流、自定义 Orchestrator | 状态所有权、审批账本、trace 写入 |
| **Integrations** | LexVault/电商的 DB、认证、检索、文件、UI 适配 | 框架契约 |

**依赖方向：** `integrations → applications → sdk/ports → runtime/domain`。反向依赖只能通过 Protocol，不能 import 具体应用。

---

## 2. 目标架构

```text
forgeharness/
  domain/            # 不可变类型：RunContext, Action, Result, Usage
  runtime/           # 受治理的运行内核：预算、终止、审批挂起、恢复
  sdk/               # 公开契约：ApplicationSpec, Harness, Tool, Policy, Verifier, Records, Sessions
  ports/             # 新增：Orchestrator, HostPort, RetrievalPort, AuthPort（Protocol）
  observability/     # trace, hash chain, step projection
  state/             # checkpoint, approval ledger, invocation journal
  applications/
    legal/           # LexVault 的领域应用（工具/策略/验证器/DAG Orchestrator）
    commerce/        # 电商领域应用
  integrations/
    lexvault/        # 只读 DB/检索/认证适配；不含领域规则
    commerce_host/   # 商品/账本/渠道适配
  examples/
  docs/
```

关键点：

- `applications/legal/` 不是“LexVault 的替代实现”，而是“LexVault 领域逻辑在框架上的应用层”。LexVault 的检索、页级引用、专家规则可以迁到这里，也可以先通过 host port 调用。
- `integrations/lexvault/` 只做数据/认证/检索适配，不含“什么算矛盾”“法条怎么适用”这类领域判断。
- `ports/` 是框架的扩展面；应用通过实现 Protocol 接入，而不是改核心分支。
- `ports/` 是逻辑分层，第一版可以放在 `sdk/` 下，不必立刻新建目录；关键是 Protocol 和 import 方向。这里的“core”指 `domain/runtime/sdk/tools/state/context/observability` 的集合，不是要求新建一个 `core/` 目录。
- `core` 不允许 import `applications/*` 或 `integrations/*`；用 import-linter 或 pytest 做边界测试。

---

## 3. 当前基线：已经有什么，缺什么

### 3.1 已经很好的部分

- 公开 SDK v1：`sdk/__init__.py` 明确“Business apps import contracts from here”，`ApplicationSpec` 是组合根。
- 运行时治理完整：`Harness` 默认路径构造带 policy、budget、trace、approval、checkpoint、verifier 的 `AgentRuntime`（[sdk/harness.py](/Users/xiaoy/Documents/多agent协作/src/forgeharness/sdk/harness.py#L161-L178)）。
- 业务原语完整：`RevisionStore`、`CitationVerifier`、`SessionService`、`TaskRunner`、`InvocationJournal`、hash-chain trace。
- 已有两个参考应用：legal（只读问答 + 待审笔记）和 commerce（商品咨询 + 工单）；不是空框架。
- 已有 ADR/审计/评测体系；工程纪律比多数“框架”项目强。

这些是资产，不要推翻。

### 3.2 缺口一（已部分解决）：编排曾被写死为 ReAct

- **2026-10-18 已实现** `Orchestrator` + `RunKernel`（[ADR 023](adr/023-orchestrator-run-kernel.md)）：应用可以自定义控制流，通过 kernel 调 model、只读工具、budget、trace、checkpoint、verifier；不传 orchestrator 时原 ReAct 路径不变。
- **仍未解决：** 写工具/审批/调用日志/恢复仍只能走 ReAct。LexVault 的只读 DAG 可以接；写路径要等 F1c。
- 历史证据：`ApplicationSpec` 字段包括 model/tools/policy/verifier/budget/instructions/context_providers/authorize_run，**没有 runner/orchestrator**（[sdk/spec.py](/Users/xiaoy/Documents/多agent协作/src/forgeharness/sdk/spec.py#L44-L66)）。
- 历史证据：`Harness._runtime` 曾固定 `AgentRuntime`（[sdk/harness.py](/Users/xiaoy/Documents/多agent协作/src/forgeharness/sdk/harness.py#L161-L178)），而 `AgentRuntime.run` 是 ReAct 主循环。
- 历史证据：`WorkflowRunner` 是顺序执行（[sdk/workflows.py](/Users/xiaoy/Documents/多agent协作/src/forgeharness/sdk/workflows.py#L364-L449)），不能表达 LexVault 的并行 fan-out（[本地律所/app/agents.py](/Users/xiaoy/Documents/本地律所/app/agents.py#L777-L816)）。
- 原缺口下的两条坏路——把 DAG 改写成 ReAct/顺序 Workflow，或在框架外自己跑 DAG——现在有第三条路：实现 `Orchestrator`，通过 `RunKernel` 治理调用。

### 3.2b 缺口一之二：`ApplicationSpec` 会变成 god object

- 现在 `ApplicationSpec` 同时持有 model/tools/policy/verifier/budget/instructions/context_providers/authorize_run；每加一个应用能力，就加一个字段或改一次核心 dataclass。
- 上游 PydanticAI 的解法是 **capability 组合**：工具、指令、上下文、hooks 都是自包含的 capability；完整 Agent（Coder/Researcher）本身也是 capability 的组合，可以整用也可以拆开。详见 [UPSTREAM_FRAMEWORK_STUDY.md](UPSTREAM_FRAMEWORK_STUDY.md)。
- 结论：框架化不只是加一个 Orchestrator 端口，还要把“应用能力”从 `ApplicationSpec` 里拆出来，做成可组合的 `Capability`（F1a/F1b 已实现；仍需 LexVault/电商两个真实应用验证）。

### 3.3 缺口二（已收口为显式兼容层）：核心曾有 Coding 垂直耦合

- **2026-10-18 已处理：** ADR 024 + `tests/unit/test_import_boundaries.py` 把依赖方向变成可执行规则；核心不 import `forgeharness.applications`，新增 core→coding import 会失败。
- 三个 Coding API-v1 兼容 shim 仍存在，被测试显式冻结，API v2 移除；其中 `context/repository.py` 为转发入口，其余为：`runtime/verification.py` 懒加载 `CodingVerifier`（[runtime/verification.py](/Users/xiaoy/Documents/多agent协作/src/forgeharness/runtime/verification.py#L55-L61)）、`context/__init__.py` 懒加载 `PythonRepositoryMap`（[context/__init__.py](/Users/xiaoy/Documents/多agent协作/src/forgeharness/context/__init__.py#L16-L21)）。
- `ARCHITECTURE.md` 已改为“SDK 组合根 + 两条执行路径”，Coding 只是参考应用，不再是系统图收尾（[ARCHITECTURE.md](/Users/xiaoy/Documents/多agent协作/docs/ARCHITECTURE.md#L5-L35)）。
- Legal 的 `case_qa_application` 已用 `Capability` + `compose_application`（[applications/legal/pilot.py](/Users/xiaoy/Documents/多agent协作/src/forgeharness/applications/legal/pilot.py#L290-L315)），成为第一个迁移的参考应用。

### 3.4 缺口三（F2a/F2b-1 已完成）：完整真实 host 验证未完成

- **2026-10-20 基线：** F2b-1 已有 SQLite 只读导出器，并用本地 14 页/5 文档运行 4 题脚本模型；不代表 live 授权、真实模型或律师验收。
- **2026-10-18 F2a：** 新增 LexVault 形状的只读 `CaseBundle` + `BundleCaseAdapter` + 并行 DAG + `[page:N]` verifier（[applications/legal/lexvault/](/Users/xiaoy/Documents/多agent协作/src/forgeharness/applications/legal/lexvault/)、[ADR 025](adr/025-lexvault-readonly-bundle.md)）。它验证了控制流，但仍是 bundle 合成数据，不是 live LexVault。
- legal 的 `case_qa_application` 面向 `CaseAdapter`，合成样例、只读（[applications/legal/pilot.py](/Users/xiaoy/Documents/多agent协作/src/forgeharness/applications/legal/pilot.py#L290-L315)）。
- commerce 的 `commerce_application` 面向合成 `CommerceStore`（[applications/commerce/application.py](/Users/xiaoy/Documents/多agent协作/src/forgeharness/applications/commerce/application.py#L697-L735)）。
- LexVault 有真实 DB、FTS5+向量+RRF、页级引用、案件权限、UI、ARQ 后台任务；F2b 才通过 host port 接进来。
- 结果：现在的“两个应用”证明的是“框架能跑两个样例 + 一个法律形状的 DAG”，还不是“框架能承载两个真实应用”。

### 3.5 缺口四：打包/版本/运行环境

- ForgeHarness 要求 Python `>=3.12,<3.14`（[pyproject.toml](/Users/xiaoy/Documents/多agent协作/pyproject.toml#L16)），LexVault 支持 3.11–3.14。
- 这不是框架设计问题，但决定第一个 host 是“独立进程/独立 venv”还是“同进程库”。框架化阶段建议先独立进程，避免把 LexVault 的运行时约束带进核心。
- SDK API v1 已存在，但需要明确弃用策略和 v2 的触发条件；否则框架会在应用压力下随意变形。

---

## 4. 关键设计：Orchestrator 端口（已实现只读内核）

> 2026-10-18：`Orchestrator` + `RunKernel` 已实现（[ADR 023](adr/023-orchestrator-run-kernel.md)）。当前只读；写工具/审批/恢复仍走 ReAct。

### 4.1 为什么它是核心

框架不应该规定“所有 Agent 都是 ReAct”，也不应该规定“所有多 Agent 都是顺序 Workflow”。它应该规定：

> **不管应用用什么编排，模型调用、工具调用、预算、审批、trace、verification 都必须经过同一个受治理的 RunKernel。**

这样 LexVault 的并行 DAG、电商的咨询流程、未来的辩论/动态路由，都可以作为应用级编排接入，而不需要改核心。

### 4.2 实际契约（只读内核）

```python
class Orchestrator(Protocol):
    async def run(self, *, kernel: RunKernel, task: str) -> RunResult: ...


class RunKernel:
    context: RunContext
    budget: RunBudget
    usage: Usage
    messages: list[Message]
    evidence: list[ToolEvidence]

    async def call_model(self, *, messages=None, phase="orchestrator") -> ModelResult: ...
    async def call_tool(self, call: ToolCall) -> ToolOutput: ...  # RiskLevel.READ only
    def checkpoint(self) -> RunResult: ...
    def emit(self, event_type: str, payload: dict | None = None) -> None: ...
    async def verify(self, final: FinalAction) -> VerificationResult: ...
    async def finish(self, *, final=None, status=RunStatus.SUCCEEDED, error=None) -> RunResult: ...
```

- `Harness(application, state_dir=..., orchestrator=...)`：传了 orchestrator 时构建 kernel；不传时保持原 ReAct 路径。
- `RunKernel` 把 model 调用绑定到 `prepare_request`/budget/trace/evidence transform；把只读工具调用绑定到 `BoundPolicy` + `ToolDispatcher`；Orchestrator 只决定“下一步做什么”。
- 约束：Orchestrator 不得直接访问 DB、不得绕过 dispatcher、不得自己写 trace/checkpoint；所有调用经 kernel。
- 当前没有内置 `ReActOrchestrator`/`PlanExecuteOrchestrator`；ReAct 仍走 `AgentRuntime`。目标态是让 `AgentRuntime` 也走同一 kernel，避免两套治理。
- **只读限制：** `call_tool` 只允许 `RiskLevel.READ`；写工具/审批/调用日志/恢复返回 `KernelToolDenied` 或仍走 ReAct。自定义 run 不支持 `resume`/`approve`。

### 4.3 迁移策略与进展

1. **已完成：** `Harness` 增加可选 `orchestrator`，默认行为不变；现有测试全绿。
2. **已完成：** `RunKernel` 门面，把 model/只读工具/policy/trace/budget/verifier/checkpoint 暴露给自定义 Orchestrator。
3. **已完成：** 2 节点并行 DAG test double 验证 trace、budget、policy、verifier 都生效。
4. **下一步：** 把 LexVault 的 DAG 接进来；不要先改 LexVault 的专家逻辑（F2）。
5. **未完成：** `AgentRuntime` 仍走自己的执行路径；目标态是 ReAct 与自定义编排共用同一 kernel。写工具/审批/恢复（F1c）也需要先抽同一套执行语义，不能复制。

**时间盒：** F1b 只交付只读内核；`AgentRuntime` 的完整统一延到 F1c/F2，不要让框架重构阻塞 LexVault 只读验证。

### 4.4 Capability 组合（与 Orchestrator 同等重要）

只加 Orchestrator 端口还不够。如果应用能力继续堆在 `ApplicationSpec` 上，框架会变成“一个大 dataclass + 很多 if”。建议引入 `Capability`：

```python
@dataclass(frozen=True)
class Capability:
    tools: tuple[Tool, ...] = ()
    instructions: tuple[str, ...] = ()
    context_providers: tuple[ContextProvider, ...] = ()
    policy: ContextualPolicy | None = None
    verifier: Verifier | None = None
    hooks: CapabilityHooks | None = None
```

- `ApplicationSpec` 只保留身份、模型、预算、orchestrator、capabilities；应用是 capability 的组合。
- 完整应用可以“整用也可以拆开”：`LegalQa()` 可以是一个组合 capability，也可以由 `CitationRules()`、`CasePageTools()`、`ChineseLegalInstructions()`、`LawyerReminder()` 重新拼出来。
- **Core 不变量不可组合掉：** scope、budget、trace、verification gate 由 Harness/kernel 强制；approval 由 ReAct 路径承担——只读 `RunKernel` 对需要审批的调用显式拒绝而不是代为放行（ADR 023/026）。capability 只能“贡献”，不能关闭这些治理。
- **不要照搬 50+ capability 的规模：** 先做 5–7 个真正跨应用的能力（引用、检索、上下文、审批提示、领域验证），领域专用能力留在 app 包。
- 参考：PydanticAI `Agent(capabilities=[...])`、Pydantic AI Harness 的 Coder 拆解；但它是 0.x，且“模型运行时写 capability”需要沙箱，ForgeHarness 不要照搬动态导入。

**测试：** take-apart test（完整 app 能由 capability 重新组合）、capability 单测、core 不变量测试、import boundary。

**不要做：** 为了 LexVault 在 `AgentRuntime` 里加 `if application_id == "legal"` 分支；那会把框架变成领域代码。

---

## 5. Host 端口：框架不碰宿主数据

- `HostPort`/`CasePort`：按 `RunContext` 授权后读取 case/page；返回带版本/摘要的原文。
- `RetrievalPort`：宿主检索（LexVault 的 FTS5+向量+RRF）；框架不内置领域检索。
- `AuthPort`：LexVault token/OIDC → `RunContext` 的 tenant/actor/resources；每次请求重新授权。
- `RecordPort`：`RevisionStore` 已有；宿主负责发布/审阅策略。
- `ModelPort`：已有 `Model` Protocol 和 OpenAI-compatible/OMLX 适配器。

**LexVault 接入顺序：**

1. 只读 `CasePort` + `RevisionStore` + 页级精确引用；用 evidence pack 或只读 DB 快照。
2. `LexVaultDagOrchestrator` 包装现有 `LawReviewCoordinator` 的专家节点；先跑 4 个问题。
3. 对照：同一批 contexts、同一模型、同一权限；比较 contract、citation、延迟、调用数。
4. 通过后再接写路径（证据审批、笔记草稿、律师复核）；写路径才需要 `ApprovalLedger` + `RevisionStore` 的完整生命周期。
5. 最后接 UI/ARQ；UI 不应该进入核心。

**原则：** host 适配器放 `integrations/lexvault/`，领域规则放 `applications/legal/`，两者都不进 `runtime/`/`sdk/` 的领域分支。

---

## 6. 路线图 F0–F5

| 阶段 | 时间 | 交付 | 通过条件 |
|---|---|---|---|
| **F0 边界** | 主要项已完成；shim 移除待 API v2 | 边界 ADR（024）；import-boundary 测试；ARCHITECTURE 更新；legal 应用迁移；两个 Coding shim 冻结 | core 无 `applications/*` import；新增 core→coding import 失败；现有测试全绿；文档不再以 Coding 为默认 |
| **F1 编排与组合** | F1a/F1b 已实现（只读）；F1c ADR/gate 已完成，实现待做 | `Capability` 组合；`Orchestrator` + `RunKernel`；2 节点并行 DAG test double；写路径 ADR 026 | 只读 DAG double 的 trace/budget/policy/verifier 全部生效；写工具/审批 fail-closed；F1c 实现前仍 ReAct-only |
| **F2 LexVault 只读应用** | F2a/F2b-1/F2b-2 数据形状已完成；真实模型质量待做 | F2a: bundle/adapter/并行 DAG/verifier；F2b-1: 只读 SQLite 导出器 + document/page 身份；F2b-2: 真实 case 1（14 页/5 文档）+ 4 题只读 harness | 0 写入、DB 哈希不变、4 题成功；真实模型/律师质量仍未评测 |
| **F3 电商第二应用 + conformance** | commerce adoption + F3a/F3b 共享子集已完成；审批/预算全矩阵与 optional extras 待做 | commerce 已用 `compose_application`；`test_application_conformance.py`；`test_runtime_conformance.py`；`docs/CONFORMANCE.md` | 工具顺序/策略/验证器不回归；成功/trace/inspect/verifier/binding/missing-resource 两应用一致；审批/resume/预算全排列未做 |
| **F4 写路径/UI/运维** | 4–6 周 | 审批/修订/笔记；UI/队列；SDK v2 计划；迁移指南 | 写路径 fail-closed；恢复/审计通过；版本/弃用策略明确 |
| **F5 清理与文档** | 持续 | 移除兼容层；示例/教程；评测；发布 | 核心稳定；应用只依赖公开 SDK/ports |

> 2026-10-18/19 进展：F0b（边界测试 + legal Capability adoption）、F1a、F1b（只读内核）、F2a（只读 bundle + 并行 DAG）、F2b-1（只读 SQLite 导出器 + 4 题测试）、F2b-2（真实 case 1 的 14 页/5 文档 + 4 题脚本模型只读 harness，DB 哈希不变）、commerce Capability adoption、F1c ADR/gate、F3a 应用契约 conformance/take-apart、F3b 共享运行时子集已完成；F1c 实现、真实模型/律师质量、审批/预算全矩阵与 optional extras 仍待做。见 [ADR 022](adr/022-capability-composition.md)、[ADR 023](adr/023-orchestrator-run-kernel.md)、[ADR 024](adr/024-core-application-boundary.md)、[ADR 025](adr/025-lexvault-readonly-bundle.md)、[ADR 026](adr/026-custom-orchestrator-write-path.md)、[CONFORMANCE](CONFORMANCE.md)、[F0b](EXECUTION_PLAN_F0B_BOUNDARY_ADOPTION.md)、[F1a](EXECUTION_PLAN_F1A_CAPABILITY.md)、[F1b](EXECUTION_PLAN_F1B_ORCHESTRATOR.md)、[F2a](EXECUTION_PLAN_F2A_LEXVAULT_DAG.md)、[F2b/F3/F1c](EXECUTION_PLAN_F2B_COMMERCE_F1C.md)、[F3a](EXECUTION_PLAN_F3A_CONFORMANCE.md)、[F3b](EXECUTION_PLAN_F3B_RUNTIME_CONFORMANCE.md)。

> 2026-10-20 进展：P0 完整性批次已完成（[ADR 027](adr/027-bundle-citation-integrity.md)、[执行计划](EXECUTION_PLAN_P0_CITATION_INTEGRITY.md)）：页级 digest + snapshot_digest、精确引文校验（可选 strict）、导出单读事务/原文保真/版本绑定内容、边界测试覆盖动态 import（并发现第三个冻结 shim）。
> 2026-10-20 进展（P1 批次）：组合审计已完成（[ADR 028](adr/028-application-composition-audit.md)、[执行计划](EXECUTION_PLAN_P1_COMPOSITION_AUDIT.md)）：`ApplicationComposition`（capability 名单 + 结构 digest）进入 spec 与 trace；全部 7 个参考应用工厂迁移到 `compose_application`；lexvault 模块覆盖率 adapter 78→98%、dag 88→99%；README/pyproject 定位改为框架优先。
> 2026-10-20 进展（F1c-1）：共享 model 执行核心已完成（[ADR 029](adr/029-shared-model-invocation.md)、[执行计划](EXECUTION_PLAN_F1C1_SHARED_MODEL_CORE.md)）：`invoke_model_once` 统一 ReAct 与 `RunKernel` 的准备/trace/计费，kernel 违规按 ReAct 语义计被拒 token 并带稳定错误码；`sdk/application.py`（~970 行）拆为 spec/kernel/harness + 30 行 shim。剩余：F1c-2 共享 tool 执行器（审批/journal/自定义 run 挂起恢复，ADR 026 gate 未解除）、README 全面重写、真实模型评测。

**总投入：** F0–F3 约 10–15 周（全职，单人）；F4 另计。不要承诺“两周变成框架”。

---

## 7. 框架验收指标

| 指标 | 目标 |
|---|---|
| 真实应用数 | ≥2（LexVault + 电商；如果电商还是样例，只能算 1.5） |
| 核心应用 import | 0（用测试强制） |
| 同一 conformance suite | 两个应用都通过 scope/policy/approval/budget/trace/verification/resume 用例（全矩阵未完成） |
| 应用契约 conformance | 四个参考 app 通过 identity/tool/order/wrong-binding 检查（F3a） |
| 完整运行时 conformance | 未完成；现有 SDK/workflow 测试覆盖分片 |
| 自定义编排 | 2 节点 DAG double 通过；ReAct 回归通过；只读工具治理生效；写路径明确仍走 ReAct |
| Capability 组合 | 完整应用可由 capabilities 重新组合；take-apart test 通过 |
| Capability 复用 | 至少 1 个能力被两个应用复用，或明确记录为何不复用 |
| 可选安装 | 最小安装可跑 core；`[legal]`/`[commerce]` 按需；无隐式依赖 |
| 新应用接入时间 | toy <1 天；真实 host <2 周（有指南和示例） |
| 核心改动 | 新应用不修改 `runtime/`/`sdk/` 领域语义；只实现 ports |
| 版本兼容 | SDK API 版本 + 弃用策略 + 迁移指南 |
| 评测 | 每个应用有自己的评测集，不用一个通用 benchmark 冒充 |
| 文档 | 接入指南、扩展点、失败语义、边界说明齐全 |

---

## 8. 对现有 LexVault 计划的影响

- [LEXVAULT_INTEGRATION_PLAN.md](LEXVAULT_INTEGRATION_PLAN.md) 仍然有用，但它是 **F2 的 host 验证子计划**，不是总路线。
- sidecar/G4 的问题从“ForgeHarness 是否比 LexVault 强”改为“**ForgeHarness 能否在不改核心的情况下承载 LexVault 的 DAG 与数据**”。
- A1/A3/A4 是 LexVault 应用级改进，不是框架核心；可以并行做，但不要用它们定义框架。
- **不要**为了“框架化”把 LexVault 的专家节点改写成 LLM 子 Agent；先让 DAG 作为 Orchestrator 接入。
- **不要**为了“统一”把 LexVault 的检索、审批、UI 全搬进框架；host port 就是用来隔开的。
- 如果 F2 发现必须改核心才能跑通，说明 Orchestrator/HostPort 设计不成立；停下来改设计，不要加领域分支。

**一句话：** 替换的是运行时底座（scope/policy/approval/trace/verification），不是领域实现（专家规则、检索、引用、复核）。

---

## 9. 风险与对策

| 风险 | 表现 | 对策 |
|---|---|---|
| 框架陷阱 | 为“未来应用”设计大量抽象，没人用 | Rule of Two：两个应用都需要才进核心 |
| God Object | `ApplicationSpec` 继续膨胀，什么都往里塞 | 拆 `ApplicationSpec` + `OrchestrationSpec` + `HostPorts`；组合优于字段 |
| 双轨维护 | LexVault native DAG 和框架 DAG 长期并存 | 时间盒；一条路径通过对照后切一条；保留 native 作为 reference |
| 核心泄漏 | 为某个应用加分支/兼容 import | import-boundary 测试 + code review checklist |
| 真实 host 缺失 | 两个应用都是合成数据，框架过度拟合 | F2 必须接 LexVault 真实只读数据；F3 接电商真实 host 或明确标注样例 |
| 版本冲突 | Python 3.12 vs 3.11/3.14 | 第一版 host 用独立进程/venv；框架不强行降级 |
| 迁移成本 | 一次性重写 LexVault | 只读先行；DAG 作为 Orchestrator；写路径最后 |
| 过度承诺 | “多 Agent 框架”但没有评测 | 每个应用单独评测；不宣称通用优越性 |
| 文档债 | 只有作者能接入 | 接入指南 + 最小示例 + conformance suite 作为文档 |

---

## 10. 历史：F0b/F1b 后的 10 天建议（已由当前完善计划取代）

| 天 | 任务 | 完成标志 |
|---|---|---|
| D1–D2 | F1c ADR：写工具/审批/journal/恢复如何复用 `AgentRuntime` 的执行语义；同时把 commerce 参考应用迁到 `Capability` | ADR 草案 + 代码路径清单；commerce 用 `compose_application` |
| D3–D5 | F2b：LexVault 导出器或只读 DB snapshot → `CaseBundle`；用同一 DAG 跑 4 题 | 不改核心即可跑；0 写入；citation/contract 有结论 |
| D6–D8 | F1c prototype（仅当 ADR 确认可复用现有审批/journal；否则只保留 ADR） | 不复制第二套治理；写路径仍 fail-closed |
| D9–D10 | conformance/take-apart 测试；更新 F2b checklist 与文档 | 知道 F2b 还缺什么；写路径是否必须等 F1c 有明确结论 |

**D10 决策：** 如果 F1c 无法在不复制审批/journal 语义的情况下接入 kernel，先保留“写路径走 ReAct、只读路径走自定义编排”的双轨，不要为了统一而伪造恢复保证。LexVault 全量迁移不在这一步。

---

## 11. 待确认

1. **电商 Agent 是真实 host 还是样例？** 这决定 F3 能否证明“两个真实应用”；如果还是样例，框架结论只能算“1 个真实 + 1 个参考”。
2. **LexVault 的 DAG 是保留为应用级 Orchestrator，还是重写成框架内置 DAG？** 建议保留；框架只提供 kernel 和端口。
3. **框架的目标用户是 Python 开发者，还是要给非开发者做配置平台？** 前者是 SDK/ports 路线；后者需要 schema/UI/插件市场，是另一个产品。
4. **Python 3.12 能否作为框架运行时？** 如果可以，LexVault 通过独立进程/venv 接入；如果必须支持 3.11，需要先评估核心语法/依赖。
5. **你每周能投入多少时间？** F0–F3 约 10–15 周全职；兼职要按比例拉长，不要用“框架”目标压垮应用交付。

> 如果暂时不回答，默认按“Python 开发者 SDK、LexVault 保留 DAG、电商先作参考应用”推进 F0/F2；F1c 开始前再确认写路径范围。
