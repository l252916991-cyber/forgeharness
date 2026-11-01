# 应用身份与授权层（W1）

日期：2026-10-28。状态：已实现并接入[独立工作台](COMMERCE_WORKBENCH.md)；本批修复身份边界并增加 HTTP/撤权回归。旧演示入口继续使用公开演示身份。
代码入口：`src/forgeharness/applications/commerce/identity.py`（存储与权限）、
`identity_http.py`（认证路由与依赖注入）。权限矩阵的机器可读副本见
[permission-matrix.json](../reports/application-20261028/permission-matrix.json)。

## 1. 解决什么问题

框架信任宿主传入的 `Actor`。在应用侧，这意味着“谁在调用”必须由应用自己证明。
本层把演示用的角色字符串替换为邀请制账户，并保证：

| 属性 | 实现方式 |
| --- | --- |
| 身份来自认证结果 | Cookie 令牌 → `web_sessions` 行 → 用户与成员关系行 |
| `actor.id` 稳定且不是角色名 | `user:<随机用户 id>`，两名同角色用户永不合流 |
| 作用域由服务端计算 | `context_for()` 只用成员关系与宿主提供的资源解析器 |
| 每次访问重新核验 | 认证重读账户与成员关系；禁用与撤权下一次请求即生效 |
| 审批人与 owner 分离 | 独立令牌通道 `X-Approval-Token`，不得复用浏览器 Cookie |
| 对象 ID 不是凭证 | 会话绑定表 + `require_ticket_read()` 对象级检查 |
| 写入需防伪 | 非安全方法必须带 `X-CSRF-Token`，与服务端会话行比对 |

## 2. 数据模型

| 表 | 主键 | 说明 |
| --- | --- | --- |
| `users` | `id` | 规范化邮箱、显示名、`active/disabled`、scrypt 盐与摘要 |
| `workspaces` | `id` | 名称、`active/suspended` |
| `memberships` | `(workspace_id, user_id, role)` | `active` 标记，撤销后该角色不再计入 |
| `invitations` | `token_hash` | 邮箱、角色、邀请人、过期、接受、撤销 |
| `web_sessions` | `token_hash` | 用户、工作空间、CSRF、签发/过期/撤销 |
| `approval_sessions` | `token_hash` | 审批人、工作空间、10 分钟 TTL |
| `consultation_sessions` | `(workspace_id, session_id)` | owner、创建、关闭 |
| `login_failures` | 行 | 邮箱与时间，用于节流 |
| `audit` | 自增 `seq` | 操作者、动作、对象、结果、时间，不含任何密钥 |

令牌只存 SHA-256 摘要；密码用 `hashlib.scrypt`（n=2¹⁴, r=8, p=1）加盐派生，
比较使用 `hmac.compare_digest`。未知邮箱也执行一次等量派生，避免以耗时区分账户是否存在。

## 3. 流程

### 3.1 登录与请求

1. `POST /identity/login {email, password, workspace_id}` → 200 并 `Set-Cookie: fh_identity=…`
   （`HttpOnly; SameSite=Lax; Path=/; Secure`（HTTPS 部署）; 8 小时绝对过期）。
   响应体含 `csrf`、角色与权限，**不含密码**。
2. 业务请求带 Cookie；非安全方法同时带 `X-CSRF-Token`。
3. 依赖注入按序执行：认证 → CSRF → 会话标识校验/绑定 → 构造 `RunContext`。
4. `GET /identity/me` 返回当前身份与实时权限；`POST /identity/logout` 撤销服务端记录。

### 3.2 审批

1. 审批人用**自己的凭据**调用 `POST /identity/approval`，换取短时审批令牌。
2. 审批/续跑请求同时携带 owner 的 Cookie（选择业务对象）与 `X-Approval-Token`（证明审批人）。
3. 服务端重新读取审批人状态与角色，并要求：持有 `approval:decide`、`actor.id` 与 owner 不同、
   `scope` 与 owner 一致、被审批调用仍在待审批状态。
4. 审批上下文的绑定是确定性的（`run_id=approver-<user>`），因此“审批时的审批人上下文”
   与“续跑时重新认证的审批人上下文”可逐字段比对；这正是 SDK 的 grant 校验依据。

