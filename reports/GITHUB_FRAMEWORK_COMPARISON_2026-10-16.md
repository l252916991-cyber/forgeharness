# ForgeHarness 与 GitHub 同类项目对照

研究日期：2026-10-16（Asia/Shanghai）。仅阅读官方仓库、官方文档与本地源码；没有执行跨框架性能、质量或安全胜负测试。各仓库当时的头提交记录于 [来源清单](GITHUB_COMPARISON_SOURCES_2026-10-16.json)。未使用 Star 数量评价质量。

## 判断

ForgeHarness 已形成有工程深度的本地可嵌入原型，值得继续发展。当前最有价值的定位是：**面向受控业务资料、人工审批和可追溯执行的 Agent Harness**。将它定位为全面通用框架，还缺少真实业务验证、接入体验和部署证据。

审批、持久化、工具调用、多代理和追踪在对照项目中已普遍存在。ForgeHarness 的潜在差异在于将当前资源权限、来源版本、派生历史失效、精确审批、幂等业务键和未知结果对账组合为一套明确契约。这是值得验证的方向，尚不是已证明的竞争优势。

## 同类项目

| 项目与层次 | 官方已提供的相关能力 | 对 ForgeHarness 的启发 |
| --- | --- | --- |
| [Deep Agents](https://github.com/langchain-ai/deepagents)，直接 Harness 对照 | 基于 LangGraph，组合规划、文件系统、子代理、上下文管理、技能与人工审批；可替换模型及后端 | 优先学习较少配置即可运行的入口、长上下文处理与后端组合方式 |
| [Pydantic AI](https://github.com/pydantic/pydantic-ai)，类型化 SDK 与 Harness 生态 | 工具/结果类型、依赖注入、延期工具审批、独立评测包；当前也已有 Harness、规划、子代理和持久化能力 | 保留严格业务契约，同时降低接入样板代码；将评测与运行时解耦复用 |
| [OpenAI Agents SDK](https://github.com/openai/openai-agents-python)，轻量应用 SDK | 工具、handoff、guardrails、会话、追踪、人工审批及可序列化恢复状态 | 借鉴统一且简洁的使用路径，清晰区分 SDK 状态与宿主保存/授权职责 |
| [LangGraph](https://github.com/langchain-ai/langgraph)，底层状态编排 | checkpointer、线程状态、原生中断/恢复、子图和跨线程存储 | 对照恢复契约与副作用幂等语义；不必为了功能对齐替换独立核心 |
| [Microsoft Agent Framework](https://github.com/microsoft/agent-framework)，应用与工作流框架 | Python/.NET、结构化结果、工作流检查点、人工参与、评测及持久执行扩展 | 学习核心、后端与托管责任的划分；避免混淆内置功能和基础设施集成 |
| [OpenHands Software Agent SDK](https://github.com/OpenHands/software-agent-sdk)，编码 Agent SDK | 核心/工具/工作区/服务端分包，本地/Docker/远程工作区，持久会话、动作确认与指标 | 对照执行隔离、工作区抽象、部署迁移和真实成本统计 |

Deep Agents 与 LangGraph 属于同一技术体系的不同层次；OpenHands 偏编码代理。表格用于找设计参照，不将它们当作六个完全同层产品。

## 比较时必须保留的边界

- Pydantic 的依赖注入类型不等于租户授权；持久执行可通过 Temporal、DBOS、Prefect 等引擎集成，和单纯保存聊天不同。[依赖注入](https://pydantic.dev/docs/ai/core-concepts/dependencies/)、[延期工具](https://pydantic.dev/docs/ai/tools-toolsets/deferred-tools/)、[持久执行](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/)
- OpenAI SDK 已支持审批中断与恢复，延迟审批可序列化状态。宿主仍需安排身份、存储和实际动作边界。[官方审批说明](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals)
- LangGraph 恢复会重新执行中断所在节点，官方要求中断之前的副作用具备幂等性。应比较“写入成功但回执丢失”等场景，而非宣称它没有审批或恢复。[中断与副作用](https://docs.langchain.com/oss/python/langgraph/interrupts)
- Deep Agents 已有声明式路径读写权限；官方说明这不覆盖 sandbox 后端的任意命令执行。[权限范围](https://docs.langchain.com/oss/python/deepagents/permissions)
- OpenHands 有确定性风险分析与确认策略，官方也强调风险分析不能替代隔离环境。[安全边界](https://docs.openhands.dev/sdk/guides/security)
- Microsoft 的 Durable Task 扩展需要持久后端，但可以采用独立 worker，不能概括为必须使用 Azure。[持久执行扩展](https://github.com/microsoft/agent-framework-durable-extension/blob/main/docs/features/durable-agents/README.md)

## ForgeHarness 的证据与不足

当前源码仍匹配 0.5.0 冻结身份。源码和独立副本各通过 978 项测试、72 项门禁；干净副本纯分支覆盖 86.925%。这些证明本项目声明范围内的工程行为，不证明比上游更安全、速度更快或模型效果更好。[本地审计](business-release-audit.json)

已有价值：身份/范围由宿主管理；来源撤回能阻止派生历史继续读取或恢复；执行审批与业务内容审核分开；不明副作用进入对账路径；完整验证与安装产物绑定同一源码。这些约束可供两个业务复用。[范围与实际交付](../docs/BUSINESS_PILOTS.md)

主要不足：

1. **当前业务质量证据不足。** 法律/电商新链路使用确定性模型及合成资料，报告明确 `business_qualified=false`。旧版本已有有限的本地真实模型编排实验，但不能用于证明 0.5.0 业务效果，更不是与上述上游的同题评测。[历史实验范围](../docs/M8B_LIVE_COMPARISON.md)
2. **接入成本需要实测。** SDK 同时暴露运行、工作流、资料、会话、任务和策略等概念。建议让独立开发者接入一个只读助手和一个审批写入助手，记录接入时间、样板代码和错误配置，而非凭概念数量评价易用性。
3. **生产与隔离边界有限。** 已验收范围是本地 POSIX/SQLite；身份与来源验证依赖可信宿主配置。`tools/process.py` 使用普通子进程、工作目录与环境过滤，不能将它描述为 Docker/虚拟机级隔离。分布式恢复、生产认证和真实外部写入仍需独立验证。
4. **资料治理需要正确声明。** 受治理来源依赖工具的 `governed_sources` 声明及宿主验证器；不能声称任意第三方工具自动获得完整来源撤回保护。
5. **旧对照文档有过时口径。** `docs/LANGCHAIN_COMPARISON.md` 中的“custom callback”“experimental”等描述，应明确为本仓库适配器的实现状态。官方现有原生中断/恢复及完整[子图文档](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)。建议后续改为“上游能力 / 本地适配范围 / 实测结果”三列。本次研究未修改冻结源码或该历史文档。

## 建议的下一步

优先选择 Deep Agents 与 Pydantic AI Harness 做受控对照，固定模型、任务、预算、工具与资料；为每个框架配置其推荐的持久化、审批和隔离后端。分别报告默认接入成本及达到相同业务契约后的集成成本。

第一组比较六类确定性故障：等待期间撤权、资料撤回后追问、审批参数被改动、写成功但回执丢失、重复提交、取消与写入同时发生。衡量是否越权、重复写入、虚报完成，以及恢复所需人工介入；保留失败轨迹。

第二组再用真实模型与独立业务题衡量事实和引用正确性、澄清/转接表现、token、时延与维护成本。当前合成开发场景只能作契约回归；既有封存集继续遵循其治理规则，不为这次资料对照打开。

产品上先收窄到一个有真实使用者的闭环，完成无商品工单、自然语言澄清、真实身份与资料接入。若相同故障测试显示这些约束用现成框架即可低成本实现，应优先把资料治理和审计能力做成可复用集成；若自有核心有清晰收益，再继续扩大运行时范围。
