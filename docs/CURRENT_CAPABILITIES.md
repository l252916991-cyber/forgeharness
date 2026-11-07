# 当前能力、证据与宿主兼容矩阵

适用版本：0.13.0（2026-11-02）。本页列出每项能力的实现位置、已有验证证据，以及尚未证明的范围。
各轮候选的完整资格以对应报告为准；下文「版本历史」保留每轮的原始说明。

当前版本概况：

- **执行内核**：ReAct 与 Plan-Execute 运行时、精确审批、预算、调用日志与对账、checkpoint 恢复、哈希链 trace；
  每个带工具的请求都会写明「一步一个工具调用」（[ADR 048](adr/048-pinned-protocol-notice.md)），参数格式错误会指出字段和约束（[ADR 047](adr/047-field-level-schema-feedback.md)）。
- **编排与多 Agent**：R2 持久图、固定专家协作、有界模型并行（见下表）。
- **托管执行**：PostgreSQL 状态层、托管串行执行，以及邀请制内部 Alpha 控制面（[报告](../reports/saas/20261101-control-plane/REPORT.md)）。
  生产 IdP/MFA、人民币额度、多 worker 容量、公网部署、备份/SLO 与真实业务仍未验收。
- **真实模型评测**：DeepSeek `deepseek-flash` 的基线、失败分析与改进前后对比见 [实测报告](../reports/deepseek-flash/REPORT.md)；
  固定专家协作与单 Agent 的规模对照见 [规模实验](../reports/collaboration-scaling/REPORT.md)，协作在各规模都没有达到事先封存的判定条件。
  评测数据是合成的，不代表真实业务质量。

| 能力 | 当前实现 | 已有验证定位 | 未证明的范围 |
|---|---|---|---|
| PG 托管串行执行 | 现有 AgentRuntime、完整输入/结果/证据、精确审批、预算与epoch恢复 | `cloud/runtime.py`、真实PG/HTTP/独立账本故障矩阵；工程资格见[本次报告](../reports/saas/20261030-hosted/REPORT.md) | 内部可信宿主API；通用身份/UI、集群调度、PG R2/并行/会话、账单与真实云上线未完成 |
| 固定专家协作 | 已实现：独立材料分配、结构化结果、持久 delegation 节点和协调者；显式支持并发 2 | `sdk/collaboration.py`、`test_collaboration.py`、`test_parallel_execution.py`；新候选工程资格见独立报告；真实模型对照见[规模实验](../reports/collaboration-scaling/REPORT.md)和[消融实验](../reports/collaboration-ablation/REPORT.md) | 一次决策；自主循环、写隔离未验证；规模实验中各规模都不优于「读完后一次调用」；32 份材料时专家层漏报，消融未找到达到判定门槛的原因 |
| 有界模型并行 | 单 owner/writer；run 上限 1/2、共享 provider 容量、有界轮转队列、工具屏障、持久发送与取消/对账 | `sdk/parallel.py`、`sdk/parallel_runner.py`、`test_parallel_recovery.py`；[本轮报告](../reports/parallel-execution-20261027/REPORT.md) | 单进程/容量域；后端实际提速按同拓扑实验判定；无并行工具/分布式保证 |
| R2 模型输出 schema | 本地 schema、普通结算和对账共用校验；错误结果保留用量 | `runtime/structured.py`、`test_reconciliation_cannot_bypass_result_schema` | 引用身份不等于语义支持 |
| ReAct 状态、审批、恢复 | 已实现 | `sdk/harness.py`、`state/approval.py`、`state/invocation_journal.py`；历史候选全量测试 | 外部 exactly-once、跨主机 failover |
| 任意 Orchestrator / RunKernel | 只读、one-shot | `sdk/kernel.py`、`tests/unit/test_orchestration_kernel.py` | 写、审批、resume 明确不支持 |
| R2 持久节点、审批、预留、对账 | 显式有限图已实现 | `sdk/durable.py`、`state/orchestration.py`、`tests/unit/test_durable_orchestration.py` | 不是任意协程栈持久化 |
| 分支与 join | 结果持久化；显式允许独立模型重叠；依赖按声明顺序输入 | `test_barrier_real_overlap_bound_and_join`、`test_parallel_approval_collection_and_join_are_durable` | 工具仍独占；目标后端吞吐单独测量，失败终态不自动重试 |
| 未知模型/工具结果 | 保留 reservation；可信 resolver 查询已有回执 | 同文件 `test_unknown_effect_never_replays_even_when_idempotent`、`test_model_response_loss_holds_reservation_and_never_resends` | 不能凭回执缺失推断未执行 |
| owner fencing、取消、outbox | 单机 POSIX flock + SQLite、持久请求、至少一次事件交付 | 同文件 owner/cancel/outbox 测试；[ADR 031](adr/031-durable-orchestration-r2.md) | 非分布式 lease/调度/HA；消费者仍需去重 |
| 工具权限/审批内容绑定 | 当前授权、完整 run/graph/model/resource/budget 身份重验 | R2 active identity、revoked approver、zero-step 回归 | 宿主身份回调不是内置 IAM；路径限制不是 OS sandbox |
| 三组策略比较 | A ReAct、B 固定流程、C planner/worker/reviewer 的开发诊断实验工具已实现 | `benchmarks/strategy_comparison.py`、`tests/unit/test_strategy_comparison.py` | 最新真实模型状态见执行报告；短合成题不是代码修复或业务终评 |
| 原生框架完整比较 | 未完成 | 历史 33 项共同宿主组合全部通过 | `native_full_fault_comparison_complete=false`、`core_advantage_proven=false` |
| 独立接入/学习 | 文档和可执行自测已准备 | [只读 Quickstart](guides/QUICKSTART_READONLY.md)；`test_readonly_quickstart.py` | 真人首次接入时间、无 AI 定位记录待本人完成 |

