# 从优秀 Agent 项目中借鉴什么，以及如何完善 ForgeHarness

日期：2026-10-16。结论来自固定提交的源码阅读与本地缺陷回归；没有新增跨框架性能、真实模型质量或安全胜负测试。

## 结论

值得借鉴，而且现在最有收益的是：**把已有的权限、审批、恢复能力做得更容易接入，并把上下文和预算计算做得更准确。** 不需要为了接近上游功能表而重写核心、扩展代理数量或引入编排框架。

本轮已落实一项生产代码修复：资料预算包含来源标签、完整渲染和分隔符。其余设计按下面的价值、影响和验收条件分阶段实施。电商应用可以在既有公共 SDK 上继续开发，不需要等待全部研究完成。

## 研究对象与证据

选择依据是公开实现中有清楚的运行循环、类型/存储/执行边界及测试，且可以回答 ForgeHarness 当前的具体问题。没有以 Star 排名代替质量判断。四个项目不是同一产品层次，尤其 OpenHands 和 smolagents 偏编码/通用工具执行。

| 项目 | 本轮阅读提交 | 为什么值得拆解 |
| --- | --- | --- |
| [Deep Agents](https://github.com/langchain-ai/deepagents/tree/c5313d7005534c8f8514004890c6f6d5174c6e2f) | `c5313d7005534c8f8514004890c6f6d5174c6e2f` | 预装能力的组合、长上下文、存储后端协议 |
| [Pydantic AI / Harness](https://github.com/pydantic/pydantic-ai/tree/2c23dd01b804dd238e52f70754cdbdb7b8ace70d) | `2c23dd01b804dd238e52f70754cdbdb7b8ace70d` | 类型校验、校验与执行拆分、使用量和上下文边界 |
| [OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk/tree/12e967664c2353f4cb85dd27da04ade035ff078c) | `12e967664c2353f4cb85dd27da04ade035ff078c` | SDK/工作区/服务边界、先持久化再通知 |
| [smolagents](https://github.com/huggingface/smolagents/tree/96f33faaf028479119ec8d34507b47694cf14e34) | `96f33faaf028479119ec8d34507b47694cf14e34` | 简洁入口、显式步骤记录、执行器可替换 |

通过 GitHub 连接器先固定 HEAD，再取得指定文件。共保存 17 个源码/许可证文件，逐一核对 Git blob SHA，记录 SHA-256、字节数和永久链接，见 [来源清单](upstream-design-study-2026-10-16/sources.json)。普通终端访问 GitHub 的 API 限额和 Git 连接失败后，改用连接器完成读取；没有执行下载的源文件或安装新上游包。

前三个项目的根许可证为 MIT，smolagents 为 Apache-2.0；原文与署名保留在研究目录。本轮只借鉴设计思想，没有复制上游实现到发行代码。以后移植任何具体实现时，需再核对对应文件及子包的许可。

这批 GitHub 提交是**本次静态研究版本**。此前 33 项集成用例实际运行的是独立锁定的 Deep Agents 0.7.25、Pydantic AI Harness 0.56.0 / SDK 2.56.0；不可把两者视为同一版本，也不能让此前通过结果为新 HEAD 背书。

## 1. Deep Agents：学习上下文治理和能力组合

源码入口是 [`create_deep_agent`](https://github.com/langchain-ai/deepagents/blob/c5313d7005534c8f8514004890c6f6d5174c6e2f/libs/deepagents/deepagents/graph.py)，通过组合中间件装配文件、规划、摘要和可选委派；读写后端由 [`BackendProtocol`](https://github.com/langchain-ai/deepagents/blob/c5313d7005534c8f8514004890c6f6d5174c6e2f/libs/deepagents/deepagents/backends/protocol.py#L404) 表达，命令执行另由 `SandboxBackendProtocol` 表达。可替换后端比把存储和运行循环绑死更适合扩展。

[`_count_tokens`](https://github.com/langchain-ai/deepagents/blob/c5313d7005534c8f8514004890c6f6d5174c6e2f/libs/deepagents/deepagents/middleware/summarization.py#L1034) 在计数器支持时一起计算消息、系统消息与工具定义。摘要模块还保留被移出的历史及访问路径，并区分自动触发和显式压缩。它体现的原则是：缩小模型工作窗口时，保留可以检查的原始依据。

ForgeHarness 已有 `ContextCompiler`、按轮保留的 `ContextAssembler` 和来源失效验证。真正的差距是完整请求开销与分层压缩，而不是“没有上下文管理”。

建议先做完整请求的估算清单，再考虑“旧工具结果压缩 → 按完整轮次裁剪 → 可选语义摘要”。资料撤回后，摘要及移出历史也要重新验权；不得把摘要当成可信系统指令或让磁盘引用绕过租户范围。电商价格和库存必须重新查询，不能从压缩历史中恢复成当前事实。上游的具体默认阈值不直接搬到本地小模型。

## 2. Pydantic AI：学习类型化的执行阶段和预算语义

[`ToolManager`](https://github.com/pydantic/pydantic-ai/blob/2c23dd01b804dd238e52f70754cdbdb7b8ace70d/pydantic_ai_slim/pydantic_ai/tool_manager.py#L658) 将参数校验和执行区分；自定义校验与延期审批各有阶段规则。它让不合法请求先得到校验反馈，再进入需要人工处理的阶段。类型信息也贯穿结果和运行依赖。

[`UsageLimits`](https://github.com/pydantic/pydantic-ai/blob/2c23dd01b804dd238e52f70754cdbdb7b8ace70d/pydantic_ai_slim/pydantic_ai/usage.py#L485) 明确区分运行累计输入、输出、模型请求数和单次输入限制。其源码也说明模型请求计数不等于底层 HTTP 重试次数；预先计数需要显式开启，并非默认就能避免所有超预算请求。Harness 的[上下文测试](https://github.com/pydantic/pydantic-ai/blob/2c23dd01b804dd238e52f70754cdbdb7b8ace70d/tests/harness/compaction/test_context_budget.py#L817) 覆盖工具新揭示后 schema 带来的成本。

ForgeHarness 已有 Pydantic/JSON Schema、类型化 Model、Verifiers、协议纠正预算和安全工具重试。应保留这些约束；模型纠正参数和自动重新执行副作用不是同一件事，不能为了“自动恢复体验”合并它们。

建议新增一个可检查的完整请求预算阶段：材料、最终消息、工具 schema、预留输出和适配器开销分别记录，未知模型窗口由宿主配置。后续接入支持内部重试的模型适配器时，显式记录“逻辑模型调用”与“物理 HTTP 尝试”的区别。真实费用仍须有用量和计价依据。

## 3. OpenHands：学习执行环境边界与持久化顺序

[`BaseWorkspace`](https://github.com/OpenHands/software-agent-sdk/blob/12e967664c2353f4cb85dd27da04ade035ff078c/openhands-sdk/openhands/sdk/workspace/base.py) 定义命令、文件和生命周期接口；[`DockerWorkspace`](https://github.com/OpenHands/software-agent-sdk/blob/12e967664c2353f4cb85dd27da04ade035ff078c/openhands-workspace/openhands/workspace/docker/workspace.py) 处理容器、挂载和网络等执行配置。这种拆分使 Agent 核心与执行位置分开，但安全结果仍取决于挂载、网络、用户权限和宿主配置。

[`LocalConversation`](https://github.com/OpenHands/software-agent-sdk/blob/12e967664c2353f4cb85dd27da04ade035ff078c/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py#L431) 将状态落盘回调放在调用者回调之前；持久化失败时，不继续向后面的订阅者发送可执行的通知。源码同时说明本地可视化可能先于持久化，因此不能概括为所有输出都先落盘。

ForgeHarness 的普通子进程工具已有环境过滤、输出限制和取消清理，但不是操作系统沙箱。现有 SQLite、journal 和 hash trace 也已有持久化职责，不需要再复制一个会话状态体系。

借鉴路径是：需要运行不可信代码时再引入执行后端协议，保持本地模式的诚实标注；未来做流式 UI/外部事件时，对“落盘失败但已通知完成”注入故障测试。电商只读 API 和受控工单工具不应为了形式统一而开放命令执行。

## 4. smolagents：学习更短的使用路径和明确的步骤对象

[`run` 与步骤循环](https://github.com/huggingface/smolagents/blob/96f33faaf028479119ec8d34507b47694cf14e34/src/smolagents/agents.py#L436) 将工具、模型、步骤限制和最终检查放在较容易找到的位置；[`ActionStep / AgentMemory`](https://github.com/huggingface/smolagents/blob/96f33faaf028479119ec8d34507b47694cf14e34/src/smolagents/memory.py#L51) 把观察、错误、调用与时间信息组织成步骤。执行器也可替换，而不是把执行方式固化到模型协议。

ForgeHarness 有类似的 trace step 投影及 SDK 示例。需要借鉴的是接入路径：让开发者先完成一个只读助手，再增加审批写入；不要让每个使用者在第一次调用前理解所有运行时组件。

不能直接照搬的部分：本地 Python 执行器不能作为不可信代码的安全边界；达到步骤上限后仍生成最终文字，也不能在本框架中自动解释为任务成功。ForgeHarness 的成功状态继续由持久状态和验收器决定。所谓接口更简单，必须通过实际接入验证；本轮没有独立开发者工时数据。

## 落到本项目的优先级

| 优先级 / 状态 | 具体改进 | 主要落点 | 验收条件 |
| --- | --- | --- | --- |
| P0：本轮已修复 | 资料完整渲染计入预算，纠正估算器说明 | `context/compiler.py` | 必选标签溢出不调用模型；可选大来源跳过后仍可选小项；分隔符与取整边界通过 |
| P1：建议下一轮先做 | 完整模型请求的预算预检与明细 | `context/assembly.py`、`models/base.py`、`runtime/loop.py` | 小窗口＋大 schema 在请求前拒绝；工具定义改变后重算；最终发送与 trace 一致；未知窗口不伪称准确 |
| P1：与电商第一版一起做 | 精简只读和审批写入接入路径 | `sdk/application.py` 与独立示例 | 两个示例只用公共 API；保留宿主身份、scope、policy、verifier；撤权、参数变化、重新启动回归通过；接入工时待独立参与者 |
| P1：接真实模型时做 | 对齐协议纠正、模型请求及传输重试的统计 | 模型适配器、`runtime/budget.py`、trace | 失败请求不会消失；物理尝试单列；不会扩大原有副作用重试范围；故障注入后预算与恢复计数一致 |
| P2：需要长对话时做 | 受来源治理约束的分层上下文压缩 | `context/compression.py`、sessions/sources | 工具调用与结果成对；撤回来源后摘要/历史也失效；保留完整输入证据；用开发题衡量质量与用量 |
| P2：需要不可信代码执行时做 | 可替换工作区/执行后端 | `tools/process.py`、coding 工具 | 对越界挂载、网络、环境、取消、重启做真实后端验证；本地默认不冒充沙箱 |
| P2：需要外部事件订阅时做 | 先持久化，再发布运行事件 | session/task 控制适配器 | 持久化失败不发完成；订阅者失败不改已提交业务事实；重复事件可识别 |

已经具备的审批账本、当前资源验证、未知结果对账和最终验收，应通过原有门禁保持，而不是更换成只有提示词说明的流程。多 Agent、通用 DAG、大规模插件市场和自动自我修改不列为本轮优先项。

建议下一次实现范围：**完整请求预算预检＋电商只读咨询接入样例**。这两项可以直接帮助电商落地；工作区容器化仅在确有代码执行需求时启动。原生完整故障对照仍按原计划补证据，不作为现在不能开发电商的前置理由。

## 本轮改动、验证与精确边界

修复前：正文 `1`、来源 `product/` 加 120 个字符，在上限为 1 时被接受；报告为 1，但完整文本在同一估算器下为 40。

修复后：先渲染资料块与分隔符，再对完整候选文本估算。必选溢出抛出 `ContextBudgetError`；可选溢出跳过，继续考察后续候选。保留排序、来源标签格式与公共类型，不增加依赖或数据库迁移。

`CompiledContext.estimated_tokens` 对应输出文本的完整估算；单个 decision 是该候选渲染增量的独立估算，不能假设可直接加总。默认每四字符一个 token 仍只是启发式，尤其不保证中文或真实 tokenizer 的上界。逐候选重新估算的成本取舍见 [ADR 019](../docs/adr/019-rendered-material-budget.md)。

本轮并未完成整个模型请求的预检：工具 schema、provider 包装和预留输出仍需下一轮处理。也没有完成真实电商质量、生产认证、分布式恢复或各上游原生完整故障对照。

审查见 [审查记录](../docs/review/UPSTREAM_DESIGN_REVIEW.md)。完整门禁使用独立目录，不覆盖此前冻结：

```bash
uv run --no-sync python reports/upstream-design-study-2026-10-16/verify_candidate.py --freeze
# 同一源码复验时省略 --freeze；源码不匹配时拒绝沿用报告。
```

最终通过与否、测试数和覆盖以 [新候选验证摘要](upstream-design-study-2026-10-16/verification/summary.json) 及其不可覆盖的 attempt 记录为准。未打开封存业务集，未使用真实资料或付费模型。新门禁通过也只认证声明的本地工程范围。

## 最终验收确认（2026-10-17）

本轮首项改进已经通过完整工程验收。冻结身份为
`28bf1d7aa39c8b1d6b62148850b15a363848bc75fcce849fd52dc8dad2a5489a`，
368 个文件；确认时当前源码仍匹配该身份。源码与干净副本各通过 1017 项测试，
无失败、错误或跳过，纯分支覆盖均为 86.888%；84 项门禁全部通过。
锁定版本的三方组合场景在两个目录各通过 33 项，真实 wheel/sdist 与最小依赖安装验证通过。
旧冻结及报告保留，没有用新的测试数量覆盖历史结果。

详细结论见 [本轮审计](upstream-design-study-2026-10-16/audit.json) 和
[执行状态](UPSTREAM_DESIGN_EXECUTION_STATUS_2026-10-17.md)。完整请求预算、简化接入与其他建议
仍未实现；四个项目的源码优点不是 ForgeHarness 已获得相同能力或更优效果的证明。
