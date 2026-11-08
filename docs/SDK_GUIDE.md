# ForgeHarness SDK v1 使用说明

适用版本：0.13.0，SDK API v1；Python 3.12。SDK 的本地持久状态是单机 POSIX + SQLite；
PostgreSQL 托管执行见 [托管串行执行](guides/CLOUD_HOSTED_EXECUTION.md) 和 [控制面](guides/CLOUD_CONTROL_PLANE.md)。

相关专题：工作流、资料版本、人工业务审核和引用模块见 [BUSINESS_MODULES](guides/BUSINESS_MODULES.md)；
固定专家协作见 [COLLABORATION](guides/COLLABORATION.md)；有界模型并行见 [PARALLEL_EXECUTION](guides/PARALLEL_EXECUTION.md)；
持久会话见 [SESSIONS](guides/SESSIONS.md)。各能力的验证证据见 [当前能力矩阵](CURRENT_CAPABILITIES.md)。

## 安装与运行

```bash
uv sync --extra dev --extra cli --extra platform --extra benchmark --extra frameworks --frozen
uv run --no-sync python examples/sdk_demo.py
make gate
```

最小消费者只需安装 wheel 本身；基础依赖是 Pydantic 与 JSON Schema。
`models` extra 提供 HTTP 模型依赖，`web` 提供可选控制接口，`knowledge`
提供文档解析依赖，`skills` 提供 YAML Skills 解析，`cli` 保留原来的完整命令，`platform` 与 `frameworks`
分别提供外部存储和框架对照。基础安装的 `forge --help` 提示完整 CLI 的安装方式。

```bash
uv build
uv venv /tmp/forge-consumer
# 用候选版验证报告中记录的 wheel 路径替换 $WHEEL
uv pip install --python /tmp/forge-consumer/bin/python "$WHEEL"
/tmp/forge-consumer/bin/python examples/sdk_demo.py
/tmp/forge-consumer/bin/python examples/business_modules_demo.py
```

`$WHEEL` 指 `uv build` 生成的 `dist/forgeharness-<版本>-py3-none-any.whl`。正式发行时，wheel 必须取自
通过完整门禁的同一源码版本，`release.yml` 会冻结源码、在干净副本上复验后再发布同一份构建产物。

示例在临时目录演示两个应用，使用确定性模型，无需模型密钥。Coding 示例的
固定 `true` 命令仅验证执行链路；真实项目应配置自己的测试命令。文件工具约束路径，但固定测试命令不是 OS 沙箱；
运行不可信仓库或插件代码前，宿主必须提供系统级沙箱。当前 Coding 参考应用只覆盖
操作者信任的本地仓库/测试代码，不声称对恶意本地进程或并发文件替换提供隔离。完整电商业务
平台计划见 [ECOMMERCE_AGENT_PLATFORM_PLAN](archive/ECOMMERCE_AGENT_PLATFORM_PLAN.md)，目前未开发。

## 接入步骤

1. 实现 `Tool`：严格 Pydantic 输入、`ToolSpec`、`execute`；副作用默认不可重放。
2. 实现 `ContextualPolicy.evaluate` 与 `can_approve`，将资源归属和实时版本检查
   放在策略与业务存储两处。策略获得规范化后的参数以及可信 `RunContext`。
3. 实现 `Verifier`，检查完整的有序 `tool_results` 和 `BusinessReceipt`；
   `accepted`/`queued` 回执不能当作已完成。
4. 用注册的 Python 对象构造 `ApplicationSpec`，声明 ID、版本、策略版本、
   模型工厂、工具、预算、说明、可选上下文提供者和证据变换；推荐用
   `Capability` + `compose_application` 组合（见下节）。
5. 宿主完成认证后构造 `Actor`、`ResourceScope` 与 `RunContext`。用户任务文本
   不能提供这些值。`workspace` 可选；Coding 必须使用规范化的绝对工作区。
6. `Harness(spec, state_dir=...)` 的 `run(context=..., task=...)` 返回持久结果。
   继续使用原始公共 context 调用 `inspect`、`approve`、`resume`，不要用返回结果
   的内部 `task_id` 重构公共 run ID。

`examples/sdk_demo.py`、`coding/application.py` 和
`applications/commerce/application.py` 是完整可运行接入示例。业务应用的核心
契约通过 `forgeharness.sdk` 导入；Coding 的 repository/skills 为声明的应用扩展。

## 能力组合

应用可以把工具、指令、上下文拆成 `Capability`，再用 `compose_application` 组合：

```python
from forgeharness.sdk import Capability, compose_application

spec = compose_application(
    id="legal-case-qa",
    version="1",
    policy_version="1",
    model_factory=model_factory,
    capabilities=(
        Capability("case-pages", tools=(CaseSearchTool(adapter), CasePageTool(adapter))),
        Capability("legal-instructions", instructions=("引用必须落到页级原文。",)),
    ),
    policy=ReadOnlyCasePolicy(),
    verifier=LegalAnswerVerifier(store),
)
```

