# ADR 038：应用身份边界与审批独立通道

- 状态：已采纳（2026-10-28）
- 关联：[应用身份与授权层](../APPLICATION_IDENTITY.md)、
  [应用产品化计划](../APPLICATION_PRODUCT_PLAN_2026-10-28.md)、
  [ADR 036](036-scoped-model-invocation-identity.md)、[ADR 037](037-general-commerce-handoff.md)

## 背景

SDK 信任宿主传入的 `Actor`：`RunContext` 里写什么，运行时就用什么。参考应用此前的
“身份”是请求头里的角色字符串（`Authorization: Bearer customer`），同一角色的两个人在
运行时看来是同一个 actor。演示够用，多人上线即失效：无法区分“谁批准的”“谁能读这张工单”，
也无法做到撤权即时生效。若把身份判断留给模型、请求体或前端开关，同一个问题会在更隐蔽的地方复现。

同时，审批与业务会话共用一条身份通道会产生自批：同一个 Cookie 既能发起也能批准。
SDK 的 `can_approve` 有 `requester.actor.id != owner.actor.id` 约束，但前提是宿主能提供
两个真正不同的身份。

## 决策

1. **应用自持身份库**：`users`、`workspaces`、`memberships`、`invitations`、
   `web_sessions`、`approval_sessions`、`consultation_sessions`、`audit` 落在应用自己的
   SQLite 中，不改动 SDK 的信任模型。
2. **`Actor.id` 使用稳定用户标识**（`user:<uuid>`），角色只表达授权，不再冒充身份。
   角色到权限的映射是代码常量 `PERMISSIONS`，文档与机器可读矩阵都必须与它一致。
3. **每次请求重读状态**：认证路径同时读取账户状态、工作空间状态与启用中的成员关系。
   禁用账户与撤销角色在下一次请求即生效，不依赖令牌过期或缓存失效。
4. **浏览器凭证与会话标识分离**：Cookie 只证明“你是谁”；`X-Session-ID` / 路径参数只选择
   “访问哪段会话”，且由服务端绑定表校验归属。对象 ID 不是授权凭证。
5. **审批走独立通道**：审批人用自己的密码换取 10 分钟审批令牌（`X-Approval-Token`），
   不与 owner Cookie 复用；审批上下文使用确定性绑定（`run_id=approver-<user>`），
   使“审批时的审批人上下文”与“续跑时重新认证的上下文”可逐字段相等——这是 SDK
   校验 approval grant 的前提。
6. **CSRF 默认拒绝**：非安全方法必须带与会话行匹配的 `X-CSRF-Token`；没有宽容模式。
7. **管理员不是审批人**：`admin` 角色不含 `approval:decide` 与 `consult`，
   由权限表而不是流程约定保证。

## 备选方案与否决理由

| 方案 | 否决理由 |
| --- | --- |
| 继续用角色字符串 | 无法区分同角色多人、无法即时撤权、审计无主体 |
| 把角色放进 JWT 由前端携带 | 撤权需等令牌过期；角色断言来自客户端 |
| 直接引入第三方 IAM | 首版为单机邀请制，接入与运维成本高于收益；接口位置已预留（`authenticate` 回调） |
| 审批复用 owner Cookie | 无法证明“另一个人批准”，与 `can_approve` 的意图冲突 |

## 后果

- 应用必须自行承担密码策略、令牌过期、登录节流与审计；已在
  `docs/APPLICATION_IDENTITY.md` 记录限制（无 MFA、无找回、单工作空间绑定、SQLite 单 writer）。
- 备份清单必须包含身份库，否则恢复后账户与审计不一致。
- 引用应用只能用 `forgeharness.sdk` 公共契约；身份层因此不依赖 `forgeharness.domain` 或
  `forgeharness.state` 内部模块（由 `test_sdk_architecture.py` 强制）。
- 后续接入真实宿主时，替换点是登录端点与 `authenticate` 回调，而不是散落在路由里的角色判断。
