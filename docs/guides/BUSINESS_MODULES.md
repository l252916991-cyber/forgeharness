# 法律与电商共用模块（0.3.0）

更新：2026-10-12。公共 SDK API 保持 1；新增 workflow state v1、record schema v1。
本次交付覆盖嵌入式、单机 POSIX/SQLite 模块，最终门禁证据见
`reports/modular-release-audit.json`。旧 0.2.0 审计不替本版本背书。

本文件描述 0.3.0 的模块范围；0.4.0 候选版在此基础上新增共享会话层，说明见
`docs/guides/SESSIONS.md`，资格证据见 `reports/session-release-audit.json`。下文 0.3.0 的结论
仅在会话这一点上被取代，其余内容不变。

## 1. 从业务需求到公共能力

| 需求 | 法律应用 | 电商应用 | 当前落点与状态 |
| --- | --- | --- | --- |
| 授权范围 | 案件/证据归属 | 商家/商品/订单归属 | 已有 Actor/ResourceScope/ContextualPolicy；宿主认证与实时撤权 |
| 多步流程 | 检索、整理、笔记、审校 | 咨询、实时查询、内容草稿 | 新增 WorkflowSpec/Step/Runner；确定性拓扑顺序执行 |
| 全流程资源控制 | 防止多节点放大成本 | 批量业务的调用额度 | 新增 WorkflowBudget；所有节点最大预算之和预留，不在恢复时重置 |
| 中断续跑 | 等待确认后继续整理 | 转接或写草稿后断线 | 复用每节点 Harness/审批/journal；稳定节点身份与父状态衔接 |
| 版本化资料 | 页级证据、审阅笔记 | 商品说明、政策、运营稿、训练材料 | 新增 RevisionStore；版本 CAS、业务请求去重、审核、发布、撤回 |
| 来源位置 | 原文页、段落、条文版本 | 商品字段、图片/视频位置 | 新增 SourceAnchor；定位元数据由解析器/业务适配器提供 |
| 派生资料失效 | 原始证据更新后笔记不能继续引用 | 产品说明撤回后旧素材退出可用范围 | 新增 RecordDependency；读取发布内容及批准/发布前递归检查当前来源 |
| 引用验收 | 引文是否等于指定原文片段 | 卖点摘录是否来自当前说明 | 新增 Citation/CitedAnswer/CitationVerifier；检查版本、digest、精确字符区间 |
| 完成判断 | 节点完成后审校业务结果 | 知识与价格在流程结束时重查 | 新增 WorkflowVerifier、AllOfVerifier；保留已有工具回执验收 |
| 解析与检索 | OCR、页级检索、法条库 | 图文资料、商品知识检索 | 已有可选 knowledge 协议/解析/检索；PublishedContextProvider/Tool 可接入审核资料 |
| 实时事实与业务写入 | 案件业务系统适配 | 价格库存、人工工单 | 已有商品/工单模拟 API、回执与跨 run 去重；真实业务 API 由应用接入 |
| 多轮会话与记忆 | 案件工作会话 | 客服对话与反馈 | 新增公共嵌入式会话服务 `forgeharness.sdk.SessionService` 与可选 HTTP 适配器 `forgeharness.sdk.session_control.session_router`；现有 knowledge 会话/审核记忆保留，且没有自动迁移旧会话 |
| 后台任务 | 大文件解析 | 批量导入、视频处理 | 现有可选 Inline/ARQ；通用持久任务调度与跨服务 outbox 仍是后续工作 |
| 前端与渠道 | 原 LexVault 工作台 | 客服/管理台/发布渠道 | 本次没有迁移 LexVault，也没有交付完整电商 UI 或真实渠道 |

“适用于多数应用”的依据是可组合的常见模式，不是对任意行业、数据规模或生产部署的保证。
业务规则留在 `applications/` 或外部宿主；核心不识别案件、商品、法条或订单字段。
阅读 LexVault 架构帮助确定需求，未复制其代码或卷宗数据。

## 2. 模块与安装

公共类型均从 `forgeharness.sdk` 导入，基础 wheel 仍只直接依赖 Pydantic/JSON Schema。
没有新增运行依赖，也不要求运行模型服务、Web、Redis 或下载模型。

```sh
uv sync --extra dev --extra cli --extra platform --extra benchmark --extra frameworks --frozen
uv run --no-sync python examples/sdk_demo.py
uv run --no-sync python examples/business_modules_demo.py
```

