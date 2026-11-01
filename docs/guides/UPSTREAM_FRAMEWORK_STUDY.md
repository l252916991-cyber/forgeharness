# 上游 Agent 框架研究：模块化设计可借鉴什么

> 状态：v0.1 研究记录（2026-10-18）。
> 方法：GitHub shallow clone 8 个代表项目，固定 commit，读目录结构、核心契约、文档，不复制代码。
> 本地位置：`references/upstream/`（已 gitignore，仅学习用）。
> 进展：F0b core/application 边界 + legal Capability adoption 已完成（ADR 024）；F1a `Capability` + `compose_application` 已实现（ADR 022）；F1b `Orchestrator` + `RunKernel` 已实现只读内核（ADR 023）；F2a LexVault 形状的只读 bundle + 并行 DAG 已实现（ADR 025）；F2b-1 只读 SQLite 导出器 + 真实 case 1 数据形状验证、commerce Capability adoption、F1c 写路径 ADR/gate 已完成（ADR 026）；F3a 应用契约 conformance、F3b 共享运行时矩阵已完成。真实模型/律师质量、F1c 实现、审批/预算全矩阵与真实 LexVault UI 仍待做。
> 结论先说：**推荐模块化，但推荐的是“能力组合 + 受治理内核 + 端口/适配器”，不是“所有东西都做成插件”。**

## 0. 结论

1. **模块化是对的，而且是你这个项目最该补的架构能力。** 但“模块化”不等于“目录多”或“插件多”；它等于：核心契约稳定、扩展点清晰、应用可拆可合、边界可测试。
2. **最接近你目标的是 Pydantic AI + Pydantic AI Harness。** 它把“能力（capability）”作为唯一组合单元：工具、指令、上下文、hooks 都是 capability；完整 Agent（Coder/Researcher）本身也是 capability 的组合，可以整用也可以拆开。这直接对应你说的“不同 Agent 项目都能搭、好管理”。
3. **编排层最值得看 Google ADK 和 LangGraph。** ADK 的 `Workflow` 是图节点，支持并行分支、join、嵌套、interrupt/resume；LangGraph 把图运行时和 checkpoint 后端分开，并有 checkpoint conformance suite。两者都能支撑 LexVault 的并行 DAG，而不必把 DAG 改写成 ReAct。
4. **组件/管线最值得看 Haystack。** `Component` + `Pipeline` 的 typed 输入/输出契约，是“可替换组件 + 显式 DAG”的成熟做法；适合检索/预处理/路由这类模块。
5. **最小核心最值得看 OpenAI Agents SDK。** Agent/Runner/handoffs/guardrails/sessions/tracing 各自独立；核心小，扩展点清楚。
6. **打包/集成最值得看 Microsoft Agent Framework。** core 包 + 每个 provider/store 一个独立包（`agent-framework-openai`、`agent-framework-redis` 等），避免核心依赖爆炸。
7. **不要照搬的：** PydanticAI 的 50+ capability 规模、ADK 的云平台耦合、LangGraph 的“图优先”复杂度、CrewAI 的 role/task 高层抽象、AutoGen（已归档）。这些是“可以学设计、不要学表面积”。
8. **对 ForgeHarness 的最小改造：** 在现有 `ApplicationSpec` 之上引入 `Capability` 组合；在 `Harness` 上引入 `Orchestrator` 端口；把 core / harness / apps / integrations 分开；用 conformance suite 证明两个应用共用同一核心。**不要**先做插件市场或动态加载。

---

## 1. 下载清单

完整清单、commit、license 见 [`references/upstream/README.md`](../../references/upstream/README.md)。

