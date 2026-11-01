# 有界模型并行

SDK 0.9.1 / API v1。设计：[ADR 035](adr/035-bounded-model-parallelism.md)、
[调用身份修正 ADR 036](adr/036-scoped-model-invocation-identity.md)；
计划：[执行计划](EXECUTION_PLAN_PARALLEL_EXECUTION.md)；候选与性能结论以
[本轮报告](../reports/parallel-execution-20261027/REPORT.md)为准。

## 接入

R2 与 Collaboration 可以同时等待两个独立 final-only 模型请求。配置默认并发 1；
工具仍在本 run 内独占执行。只有全部前驱成功并通过 schema 校验，综合节点才开始。
专家是持久节点的一次决策；角色数量与物理并发槽位分开管理。

```python
from forgeharness.sdk import Collaboration, ModelDispatcher, ParallelExecution

# Runs using the same provider share this dispatcher and its private directory.
with ModelDispatcher(
    state_dir=private_provider_capacity_dir,
    provider_id="registered-local-model-v1",
    capacity=2,
    max_queue=64,
) as dispatcher:
    group = Collaboration(
        plan=registered_plan,
        model_factory=registered_model_factory,
        budget=registered_root_budget,
        state_dir=private_run_state_dir,
        authorize_run=host_authorize_current_binding,
        validate_resources=host_validate_current_resources,
        execution=ParallelExecution(
            max_in_flight=2,
            model_concurrency_safe=True,
        ),
        dispatcher=dispatcher,
    )
    result = await group.harness.run(context=authenticated_context, task=group.plan.goal)
    snapshot = group.inspect(authenticated_context)
```

同样两个参数可传给 `DurableOrchestrator`。并发 2 必须配置 dispatcher，并由宿主确认
模型适配器允许并发请求：没有跨请求共享的可变 messages、角色状态或用量计数。
即使单个 run 配置为 1，只要共享 provider 容量为 2，也必须提供该并发安全声明。
适配器还应异步等待 I/O、配合取消，不能阻塞整个 event loop。该布尔声明是可信宿主
注册契约，模型输出不能授予此能力。`ScriptedModel` 的顺序响应队列不适合直接作为
按角色寻址的并行模型；示例使用独立请求身份选择合成响应。

使用 `OpenAICompatibleModel` 时推荐设置 `require_usage=True`，缺失或不完整的用量
不能变成零成本。该选项为旧调用保留默认兼容行为；真实并行评测显式启用严格模式。
声明 model_identity 时需绑定实际 provider、模型、请求参数和用量校验配置的版本。
HTTP 客户端由创建它的宿主在所有调用收尾后关闭。

```bash
uv run --no-sync python examples/parallel_demo.py
```

此示例不启动模型、不访问网络：两个专家先一起到达事件屏障，再返回结果；第二名
专家保存合成回执后模拟丢失响应。随后两个新进程分别对账/综合与复用终态，断言没有
重复推理。scripted tokens 不代表真实费用，临时材料与回执自动清理。

## 状态、预算和控制

`group.inspect(context)` 包含并发上限、每节点 invocation ID、实际用量、reservation、
dispatch_phase 和 execution_state。常见执行视图为：

| execution_state | 含义 |
|---|---|
| ready | 依赖已完成，等待 run 调度 |
| waiting_dependencies | 前驱尚未全部成功 |
| waiting_capacity | 已排入 provider 容量队列，尚未发送 |
| admitted | 已预留根预算，正在准备规范请求和发送许可 |
| sent | 已有持久发送许可，不能据此确认 owner 或后端当前仍存活 |
| needs_reconciliation | 响应/消耗未能确认，必须查询已有回执 |
| completed / failed / cancelled | 已知节点结果；失败消耗仍记账 |

R2 完整状态还记录排队、获容量、发送和完成时间，以及 prepared payload 摘要。
规范输入保存在发送前 fsync 的 trace，所有分支由一个 writer 串行写入 hash 链。
worker 的 provider 观察时间保存在事件 payload；append 顺序是提交顺序，综合输入
顺序仍按定义依赖排列。trace outbox 至少一次交付，按稳定 event_id 去重。

所有准入由根 owner 原子预留。对于 admitted/unknown 节点，每个用量维度按
`max(已知用量, reservation)` 占用，不能把同一份额度重复借给两个 worker。
已知实际用量超出声明时仍如实记录、失败并停止新准入。材料失效或撤权后停止发送
和对外暴露，已经发生的消耗可通过内部 owner-fenced 路径结算。