### 3.3 邀请

`POST /identity/invitations`（需 `member:manage` + CSRF）返回一次性邀请令牌；
`POST /identity/invitations/{token}/accept` 用该令牌设置密码并直接登录。
接受时角色**只来自邀请记录**，请求体不能声明角色。

## 4. 角色与权限

| 角色 | 权限 |
| --- | --- |
| `customer` | `consult`、`ticket:read_own` |
| `support` | `ticket:read_scope`、`ticket:claim`、`ticket:resolve` |
| `supervisor` | `ticket:read_scope`、`approval:decide` |
| `editor` | `knowledge:read`、`knowledge:draft` |
| `publisher` | `knowledge:read`、`knowledge:review`、`knowledge:publish` |
| `admin` | `member:manage`、`quota:manage`、`knowledge:read` |

`admin` 故意不含 `approval:decide` 与 `consult`：管理不是业务审批，也不是客户身份。
一人可被授予多个角色，但审批仍要求 `actor.id` 与 owner 不同，兼任不能自批。

## 5. 对象级检查

- 会话：`bind_session()` 在 `BEGIN IMMEDIATE` 内绑定，同一工作空间内一个标识只能属于一人；
  他人使用同一标识返回与“不存在”相同的 403。SDK 另外校验 `session.actor_id`。
- 工单：`require_ticket_read()` 先比对 `tenant`，再判断 `requester_id` 或 `ticket:read_scope`。
  （`requester_id` 列在 W5 加入；在此之前工单读取只对 `ticket:read_scope` 角色开放。）
- 知识：仅 `scope.resources` 中的 `faq:`/`product:` 且角色具备读权限时可见。
- 审批：见 3.2 四条件。

## 6. 部署要求与限制

要求：HTTPS 下 `secure_cookies=True`；身份库与业务库同属受控状态目录（`0700`）；
备份清单必须包含身份库，否则恢复后账户与审计不一致。

当前限制（明确记录，不作为“已具备”宣传）：

- 无多因素认证、无自助找回密码、无开放注册（仅邀请）。
- 一次登录绑定一个工作空间；多工作空间需重新登录或后续增加工作空间选择器。
- SQLite 单 writer；并发写入按队列处理，未做分布式会话存储。
- 管理员不能禁用或撤销自己的账户（防自锁），需要第二名管理员执行。
- `disable_user()` 兼容名称保留，语义为停用当前工作空间的全部成员角色并撤销本空间凭据；不会禁用共享用户在其他工作空间的账户。全局账户封禁未开放给工作空间管理员。
- 邀请/授予/撤销/停用在写事务内再次读取管理员当前权限，不信任旧 Principal 快照。
- 登录节流按邮箱计数，未按来源 IP 或设备指纹统计。

## 7. 验证证据

`tests/unit/test_commerce_identity.py`，本批 40 项，覆盖：

| 类别 | 用例 |
| --- | --- |
| 凭据 | 未知邮箱与错误密码不可区分；库中无明文口令与原始令牌；8 小时过期；连续失败节流 |
| 撤权 | 禁用后旧 Cookie 失效；撤销角色后不可认证；管理员不能自我禁用 |
| 身份 | 同角色两名用户 actor 不同；actor 不含角色名；管理员无审批与咨询权限 |
| 邀请 | 只授予被邀请角色；邀请令牌不可重放；无 `member:manage` 不能邀请 |
| 会话 | 绑定唯一 owner；他人读/写被拒；非咨询角色不能绑定 |
| CSRF | 伪造令牌拒绝；缺失令牌拒绝；退出后令牌失效 |
| 审批 | 无审批凭据被拒；客户不能取得审批令牌；审批令牌 10 分钟后失效；兼任角色不能自批 |
| 观察 | 审计不含密钥；支持角色只看到本租户工单；客户与管理员看不到工单列表 |
| 新增安全回归 | 管理权限快照失效、跨空间凭据不被撤销、登录/审批共用原子节流、HTTP/OpenAPI 依赖解析、并发撤权/CSRF、退出清 Cookie |

运行：`uv run --locked pytest tests/unit/test_commerce_identity.py -q`。