| 项目 | 固定 commit | License | 一句话定位 |
|---|---|---|---|
| [pydantic-ai](https://github.com/pydantic/pydantic-ai) | `0be309f8` | MIT | typed agent + capabilities + graph + durable execution |
| [adk-python](https://github.com/google/adk-python) | `e2dbc0f5` | Apache-2.0 | modular agent framework + Workflow graph + plugins/services |
| [langgraph](https://github.com/langchain-ai/langgraph) | `0870f5ef` | MIT | low-level stateful graph orchestration + checkpoint |
| [haystack](https://github.com/deepset-ai/haystack) | `c6c46c67` | Apache-2.0 | component/pipeline for RAG & agents |
| [openai-agents-python](https://github.com/openai/openai-agents-python) | `9abc9182` | MIT | lightweight multi-agent SDK |
| [crewAI](https://github.com/crewAIInc/crewAI) | `40bb2d3b` | MIT | Crew + Flow multi-agent orchestration |
| [agent-framework](https://github.com/microsoft/agent-framework) | `ebcf4db7` | MIT | Microsoft 的生产级 agent/workflow 框架（AutoGen 后继） |
| [autogen](https://github.com/microsoft/autogen) | `004749e6` | code MIT / docs CC BY 4.0 | **已归档**；看 core/agentchat 分层，不要采用 |

---

## 2. 对比表

| 项目 | 核心抽象 | 模块化机制 | 编排/持久化 | 最值得学 | 不要照搬 |
|---|---|---|---|---|---|
| **PydanticAI** | `Agent` + `Capability` + `Toolset` | capability 是可组合单元；完整 Agent 也是 capability 组合；`pydantic_graph` 独立 | `pydantic_graph`；durable execution 适配 Temporal/DBOS/Prefect | **capability 组合、“take it apart”、typed DI、core/harness 分包** | 50+ capability 的表面积；0.x API 不稳定 |
| **ADK** | `BaseAgent` 树 + `Workflow` 图节点 + `Plugin` | agents/tools/models/sessions/memory/artifacts/auth/plugins 分层；graph 可嵌套 | `Workflow` 支持并行、join、动态节点、interrupt/resume | **workflow graph 作为一等节点；插件回调；服务分层** | Google Cloud/Vertex 耦合；状态 schema 复杂 |
| **LangGraph** | `StateGraph` + `Pregel` + checkpoint | 图运行时与 checkpoint 后端分包；`checkpoint-sqlite/postgres/conformance` | 图执行、流式、human-in-the-loop、durable execution | **checkpoint 契约与 conformance suite；子图/中断** | “所有问题都用图”的复杂度；低层 API 学习曲线 |
| **Haystack** | `Component` + `Pipeline` | 组件通过 typed 输入/输出 socket 连接；组件生态 | Pipeline DAG，同步/异步/流式 | **typed component 契约；可替换组件；显式 DAG** | RAG 专用组件不要进核心；Agent 层较新 |
| **OpenAI Agents** | `Agent` + `Runner` | tools/handoffs/guardrails/sessions/tracing/models 分目录 | Runner 循环；handoff；session；tracing | **最小核心；关注点分离；handoff/guardrail 原语** | provider 绑定；治理深度不如 ForgeHarness |
| **CrewAI** | `Crew` + `Flow` + `Task` | crews/flows/events/hooks/tools/memory/knowledge 分层 | Crew 顺序/层级；Flow 事件驱动 | **Crew vs Flow 分层；events/hooks** | role/task 高层抽象当核心；模块多且耦合 |
| **Agent Framework** | `Agent` + `Workflow` + `Executor` | core 包 + 每 provider/store 独立包；WorkflowBuilder | fan-out/fan-in/switch、checkpoint、多语言 | **integration 分包；WorkflowBuilder 的图构建 API** | 多语言/双栈复杂度；不必全搬 |
| **AutoGen** | `autogen-core` + `autogen-agentchat` | core（actor/runtime）与 agentchat（teams/agents）分离 | actor 消息传递；team 编排 | **core 与高层 API 分离** | **已归档，不要采用**；迁移到 Agent Framework |

---

## 3. 六个可借鉴模式

### 3.1 Capability 组合（PydanticAI / Pydantic AI Harness）

PydanticAI 的 `Agent(..., capabilities=[...])` 把工具、指令、上下文、hooks 都收进“能力”这个统一单元；`Coder`、`Researcher` 本身也是 capability 的组合，README 明确说“use it whole, or take it apart”。这带来两个直接好处：

- **应用可拆可合：** 一个完整法律 Agent 可以是“引用能力 + 案件检索能力 + 中文法律指令 + 法规核验能力 + 复核提示能力”的组合；不需要一个巨大的 `LegalAgent` 类。
- **复用可度量：** 如果两个应用都需要“引用校验”，它就是一个 capability；只有一个应用需要，就留在 app 包，不上升为框架核心。

**对 ForgeHarness 的启发：** 现在 `ApplicationSpec` 直接持有 `tools/policy/verifier/instructions/context_providers`，应用一多就会变成 god object。建议引入 `Capability` 组合，把“贡献”拆开：

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

`ApplicationSpec` 只保留身份、模型、预算、orchestrator、capabilities；应用是 capability 的组合。不要照搬 50+ 的规模；先做 5–7 个真正跨应用的能力。

### 3.2 图/工作流作为一等节点（ADK / LangGraph / Agent Framework）

ADK 的 `Workflow` 本身是 `BaseNode`，节点可以是 Python 函数、`LlmAgent`、tool 或另一个 `Workflow`；边声明依赖，独立分支自动并行，支持 join、动态节点、interrupt/resume。LangGraph 的 `StateGraph` 把节点/边/状态编译成 `Pregel`，checkpoint 独立成包。Agent Framework 的 `WorkflowBuilder` 提供 `add_fan_out_edges`、`add_fan_in_edges`、`add_switch_case_edge_group`。

**对 ForgeHarness 的启发：** 这正是 LexVault 并行 DAG 需要的形状。不要在 `AgentRuntime` 里加 `if legal`；定义一个 `Orchestrator`/`Workflow` 端口，应用可以注册自己的图。内置 ReAct/PlanExecute/顺序 Workflow 只是三种实现；LexVault 的 DAG 是第四种。

### 3.3 core / harness / integrations 分包（PydanticAI Harness / Agent Framework）

PydanticAI 把“轻 harness”放在核心，把“重 harness（文件、shell、memory、subagents、compaction、durable execution）”放在 `pydantic-ai-harness`；Agent Framework 把每个 provider/store 做成独立包。

**对 ForgeHarness 的启发：**

```
forgeharness              # core: contracts + governance
forgeharness-harness      # batteries: filesystem, shell, memory, subagents, compaction, retrieval, planning
forgeharness-legal        # LexVault app
forgeharness-commerce     # commerce app
forgeharness-integration-*  # providers/stores/hosts (optional extras)
```

核心不装领域能力；应用不装其他应用；集成包按需安装。这样“不同 Agent 项目”只依赖 core + 需要的 harness/integration，不会被别的应用的依赖拖累。

### 3.4 typed component / pipeline（Haystack）

Haystack 的 `Component` 是一个 `run` 返回 `Mapping` 的 Protocol，`@component` 声明输入/输出 socket，`Pipeline` 在连接时校验类型并调度 DAG。它把“可替换组件”做成了生态。

**对 ForgeHarness 的启发：** 工具/上下文提供者已经是 typed 的；检索/预处理/路由类模块可以借鉴 component 契约，但不必把整个框架做成 pipeline。对于 LexVault，`HybridRetriever` 可以是一个 `RetrievalCapability`，而不是核心内置逻辑。

### 3.5 最小核心 + 独立关注点（OpenAI Agents）

OpenAI Agents 的核心只有 Agent/Runner，handoffs、guardrails、sessions、tracing、models 各自独立。它没有把“多 Agent 拓扑”写进核心，而是用 handoff 这个原语让应用组合。

**对 ForgeHarness 的启发：** ForgeHarness 的治理比它强，但可以学“核心小、关注点分”。不要把 memory、RAG、UI、审批 UI 都塞进 `runtime`；它们是服务/能力/集成。

### 3.6 checkpoint 后端分离 + conformance（LangGraph）

LangGraph 把 `checkpoint` 抽象、`checkpoint-sqlite`、`checkpoint-postgres` 分开，并提供 `checkpoint-conformance` 测试套件，验证 blob round-trip、metadata、namespace isolation、incremental channel updates。

**对 ForgeHarness 的启发：** 现在只有 SQLite checkpoint/journal；未来要支持 Postgres/远程存储时，先定义 `CheckpointStore`/`InvocationJournal` 的 conformance suite，再写第二个后端。这样“可替换”不是口号，而是可验证的契约。

---

## 4. 对 ForgeHarness 的具体设计建议

### 4.1 四层边界

| 层 | 放什么 | 不允许 |
|---|---|---|
| **Core** | `RunContext`、scope、policy、approval、budget、checkpoint、trace、verification gate、tool dispatcher | 领域 prompt、法律/电商规则、UI、宿主 DB |
| **Capabilities** | 工具、指令、上下文、领域策略、验证器、hooks、memory/retrieval/planning 等可组合能力 | 绕过 core 的治理；直接写 trace/checkpoint |
| **Orchestrators** | ReAct、PlanExecute、Sequential Workflow、Graph/DAG；应用自定义 | 直接访问 DB 或绕过 dispatcher |
| **Apps / Integrations** | legal/commerce/coding 应用；LexVault DB/auth/retrieval/UI 适配 | 把领域分支写进 core |

### 4.2 组合根

```python
spec = ApplicationSpec(
    id="legal-case-qa",
    version="1",
    model_factory=...,
    capabilities=(
        LegalCitationRules(),
        CasePageTools(host),
        ChineseLegalInstructions(),
        LawyerReviewReminder(),
    ),
    orchestrator=LexVaultDagOrchestrator(...),
    policy=...,
    verifier=...,
    budget=...,
)
```

- `ApplicationSpec` 不膨胀；新增能力加 `Capability`，不加字段。
- `Capability` 只能“贡献”，不能关闭 core 的 scope/approval/budget/trace/verification。
- `Orchestrator` 只决定控制流；模型/工具调用走 `RunKernel`。
- `HostPort` 只做数据/认证/检索；不做领域判断。

### 4.3 测试

1. **Take-apart test：** 一个完整应用（如 Legal Q&A）可以由 capabilities 重新组合出来；每个 capability 可单独单测。
2. **Conformance suite：** 两个应用都跑同一套 scope/policy/approval/budget/trace/verification/resume 用例。
3. **Import boundary：** core 不能 import `applications/*`/`integrations/*`；capabilities 不能 import apps。
4. **Optional extras：** 最小安装能跑核心；装 `[legal]` 才装法律依赖；装 `[commerce]` 才装电商依赖。
5. **No dynamic import：** 只允许可信注册的 Python capability；不要学 PydanticAI 的“模型运行时写 Python capability”除非有沙箱。ForgeHarness 现有原则“Only trusted registered Python capabilities, never arbitrary import paths”要保留。

### 4.4 打包

- `forgeharness`：core，零领域依赖。
- `forgeharness-harness`：可选 batteries（filesystem、shell、memory、subagents、compaction、retrieval、planning）。
- `forgeharness-legal` / `forgeharness-commerce`：应用包，依赖 core + 需要的 harness。
- `forgeharness-integration-*`：模型/存储/宿主适配，按需安装。
- 每个包独立版本/CHANGELOG；核心 API 用 v1/v2 版本策略。

---

## 5. 对现有路线图的修改

| 阶段 | 原计划 | 加入上游研究后 |
|---|---|---|
| **F0 边界** | import-boundary + Coding shim | 增加 **core / harness / apps / integrations 分层**；盘点哪些现有模块属于哪层 |
| **F1 编排端口** | `Orchestrator`/`RunKernel` | 同时引入 **`Capability` 组合**；`ApplicationSpec` 不再直接堆字段 |
| **F2 LexVault** | 只读 host + DAG Orchestrator | LexVault 应用 = capabilities（引用/检索/指令/复核）+ DAG Orchestrator；先 4 题 |
| **F3 电商 + conformance** | 第二应用 + 同一 suite | 增加 **take-apart test**、optional extras 安装测试、两个应用的 capability 对照 |
| **F4 写路径/UI** | 审批/修订/UI | 保持；写路径用 `RevisionStore`/`ApprovalLedger`，不要新造 |

---

## 6. 不建议做的事

1. **不要把“模块化”做成“所有东西都是插件”。** core 的 scope/approval/budget/trace/verification 必须固定；否则每个应用一套治理，框架就失去意义。
2. **不要照搬 PydanticAI 的 50+ capability。** 先做 5–7 个跨应用能力；领域专用能力留在 app 包。
3. **不要照搬 ADK 的云平台耦合。** 学 Workflow/Plugin/Service 的分层，不学 Vertex/Auth 的具体绑定。
4. **不要照搬 LangGraph 的“图优先”。** PydanticAI 文档自己说“Don’t use a nail gun unless you need a nail gun”；简单 Agent 不需要图。
5. **不要把 CrewAI 的 Crew/Task 当核心抽象。** 它是应用层的高层模型；框架核心应该是 Agent/Capability/Orchestrator。
6. **不要采用 AutoGen。** 已归档；看它的 core/agentchat 分层即可，后继是 Microsoft Agent Framework。
7. **不要让模型在运行时写 Python capability。** PydanticAI 的 `CapabilityCreation` 是给有沙箱/信任边界的场景；ForgeHarness 目前没有 OS 沙箱，不能照搬。
8. **不要为了“统一”把 LexVault 的检索/审批/UI 全搬进 core。** host port 和 app 包就是用来隔开的。

---

## 7. 下一步阅读清单（已在本地）

按优先级：

1. `references/upstream/pydantic-ai/src/pydantic_ai_harness/README.md` 与 `docs/harness/index.md`：capability 组合、Coder 的拆解。
2. `references/upstream/adk-python/src/google/adk/workflow/_workflow.py` 与 `docs/guides/workflow/workflow/index.md`：图节点、并行、join、interrupt/resume。
3. `references/upstream/langgraph/libs/langgraph/langgraph/graph/state.py` 与 `libs/checkpoint-conformance/README.md`：图运行时、checkpoint 契约。
4. `references/upstream/haystack/haystack/core/component/component.py` 与 `core/pipeline/pipeline.py`：typed component/pipeline。
5. `references/upstream/openai-agents-python/src/agents/agent.py` 与 `run.py`：最小核心、handoff/guardrail。
6. `references/upstream/agent-framework/python/packages/core/agent_framework/_workflows/_workflow_builder.py`：fan-out/fan-in/switch。
7. `references/upstream/crewAI/lib/crewai/src/crewai/flow/` 与 `events/`：Crew vs Flow、事件。
8. `references/upstream/autogen/python/packages/autogen-core` 与 `autogen-agentchat`：分层；只看，不采用。

---

## 8. 来源与边界

- 所有仓库以固定 commit 浅克隆，仅用于阅读；本地路径 `references/upstream/` 已 gitignore。
- License：LangGraph/OpenAI/CrewAI/PydanticAI/Agent Framework 为 MIT；Haystack/ADK 为 Apache-2.0；AutoGen 代码 MIT、文档 CC BY 4.0。
- 本文是设计研究，不是对上游的独立安全/质量认证；上游的测试数量、生产就绪声明不自动适用于 ForgeHarness。
- 如果后续参考了具体实现，在 ADR/注释中记录来源、commit 和 license，不直接复制大段代码。