`cancel()` 是持久停止新准入的请求；已经发送的请求先有界收尾。
`request_timeout_seconds` 默认 120，`drain_timeout_seconds` 默认 5；未知调用不自动重发。
本地等待取消不等于模型服务已经停止。未确认的请求继续占用预算和 provider 容量，
也不能让本 run 的工具越过互斥屏障。适配器若拒绝本地取消，dispatcher 拒绝正常关闭，
宿主需排查并停止该 worker 进程；上述时限不是可强制终止任意 Python/provider 的保证。

## provider 容量与故障恢复

所有受管 run 共享 provider 总容量和有界、跨 run 轮转的队列。等待容量时调度器仍能
处理完成事件、取消和 deadline。队列满会明确停止准入，不建立无限后台任务。
同一容量域只能有一个活 dispatcher 进程，使用非过期 POSIX 锁；不同 event loop、
线程或 fork 子进程不能复用该对象。

宿主必须给同一 provider 配置同一私有目录和 provider_id。使用不同目录或身份、
其他进程直接调用 provider 的流量不在本 limiter 的保证中。多机协调仍未实现。
容量目录包含持久 claims 和管理事件，应与 run 状态一起备份，不随服务重启清空。

发送中断后，域内存在未决 claims 就暂停新发送。可信 `reconcile_node` 查询原回执并
返回完整结果、实际用量和 evidence_refs；节点及容量都完成结算后继续依赖图。
恢复遇到已结算节点残留的容量 claim，可以用持久 checkpoint 证明它已结束并释放。

若 run 的发送标记尚未写入，而容量账本已经标记 sent，可能只剩保守容量记录。
宿主管理员确认对应请求结束或后端已清空后，可对 `dispatcher.unresolved` 中的 ID
调用 `confirm_ended(invocation_id, evidence_refs=("receipt:...",))`。这是需要当前管理授权
与真实证据的宿主操作，不是 agent 工具，也不提供公共未认证 HTTP 路由。
确认物理容量不自动清除未知预算；无法取得用量回执的 run 仍需保持对账状态。
不能用 TTL、无回执或本地断连推断远端从未执行。删除 run 也不清除未决容量记录。

模型 invocation ID 绑定规范状态库路径、完整运行身份和节点 ID，并在恢复时复用。
模型 `reconcile_node` 的第三个参数是该持久 invocation ID，回执应按这个参数查找；
工具节点仍使用原 journal idempotency key。同一公开任务 ID 在不同私有状态目录中
不会共享模型票据。0.9.0 候选存在票据跨目录混同问题，已由 PE-012 回归和 0.9.1 修复。

## 验收与部署推荐

无模型屏障、跨 run 限流、预算、工具屏障、权限撤销、进程死亡、对账与 trace 测试
接入候选门禁；示例接入 sdist 与最小 wheel 安装检查。

```bash
uv run --no-sync python -m benchmarks.parallel_execution --mode contract --output reports/parallel-contract-new
uv run --no-sync python -m benchmarks.parallel_execution --mode local --output reports/parallel-dev-new
uv run --no-sync python -m benchmarks.parallel_execution --seal --dev-report reports/parallel-dev-new --protocol reports/parallel-protocol-new.json
uv run --no-sync python -m benchmarks.parallel_execution --mode local --split evaluation --samples 3 --protocol reports/parallel-protocol-new.json --output reports/parallel-evaluation-new
```

真实评测使用同一有限图、资料、模型配置和预算，仅改变并发 1/2。12 开发题、30 评价题，
正式每题每配置 3 次，共 180 根运行；两个校准/预热 run 单独记录。系统内存/swap
每 0.5 秒采样，属于系统观察，不声称能归因出该模型的 GPU 峰值。HTTP 请求重叠也
不证明后端同时进行 GPU 推理。失败和未决样本保留，资源准入失败立即停止该批。

性能、质量非劣性和每成功任务 tokens 阈值在终评前封存；不通过的模型/机器组合继续
推荐并发 1。工程门禁不认证真实模型提速、专业正确性或真实业务上线资格。
模型只在该评测阶段启动，完成或失败后释放本次确认归属的进程和端口。

## 兼容边界

0.9.1 使用 orchestration schema 3、checkpoint envelope 2、recovery 5。0.8.x/0.9.0 的 R2
状态需保留原 wheel 收尾；没有隐式迁移、跨版本接管或原地降级。新 run 可选择并发 1，
活动 run 的执行策略与容量配置已经绑定身份，不能直接改配置恢复。
ReAct、任意 one-shot RunKernel、只读工具并行、自主子运行和工作区并行写入各有独立
边界。完整 SessionService/HTTP/UI 接线和真实业务验收仍是后续产品批次。