`policy`、`verifier`、`budget` 仍由应用显式提供；capability 只贡献
tools/instructions/context providers。应用级 instructions 在前，capability
按声明顺序在后；重复工具名或能力名会直接失败。`ApplicationSpec` 的原有构造方式
仍然可用，现有应用不需要迁移。

边界：capability 是宿主注册的可信 Python 对象，不是动态插件；它不能关闭
scope、审批、预算、trace 或 verification。policy/verifier 的组合语义尚未定义，
因此暂不作为 capability 贡献。

## 自定义编排（只读）

如果应用有自己的控制流（例如确定性并行 DAG），实现 `Orchestrator` 并通过
`Harness(..., orchestrator=...)` 注入：

```python
import asyncio
from forgeharness.sdk import FinalAction, Harness, Message, MessageRole, RunKernel, RunResult


class TwoNodeDag:
    async def run(self, *, kernel: RunKernel, task: str) -> RunResult:
        left, right = await asyncio.gather(
            kernel.call_model(
                messages=(Message(role=MessageRole.USER, content="left"),),
                phase="left",
            ),
            kernel.call_model(
                messages=(Message(role=MessageRole.USER, content="right"),),
                phase="right",
            ),
        )
        return await kernel.finish(
            final=FinalAction(content=f"{left.action.content}|{right.action.content}")
        )


harness = Harness(spec, state_dir=state_dir, orchestrator=TwoNodeDag())
result = await harness.run(context=context, task="root")
```

`call_model(include_tools=False)` 可为只输出文本的节点省略本次请求的工具目录，
默认仍暴露已注册工具。此选项不修改 registry、权限或预算；确定性 `call_tool` 仍经过
相同边界。实际模型可见目录保存在规范请求与 trace 中，见 [ADR 033](adr/033-model-tool-catalog-visibility.md)。

`RunKernel` 提供 `call_model`、`call_tool`（仅 `RiskLevel.READ`）、`verify`、
`checkpoint`、`emit` 和 `finish`。model 调用走同一套上下文预算/证据变换/trace；
只读工具走同一套 `BoundPolicy` + `ToolDispatcher`；成功必须过 `Verifier`。

限制：自定义编排是 one-shot，`resume`/`approve` 会明确拒绝；写工具、审批和
调用日志/恢复仍走 ReAct。不要用自定义编排绕过 kernel 直接调宿主数据库或自己写
trace/checkpoint。

### 大量独立材料：拆分 + 合并

材料互相独立、答案是各部分结果的并集时（例如逐份审计、逐条分类），推荐用程序拆分，
而不是一次调用读完全部，也不是固定专家协作：

- 每组是一次普通的受治理运行：自己的状态目录、预算和哈希链 trace，通过只读工具读材料，然后调用一次模型；
- 用信号量限制同时进行的组数；
- 由程序合并答案；失败的组要连同它没有检查的材料一起报告，不能静默丢弃。

完整可运行的例子见 `examples/fanout_demo.py`（不需要模型 key）。依据是三轮真实模型实验：
材料约 21 万 token 以上时，一次调用的错误率升到 3.4%–4.9%，按每组约 1.3 万 token 拆分后降到 0.2%–0.3%；
程序拆分和固定专家协作准确率相当，但协作的成本是 3.1 倍、延迟是 28 倍
（[长上下文实验](../reports/long-context-scaling/REPORT.md)）。
固定专家协作留给需要模型做跨材料综合的任务，这一点尚未经过实验验证。

服务商明确拒绝的请求（例如余额不足的 HTTP 402）会抛出 `ModelRequestRejected`，
按失败结算、用量记 0；超时、限流和服务端错误仍按结果未知处理（[ADR 050](adr/050-definitive-provider-rejections.md)）。
自定义模型适配器可以对确知未处理的状态码抛出它。

## 审批与重启

```python
suspended = await harness.run(context=owner_context, task=task)
grant = harness.approve(context=owner_context, approver=authenticated_approver)
rebuilt = Harness(spec, state_dir=state_dir)
result = await rebuilt.resume(
    context=owner_context,
    grant=grant,
    approver=freshly_authenticated_approver,
)
```

owner context 必须与持久记录完全一致：应用、租户、session、actor、scope、
版本和关联 ID 都不能改变。审批者由宿主重新认证，批准身份和角色不能来自
`granted_by` 字符串。`can_approve` 应查询宿主的当前授权状态；框架不实现身份目录。
权限或资源版本改变会拒绝旧审批。改变应用实现、工具 Schema、预算或策略时，
必须升级应用/策略版本，不能继续冒用原版本。

SQLite 审批与 checkpoint 在同一数据库中原子提交。审批只授权一个 call ID、
工具名与参数组合。授权后尚无 journal 的故障窗口由已持久化的 execution
admission 恢复，既不会丢掉批准记录，也不能将批准替换成其他操作。

## 恢复矩阵