第二个例子使用临时目录和明确的合成资料，演示：法律来源→带引用笔记→执行审批后落草稿；
电商知识→当前价格库存→流程级复核；运营摘录→草稿→独立业务审核→本地发布。
“发布”只改变本地资料可见状态，不会向平台或他人发送内容。
示例模型是确定性驱动器，不代表大模型分析能力、法律正确率或电商收益。

## 3. 工作流契约

`WorkflowStep(id, application, task, depends_on)` 注册一个完整 SDK 应用节点。
同一流程中节点共享应用 ID/版本/策略版本，工具、模型、验收器可以分别装配。
宿主完成认证后提供与 `WorkflowSpec` 匹配的 RunContext。

```python
from forgeharness.sdk import WorkflowRunner, WorkflowSpec, WorkflowStep

spec = WorkflowSpec(
    id=read_app.id,
    version=read_app.version,
    policy_version=read_app.policy_version,
    steps=(
        WorkflowStep("source", read_app, "Read authorized current sources"),
        WorkflowStep("draft", draft_app, "Prepare a draft", ("source",)),
    ),
)
runner = WorkflowRunner(spec, state_dir=private_state_dir)
result = await runner.run(context=authenticated_owner, task=user_request)
# 等待审批时，独立确认具体动作；重新认证身份后恢复。
grant = runner.approve(context=authenticated_owner, approver=authenticated_approver)
result = await WorkflowRunner(spec, state_dir=private_state_dir).resume(
    context=authenticated_owner,
    grant=grant,
    approver=authenticated_approver,
)
```

示意代码中的 app/context/path 来自宿主；可运行装配见例子和 `applications/`。
`approve` 仅在 `awaiting_approval` 状态调用。它允许某次工具执行，不等于确认法律事实、
批准运营稿或同意外部发布；业务审核必须另行调用资料生命周期接口。

* DAG 必须无环、依赖存在、节点名唯一，最多 64 节点。就绪节点按名称稳定排序，顺序执行。
* 节点 `max_steps/tool_calls/input_tokens/output_tokens` 最大值之和必须满足 WorkflowBudget。
  这是保守预留；不会把未用额度再分配，也不新增并发执行或预算外的模型调用。
  节点 wall deadline 沿用已有语义，不是包含人工等待时间的流程全局硬截止。
* 下游输入是有界 JSON：原任务、节点任务、声明依赖的最终输出及工具证据。
  依赖内容是数据；身份、工具、预算不能从上游文字继承。输入超限明确拒绝，不悄悄丢证据。
* 每个节点是稳定 ID 的普通 Harness run；通过 `inspect().nodes` 查询结果和用量。
  `events()` 返回父流程生命周期。模型输入、工具结果及调用副作用证据保留在对应子 run 中。
* 父状态和父事件在同一事务保存。子 run 成功而父状态尚未更新时，恢复会采纳已完成结果；
  停止的 RUNNING 子 run 走既有恢复矩阵，不能把未知写入当作未发生。
* 终态 FAILED/EXHAUSTED 不自动从头重跑。宿主检查原因后创建新的业务请求/流程；
  稳定业务操作键另行防止跨 run 重复写入。控制异常保留可重建的父状态。
* 非阻塞本机锁拒绝活动 worker 的第二次运行、恢复或删除；不提供分布式租约。
* 图、任务、工具声明、预算等定义摘要改变会拒绝恢复。Python 实现、模型/适配器配置、
  角色策略或隐私策略改变，宿主仍必须升级应用/策略版本；摘要不是代码/闭包自动指纹。
* 可提供 WorkflowVerifier，在全部节点成功后复核跨节点条件；其异常或拒绝阻止流程成功。
  未提供时，成功只表示所有节点的各自验收通过，不保证所有历史快照在最终时刻仍有效。
* `delete(context)` 先留身份墓碑，再幂等清理子 run、任务与事件；失败可重试。
  业务资料、上传和备份不属于 run 删除范围，宿主需安排对应保留策略。

本版没有并行调度、动态生成 DAG、自动补偿事务、流程级主动取消 API 或可视化流程编辑器。
无需这些能力的应用已经可以组合 SDK；新增调度复杂性应有具体负载和评测依据。

## 4. 资料与业务审核

