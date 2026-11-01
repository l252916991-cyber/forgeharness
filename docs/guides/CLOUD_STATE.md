# 通用平台 PostgreSQL 状态层

日期：2026-10-29，本文记录0.11.0已验收冻结范围。当前工作区正在实施
[schema2 与托管串行执行](CLOUD_HOSTED_EXECUTION.md)，已接入现有 AgentRuntime、完整模型/工具结果和证据，
新增执行代码的资格以[本次报告](../../reports/saas/20261030-hosted/REPORT.md)为准。
本文其余“尚未实现”列表仅描述下述历史 0.11.0 冻结范围，
当前工程进展以托管执行说明为准。
0.11.0完整验收结果读取
[本批报告](../../reports/saas/20261029-state/REPORT.md)，不能用旧候选通过数认证新增代码。

本轮实现与业务无关的组织/成员授权、任务受理、幂等请求、任务租约与 epoch、精确审批、
调用预算预留、不可变回执、实际用量结算、取消和同事务 outbox。单位是绑定配置解释的整数，
不等于人民币账单；首批 1000 元月预算仍需供应商定价和组织额度层才能强制执行。

## 本地验证

```sh
uv sync --extra dev --extra cli --extra platform --extra benchmark --extra frameworks --extra postgres-test --frozen
uv run --no-sync python -m benchmarks.saas_postgres
uv run --no-sync python examples/cloud_state_demo.py
```

测试使用锁定的 PostgreSQL 18.6 二进制、私有 Unix socket、随机独立数据库和低权限应用角色。
它拒绝缺失依赖，不跳过数据库测试。不会连接用户服务器、现有业务库或调用模型。
测试完停止服务并移除合成数据；产物记录代码/SQL 摘要、测试数量、退出状态。

`forgeharness[cloud]` 仅增加 asyncpg。安装包演示另外需要 `[postgres-test]`；
内置测试数据库不用于生产。生产的数据库连接和身份由可信宿主配置。

## 调用边界

`PostgresRunStore.submit/claim/renew/cancel/finish/events` 管理任务；
`PostgresCallStore.grant/admit/mark_sent/mark_unknown/record_receipt/settle`
管理审批、调用与结算。`TenantContext` 必须由认证后的宿主生成，不能从客户请求体直接当作身份。
当前 member 的 `can_execute` 是受信执行服务权限，不能分配给普通终端用户。

部署管理员单独运行 `cloud.provision.migrate` 和 `grant_runtime`；应用启动仅验证 schema。
运行角色不能拥有数据库对象或具有可提权成员关系。RLS 和事务内租户上下文防止误查串租户；
持有运行角色并能执行任意 SQL 的不可信客户端不属于该隔离模型。

发送前重新检查当前组织、owner、审批人、审批有效期、预算、取消和任务租约。
send intent 的数据库提交与外部请求之间仍有不可消除的边界；接入 provider 时须使用原调用键、
确认回执和执行前授权协议。未知调用保留预算，不因租约到期或取消被自动重发/记零。
确认未执行的回执必须来自可信核对，不能由用户或超时推断。

## 当前尚未实现

- 该层尚未接入 SDK/Harness、SessionService、真实模型/工具调用。
- 组织开通、通用登录/IdP、工作空间与应用版本管理的完整控制面/API/UI。
- 多节点调度、队列事件投递与去重、provider 共享容量、checkpoint/attempt 恢复。
- 完整输入输出与 trace 的证据存储、资料删除墓碑、保留策略、云备份恢复。
- 组织月额度、价格版本、模型账单核对、订阅支付和运营界面。
- 用户服务器部署、TLS/域名、云故障演练、持续运行 SLO、商业/业务验收。

因此，本轮通过仅能得出“PG 状态首切片具备本地工程证据”，不能得出“生产级 SaaS 已完成”。
完整事务目标与已实现子集见 [状态契约](SAAS_STATE_CONTRACT.md) 和 [ADR 040](../adr/040-postgres-execution-state.md)。
