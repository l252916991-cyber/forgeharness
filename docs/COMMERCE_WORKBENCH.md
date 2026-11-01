# 邀请制本地工作台

适用：0.10.0 工程候选，`commerce-workbench` application/policy v1。实际行为仍是确定性模型与显式配置目录，当前不提供 PG 集群、OIDC、真实商家渠道或订阅收费。

## 安装与初始管理员

```bash
uv sync --python 3.12 --extra dev --extra cli --frozen
uv run --no-sync python examples/commerce_workbench.py --state /ABSOLUTE/PRIVATE/WORKBENCH bootstrap --workspace shop-a --email admin@example.test
```

命令交互输入密码两次，密码不进入命令行参数或输出。状态目录必须为 owner-private 0700。只允许在身份库不存在时初始化；后续成员从管理员邀请加入，不内置公共账户。

为避免被新身份接管，使用新的空状态路径，不将旧 commerce-consultation 会话复制到该目录。一次登录绑定一个工作空间。

## 本地体验

```bash
uv run --no-sync python examples/commerce_workbench.py --state /ABSOLUTE/PRIVATE/WORKBENCH serve --loopback-http --catalog data/synthetic/commerce/catalog.json
```

浏览器打开 `http://127.0.0.1:8766/`。CLI 固定回环监听。`--loopback-http` 显式允许本地 HTTP Cookie；默认使用 Secure Cookie，需要相应 HTTPS 入口才能使用。合成 catalog 的 shop-a/shop-b 不是已接入商家。

1. 管理员登录，分别邀请咨询用户 customer、独立审批人 supervisor、客服 support。
2. 在受信任渠道转交各自邀请凭证；收件人使用页面的接受邀请表单设定密码。
3. 咨询用户提问，查看当前状态、历史和来源；请求人工转接时停在待审批。
4. 当前申请人会话下，由独立审批人使用自己的凭据批准；客户本人或管理员身份不能替代审批人。
5. 客服登录查看授权工单；会话历史、inspect 和事件仍受 owner 约束。

邀请凭证属于敏感认证材料，只在受信任渠道分发，不放入运行记录或公开报告。浏览器不保存审批令牌到长期存储。此首切片仅提供工单创建/查询，领取/处理/解决和用户自己的工单列表仍待后续业务工作包。

## 检查与备份

```bash
uv run --no-sync python examples/workbench_smoke.py
```

smoke 在临时目录创建合成账户，通过 HTTP 验证登录、审批与单次工单副作用，不打印密码或凭据。它不是实际业务质量或独立浏览器可用性验收。

发布验证另需 Node.js 24（运行服务本身不需要 Node）：`node tests/browser/workbench_races.cjs` 执行页面实际脚本的 14 个受控异步竞态与幂等场景。切换账户、退出、换会话时取消在途请求并拒绝旧响应写页面，错误信息与迟到审批令牌也受同一检查。此门禁使用模拟 DOM/fetch，不冒充真实浏览器独立试用。

`/health/live` 检查进程应答；`/health/ready` 检查身份/商品/资料/SDK 会话关键 schema。敏感业务端点每次认证，角色字符串 Bearer 不被接受。

状态整体包括 identity.sqlite、commerce.sqlite、records.sqlite、sessions 和证据目录。沿用[本地运维手册](COMMERCE_OPERATIONS.md)的停机、全目录 SQLite backup API、摘要验证及同机原绝对路径恢复操作；备份包含账户密码摘要与会话，必须同等保护。不要仅备份业务库遗漏身份库。

工作台在首次初始化时写入 application-binding.json，备份清单自动绑定 commerce-workbench/1 并校验标签与内容一致。恢复仍只支持原机器原绝对路径；带身份库和待审批任务的恢复需通过本批回归。未标记的历史 commerce-consultation 状态不能被新工作台自动接管。

## 当前边界

真实账户接线不等于整套 SaaS 身份服务。当前仍没有 MFA/找回/OIDC、开放注册、多节点状态、真实模型业务评估、账单或值班 SLO。总体目标与后续阶段见[SaaS v3](SAAS_PRODUCTION_MASTER_PLAN_V3_2026-10-28.md)，本批资格见[执行报告](../reports/saas/20261028-batch1/REPORT.md)。