`RevisionStore(path, policy=..., transform=...)` 要求 owner-private 目录。
默认 RoleRecordPolicy 是可选样例：reader 读取、editor 写草稿、reviewer 审核、publisher
发布/撤回、admin 管理。真实应用应提供查询宿主当前权限的 RecordPolicy。
所有操作先检查可信 application/tenant/resource 范围，再读取内容。

```python
draft = store.save(
    owner,
    RecordDraft(
        resource_id="case:42:note",
        kind="review_note",
        content="synthetic reviewed excerpt",
        sources=(SourceAnchor(source="synthetic.pdf", version="sha256:...", locator="page:3"),),
    ),
    expected_version=0,
    request_id=host_business_request_id,
)
store.review(reviewer, draft.resource_id, expected_version=draft.version, approve=True)
store.publish(publisher, draft.resource_id, expected_version=draft.version)
current = store.published(reader, draft.resource_id)
```

资料内容版本不可原地改写。`save` 使用 expected_version 乐观并发检查；request_id 必须
由宿主生成/验证，相同身份、内容和前置条件的重试返回原版本，参数冲突明确拒绝。
身份命名空间包含 application 和 tenant；不同应用之间共享数据需要宿主显式适配，
不能通过模型提供一个别的 application_id 来跨应用读取。

默认不能自己审核自己写的版本。编辑创建新草稿并撤回旧发布版本，旧审批不能用于新版本。
不提供把旧版本直接设为当前的捷径；回滚是复制旧内容为新草稿再走审核。
`published` 要求当前版本已发布且未过期；日期必须带时区。

RecordDependency 记录来源 resource/version/digest。保存派生稿、批准、发布及读取发布内容
时，在同一 SQLite 事务内检查依赖来源仍为当前有效发布版本，同时检查来源读取权限。
检查有深度/数量界限并复用同一事务中的已查结果；循环、过多来源和陈旧依赖明确失败。
下游数据库行不必被批量改写，其可用性在读取时立即受到来源状态约束。

外部 SourceAnchor 只负责定位，不会访问 URL，也不自动证明法条现行有效、OCR 正确或
视频素材许可。只有同一 RevisionStore 中的 RecordDependency 享有自动版本依赖检查。
RecordPolicy 的业务权限和记录审核，与 Harness 的执行审批分别存储和验收。

`read(version=...)` 用于授权历史查看；业务检索使用 `published`，不能拿历史查看接口
绕过发布状态。`audit` 记录版本、摘要、操作者和转换；不重复保存内容正文。
`delete` 清理本地内容/请求/审核事件并留资源墓碑，禁止同一身份复建；不声称物理擦除
SSD、SQLite 历史磁盘页、外部附件或备份。CredentialMask 是启发式凭据遮盖，PII 策略由宿主补充。

## 5. 引用与业务完成

Citation 字段为 resource_id/version/digest/start/end/quote；start/end 是 Python Unicode
字符索引，区间 `[start, end)`，不是字节或 PDF 坐标。CitationVerifier 读取当前授权发布
内容，检查 digest、版本、有效期和引文完全一致；它不判断 answer 中的全部主张是否获支持。
法律适用、证据证明力、商品性能承诺应由独立业务验收器和人工复核处理。
AllOfVerifier 可组合多个检查；任一个失败就不能宣布成功。

PublishedContextProvider 负责提供指定审核资料，PublishedRecordTool 允许模型按资源读取。
向量检索、全文检索仍由可选检索模块或 LexVault 适配器提供；该模块没有伪装成新搜索引擎。
价格/库存仍从商品 API 读取，不能把审核说明里的旧价格当当前事实。

## 6. 后续接入顺序与边界

1. LexVault：适配案件可信上下文、现有页级检索与引用结构，先迁移只读问答；保留 OCR、
   法条核验、证据审批和 UI。用相同任务对照原生流程，再逐条迁移写入和多节点阅卷。
2. ForgeCommerce：复用 SDK 组装商品咨询、审核知识、人工转接；在应用层建设导入、
   会话/客服 UI 与独立标注评测。新内容/训练材料可使用 RevisionStore，无需复制生命周期。
3. 多渠道/长任务需要通用任务协议、取消/进度、outbox/结果核对、限流和租户存储配额时，
   先建立跨两业务的契约用例，再提炼模块。真实发布、订单变更、支付不在本次交付中。

本版只证明确定性框架行为与合成参考应用的组合成立。未跑真实法律/电商模型终评，
未替换 LexVault 的生产流程，没有新增法律准确率、多 Agent 优势、业务收益或生产 SLA 声明。