| 状态 | SDK 行为 |
| --- | --- |
| completed + 正确摘要/调用身份 | 复用完整结果，不再执行工具 |
| claimed | 当前策略允许后继续 |
| started + 只读/幂等 | 当前策略与参数匹配后用同一键重试，保留物理尝试预算 |
| started + 非幂等 | 记录不确定副作用，停止自动重放 |
| 无 journal + 第 3 代已授权 checkpoint | 可开始已批准的具体调用 |
| 无 journal + 无已授权记录，或旧未解决 checkpoint | 明确失败 |
| waiting approval | 使用持久动作重新认证和审批 |

SDK 对每个 run 持有单机非阻塞文件锁，拒绝活动 worker 的重复运行、恢复和删除；
数据库 CAS 继续保护版本竞争。底层 legacy runtime API 的恢复要求旧 worker 已停止。
不支持分布式租约、共享网络文件系统、通用 exactly-once、任意存储间的事务继承。

同一用户业务请求可产生多个 run，因此幂等工具还需要业务 request_id 和唯一约束。
模拟工单同时消费框架 key 与业务 request_id，跨 run 不会创建第二张工单。

## 上下文、隐私与运行证据

`ContextProvider.provide` 负责在读取/检索之前过滤权限。`ContextMaterial` 声明
application、tenant、resource、source、version、有效期和内容类型；SDK 再验证范围，
编译预算并记录来源。资料文本属于数据，不能更改身份或工具权限。已有知识/记忆
模块保持为可选扩展；新应用通过 provider 组合，不会自动获得旧知识应用的访问权。

默认 `CredentialMask` 遮盖常见凭据形式。宿主可注册确定性的 `EvidenceTransform`
执行领域 PII 处理；同一变换覆盖模型输入、模型动作、工具结果、checkpoint、journal
及 Trace。凭据只能保存在宿主受控适配器中；启发式遮盖无法识别所有秘密，不能替代
秘密管理。运行失败的异常信息不包含工具/模型异常的原始正文。

`inspect` 与 `trace_path` 按完整 owner binding 授权。Trace 的 `model.request` 保留
实际发送的经过处理的输入，来源材料与业务回执可查询；不保存隐含思考。每个事件有
schema v1；v1 默认版本字段不参与历史哈希的规范化，保留旧 Trace 校验兼容性。
链内哈希只能检测内部不一致，不能独立证明没有截尾或整体替换。

`delete(context)` 清理 checkpoint、journal、物理尝试、批准与本地 Trace，并写入
tombstone，禁止同一 run 恢复或重建。删除中途失败可重复调用。它在单机锁内运行，
可清理已停止但状态仍为 RUNNING 的任务。SQLite secure_delete、WAL 清理与 VACUUM
用于减少本地残留；不承诺 SSD、外部备份、日志镜像或宿主附件的物理安全擦除。
宿主需对附件、业务账本、备份和已导出报告执行自己的保留/删除规则；删减报告必须
标记“无法完整回放”。本 SDK 不生成跨存储的隐式附件副本。

## 可选 HTTP 控制

安装 `web` extra 后使用 `forgeharness.sdk.control.control_router`。宿主必须提供
`authenticate(Request) -> RunContext`，每次验证身份、run 归属和 scope；请求 JSON
只含任务，额外身份字段会被拒绝。提供 run、inspect、approve、resume 入口。
最小 HTTP approve 是自审批；独立审批者使用嵌入式 SDK。生产宿主自行实现身份提供者、
CSRF/Origin 策略、限流、审计出口与管理界面。旧 `serve`/Coding API 是本地兼容入口，
不继承新 SDK 的身份系统或持久审批保证。

### 可选会话路由

0.4.0 候选版新增可选的会话 HTTP 适配器，位于 `forgeharness.sdk.session_control`，
由 `session_router(service, *, authenticate, authenticate_approver=None)` 构造，
提供 `/harness/sessions...` 路由。与 `control_router` 相同，宿主必须提供
`authenticate(Request) -> RunContext`，每次请求重新验证身份、会话归属与 scope；请求
JSON 不得用来构造可信 `RunContext`。提供独立审批者时传入 `authenticate_approver`，
否则 HTTP 审批为自审批。该路由依赖 `web`/`cli` extra 提供的 FastAPI，不属于基础
wheel 的依赖集。会话层的完整说明见 [SESSIONS](guides/SESSIONS.md)。

## 版本与兼容

- SDK API 1；事件 1；checkpoint 1；journal 1；业务回执 1；恢复语义 3。
- 会话存储使用独立的 `session_schema` 1，保存在单独的 SQLite 文件；不迁移旧 knowledge 表。
- generation 2 的历史评测只证明旧重试/时间控制，不证明新恢复路径。
- `runtime.verification.CodingVerifier`、`runtime.CodingVerifier`、
  `context.repository.PythonRepositoryMap` 仍提供懒加载兼容，API v2 才考虑移除。
- 原 CLI Python 导入与命令在 `cli` extra 下保留。
- 通用控制面通过 SDK 组合，无核心中的电商或编码业务分支。