实现文件前缀均为 `src/forgeharness/`。历史文档里 F1c-2 “待实现”描述只适用于当时的
one-shot 路径；R2 后续实现不应据此重复开发。`docs/guides/CONFORMANCE.md` 是参考应用子矩阵，
不是 SDK 能力总表。

| 宿主 | 绑定 wheel / SHA-256 | API / 使用路径 | 兼容性边界 |
|---|---|---|---|
| LexVault | 0.8.3 / `1d5880ed2ba1cabcb20b0970a55727bcca7b68f9b4beace70ce7600d58ba7555` | API 1；只读 worker、R2 sidecar、隔离领域任务入口 | 存在真实任务入口实现；不能从旧 binding 的只读 purpose 推断后续任务不存在。正式 HTTP/UI 切换及真实法律质量另验 |
| ForgeCommerce | 0.5.0 / `58579893b83368a36d0251393ef7c2d742b947bf184c434558987972732f56fa` | API 1；ApplicationSpec、SessionService、HTTP ASGI、本地工单 | 无 R2 绑定；按本版本验收，不强制迁移为 0.8.3。测试租户不等于真实商家系统 |

静态检查的实际清单由 `benchmarks.improvement_audit` 产生：逐宿主绑定/wheel 摘要，
代码文件摘要，全部显式 `from forgeharness...` import 的 wheel 符号与 SDK facade 导出。
它不动态导入宿主、不验证任意反射导入或行为兼容；运行兼容需结合宿主测试和进程演练。
两个宿主还有 SDK facade 外的 `models`/`tools.base`/法律应用 import；已按精确 wheel 核对，
不承诺这些模块具有 API facade 同等的长期兼容保证。后续升级先按宿主分别回归。

当前 R2 orchestration schema 3 / checkpoint 2 / recovery 5，与历史 R2 schema 2 / recovery 4
及 ReAct checkpoint 1 / recovery 3 区分。旧运行保留原 wheel；没有隐式升级、降级或
跨框架 checkpoint 转换。并发和容量配置也绑定运行身份；新 run 才可更换执行配置。

工程边界仍为 Python 3.12、单机 POSIX/SQLite。现有 package 元数据允许 3.13 不等于本轮
完整验收该版本。框架优势、业务合格、生产 SLO、专业法律正确性继续不从工程门禁推出。

## 版本历史

2026-11-01；0.13.0 新增[通用邀请制内部 Alpha 控制面](guides/CLOUD_CONTROL_PLANE.md)：
PG 账户及组织/工作空间/部署授权、受理及发送前重验、异步 HTTP、独立串行 worker、
持久审批批准/拒绝和基础浏览器工作台。工程资格只读取[本轮冻结报告](../reports/saas/20261101-control-plane/REPORT.md)。
生产 IdP/MFA、人民币月额度、多 worker 容量、公网部署、备份/SLO 与真实业务仍待独立验收。

0.12.0 历史候选已实现[PG 托管串行执行](guides/CLOUD_HOSTED_EXECUTION.md)，原工程资格读取[历史报告](../reports/saas/20261030-hosted/REPORT.md)。

2026-10-29；最近已验收冻结 0.11.0 / API v1，新增独立 [PG 状态首切片](guides/CLOUD_STATE.md)。
资格见 `reports/saas/20261029-state/REPORT.md`；SDK 接线、通用平台控制面及生产运营尚未完成。

2026-10-28；历史源码候选 0.10.0 / API v1，新增邀请制本地工作台与当前账户授权复核；
执行和资格见 `reports/saas/20261028-batch1/REPORT.md`，使用见[工作台指南](guides/COMMERCE_WORKBENCH.md)。
PG SDK 后端、集群调度、SaaS 收费和真实业务仍未验收。下述证据保留各自版本身份。

2026-10-27；历史 SDK 源码候选 0.9.2 / API v1。新增电商参考应用通用人工转接、
本地备份恢复与运维边界，执行状态见
`reports/productization-20261027/REPORT.md`；真实宿主与业务资格仍未验收。0.9.1 的有界模型并行接入见
[并行说明](guides/PARALLEL_EXECUTION.md)，其历史候选资格与性能证据见
`reports/parallel-execution-20261027/REPORT.md`。0.8.6 固定专家交付的历史证据保留在
`reports/multi-agent-20261026/REPORT.md`，接入见 [协作说明](guides/COLLABORATION.md)。
下述 0.8.5 与 0.8.3 数字保留各自历史身份，不认证新候选。

0.8.5 显式图能力沿用 0.8.3，新增每次 kernel 模型调用的工具目录控制、实验/演练工具、
文档与依赖安全刷新，并单独标版本防止不同 wheel 混用。这里区分实现、已有候选证明和本轮新增验证。
历史来源为 [0.8.3 候选](../reports/release-0.8.3-r1/verification/summary.json)：
源码及干净副本各 1178 tests，92 工程门禁。该数字不直接覆盖本轮新工具和文档。
本轮最终结果读取 [执行报告](../reports/improvement-20261026/REPORT.md) 与其中的新候选摘要。
