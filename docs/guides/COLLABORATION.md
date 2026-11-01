# 固定专家协作

SDK 0.9.1 / API v1；固定分工设计见 [ADR 034](../adr/034-fixed-specialist-collaboration.md)，
完整路线见 [执行计划](../archive/EXECUTION_PLAN_MULTI_AGENT.md)，下一批次见
[并行执行计划书](../archive/EXECUTION_PLAN_PARALLEL_EXECUTION.md)，接入与容量/恢复契约见
[并行说明](PARALLEL_EXECUTION.md)。当前候选证据见 `reports/parallel-execution-20261027/REPORT.md`；
0.8.6 交付保留于 `reports/multi-agent-20261026/REPORT.md`。旧候选不认证本轮源码。

## 接入与运行

```bash
uv run --no-sync python examples/collaboration_demo.py
```

无需本地模型、网络或密钥。示例由三个新进程完成：两名专家处理不同的合成契约；
第二名专家的合成响应在保存回执后模拟丢失；新进程查询原回执对账并运行协调者；
第三个进程恢复终态，断言没有重复模型调用。用量是测试替身数据，不代表真实成本。
示例默认仅使用临时目录，模型服务可以保持停止。

宿主使用以下公开类型：

- `CollaborationAgent`：注册专家 ID、版本、可信指令与一次决策最大用量。
- `CollaborationEvidence`：宿主已授权的快照，包含证据 ID、资源 ID、版本与原文；
  `text_sha256` 由原文计算。摘要证明字节身份，真实性与授权仍由宿主保证。
- `CollaborationTask`：稳定任务 ID、专家 ID、目标、允许读取和引用的证据 ID。
- `CollaborationPlan`：版本、模型配置身份、公共目标、专家/证据/任务、协调者。
- `AgentReport` / `AgentFinding`：任务 ID、摘要、带证据 ID 的发现、未解决问题。
- `CollaborationSnapshot` / `DelegationSnapshot`：经过授权检查的持久任务树视图。

```python
from forgeharness.sdk import Collaboration, RunBudget

team = Collaboration(
    plan=registered_plan,
    model_factory=registered_model_factory,
    budget=RunBudget(
        max_steps=3, max_tool_calls=0, max_input_tokens=24_000, max_output_tokens=3_000
    ),
    state_dir=private_state_dir,
    authorize_run=host_authorize_current_execution_binding,
    validate_resources=host_validate_all_current_resource_revisions,
)
result = await team.harness.run(context=authenticated_context, task=team.plan.goal)
snapshot = team.inspect(authenticated_context)
```

完整上下文构造见可执行示例。默认 application ID 是 `collaboration`，application version
为 plan.version，policy version 是 `collaboration-readonly-v1`。认证后的 actor/scope 来自
宿主。资源 guard 每次检查全部受影响的来源版本、删除、过期与权限，包括 inspect、
resume 和对账；不能仅比较传入 map 自身，也不能根据模型输出推断权限。
两个 guard 必须显式提供可调用对象。state_dir 沿用 Harness 的 0700 要求。

专家 ID 可重复使用，任务 ID 必须唯一；`synthesis` 保留给协调者。定义支持 2–8 个
专家任务；每个任务对应一个 R2 `specialist.<task_id>` 节点，协调者依赖全部专家。
所有专家共用注册模型工厂；model_identity 必须绑定真实 provider/config 的受控版本。
若需要不同模型或自主工具循环，需要后续扩展协议，不能通过角色字符串取得能力。

## 治理与失败行为

公共 root task 对所有节点可见，只放共同目标。节点的独立 evidence 消息只含分配材料，
协调者只接收公共目标和专家结果，不自动接收原始材料。额外来源不会被隐式共享。
指令、快照、版本、schema、预算与模型身份进入 R2 完整身份绑定；定义变化需新运行。

每个专家和协调者分别预留一次模型决策与声明的 token 上限。构造时检查所有声明的
预留总和能被根预算覆盖；运行时仍由 R2 原子准入与结算。工具 registry 和目录均为空，
策略拒绝任何工具。非法工具 action 失败，不能执行文件写入、命令或网络工具。

模型必须返回 JSON object，符合节点 output_schema：任务归属正确、字段与长度合法、
引用属于分配集合。重复 JSON key、非有限数字、非 JSON、越界引用会在节点结算前失败，
实际消耗仍计账；依赖失败则协调者不执行，独立分支的已知结果保留。
报告必须至少包含一条发现或未解决问题；材料不足时可以明确报告未解决问题。

继续使用 `team.harness.resume/cancel/reconcile/delete`。取消是停止后续准入的请求，
不会证明正在进行的调用已停止。响应或消耗未知时保留 reservation，不自动重发；
宿主可选注册 `reconcile_node`，只查询已有回执并返回 `ReconciledNode` 的规范结果、
总实际用量和证据引用。对账结果也通过相同 schema 校验，不能绕过专家输出边界。

`team.inspect(context)` 展示根 run、delegation ID、专家版本、状态、实际用量、预留和
已完成报告。delegation ID 由内部命名空间 run ID 与节点 ID 组成，不同租户或同角色的
不同任务不会混同。控制操作仍使用原公共 RunContext。R2 状态和 trace 是事实来源；
这里没有独立可恢复 child run，也没有新状态数据库。

## 能力与验收边界

本实现为单机 POSIX/SQLite、有限固定图、默认串行且可显式选择并发 2、一次 final-only
专家决策、只读材料分析。自主工具循环、动态递归、隔离写入和分布式调度尚未交付；
目标后端并行吞吐和时延以同拓扑实验报告为准。
引用校验只证明结构和来源身份；专业与语义正确性需领域验收。默认终验原因明确标注
`semantic support not assessed`，不能用通过标志宣称结论被事实语义支持。

0.9.1 为新发行身份、R2 schema 3 / recovery 5；既有 0.8.x/0.9.0 R2 运行保留原 wheel，
本轮不隐式接管旧 checkpoint。
本轮不升级业务宿主。源码候选是否合格只读同一候选的独立冻结和完整验证摘要。
