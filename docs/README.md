# 文档导航

第一次看这个项目，建议按这个顺序读：[项目 README](../README.md) → [Quickstart](QUICKSTART.md) → [架构](ARCHITECTURE.md) → [当前能力矩阵](CURRENT_CAPABILITIES.md)。

## 核心文档

| 文档 | 内容 |
| --- | --- |
| [QUICKSTART.md](QUICKSTART.md) | 十行代码跑通一次受治理的运行、添加工具、写操作审批、代码修复应用 |
| [SDK_GUIDE.md](SDK_GUIDE.md) | SDK v1 接入步骤、能力组合、编排扩展点 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 模块边界、运行状态机、信任边界、依赖方向 |
| [CURRENT_CAPABILITIES.md](CURRENT_CAPABILITIES.md) | 每项能力的实现位置、验证证据和未证明的范围 |
| [EVALUATION.md](EVALUATION.md) | 指标、数据集划分、评测结论的使用规则 |
| [REFERENCES.md](REFERENCES.md) | 参考的规范、研究与署名 |

## 专题说明（[guides/](guides/)）

| 主题 | 文档 |
| --- | --- |
| 执行与恢复 | [调用日志与恢复](guides/M10A_INVOCATION_JOURNAL.md)、[重试与尝试](guides/M10B_RETRY_AND_ATTEMPTS.md)、[运行截止时间](guides/M10C_RUN_DEADLINE.md)、[请求预算预检](guides/REQUEST_PREFLIGHT.md) |
| 编排与多 Agent | [R2 持久编排](guides/R2_ORCHESTRATION.md)、[固定专家协作](guides/COLLABORATION.md)、[有界模型并行](guides/PARALLEL_EXECUTION.md)、[三组策略对照](guides/STRATEGY_COMPARISON.md) |
| 应用接入 | [首次接入只读工具](guides/QUICKSTART_READONLY.md)、[应用契约](guides/CONFORMANCE.md)、[法律与电商共用模块](guides/BUSINESS_MODULES.md)、[持久会话](guides/SESSIONS.md)、[应用身份与授权](guides/APPLICATION_IDENTITY.md) |
| 参考应用 | [本地模拟业务说明](guides/BUSINESS_PILOTS.md)、[电商只读接入](guides/COMMERCE_READONLY.md)、[电商工作台](guides/COMMERCE_WORKBENCH.md)、[电商运维手册](guides/COMMERCE_OPERATIONS.md) |
| 托管执行 | [PostgreSQL 状态层](guides/CLOUD_STATE.md)、[状态事务契约](guides/SAAS_STATE_CONTRACT.md)、[PostgreSQL 托管串行执行](guides/CLOUD_HOSTED_EXECUTION.md)、[邀请制控制面](guides/CLOUD_CONTROL_PLANE.md) |
| 对照与研究 | [LangChain/LangGraph 对照](guides/LANGCHAIN_COMPARISON.md)、[上游框架研究](guides/UPSTREAM_FRAMEWORK_STUDY.md) |
| 质量流程 | [最终审查协议](guides/REVIEW.md) |

## 其他目录

- [adr/](adr/)：架构决策记录，每个关键设计的背景、取舍和后果。
- [review/](review/)：审计发现与安全审查记录，`FINDINGS.json` 可被机器检查。
- [migrations/](migrations/)：知识应用的 PostgreSQL 会话表结构。
- [archive/](archive/)：已完成的阶段计划、执行计划和历史状态记录。它们保留开发过程的原始上下文，内容不一定反映当前实现；当前状态以 [CURRENT_CAPABILITIES.md](CURRENT_CAPABILITIES.md) 为准。
