# 下一工作包：通用组织、身份与托管 API 控制面

日期：2026-11-01。性质：冻结期间的只读设计审查，未实施新的产品功能。

本报告对应正在验证的 candidate-r2 源树
`22bec79d6b7f187bb5798bc59e18690ba9e03add729921163cedad8cb42cac24`。
候选是否通过以独立门禁结果为准；本报告不增加其资格范围。仅新增本报告，未改源码、
文档、数据库、锁文件或配置。

用户条件继续沿用：以中国大陆客户为主，约 10 名使用者，云服务和模型月预算合计
1000 元人民币；已有服务器和模型服务，后续本机配置接入。业务尚未确定，先完成通用平台。
下一切片用本机 HTTP、隔离 PostgreSQL 和确定性模型完成，不需要新资源、外部账户或密钥。

## 1. 下一项应交付什么

交付一个**可登录、可撤权、按组织和工作空间授权的邀请制内部 Alpha 托管入口**：

1. 两个组织各有真实、稳定且不同于角色的账户主体；同一人可有多个组织的成员关系。
2. 成员登录后只看到被授权的工作空间与服务器注册应用，可提交运行并轮询状态。
3. 请求受理调用现有 `HostedRuntime.accept`，提交成功返回 `202 + run_id`；执行服务独立
   调用 `advance`。Web 请求不持有执行权限，也不等待模型或工具完成。
4. 用户可查询及取消自己的运行；独立审批人能看到获授权的待审批提案并批准。服务器从
   PG 读取原 owner、精确调用和部署绑定，不让客户端提交可信 `TenantContext` 或审批身份。
5. 禁用组织、撤销成员或应用授权后，下一次受保护访问和下一次物理发送均拒绝；已经发生
   的副作用继续按已实现的对账规则结算，unknown 保留预留，不能通过撤权清掉成本。

这仍是 M1 内部 Alpha 的一个切片。OIDC/PKCE、MFA、账号恢复、机器 API Key 全生命周期、
组织月额度、计价收费、公开注册、自助部署、持续调度公平性与生产运维仍在后续必需范围内。
邀请制本地登录适配器可用于该切片的离线与内部验证，不能被称为已完成生产 IdP 接入。

## 2. 已有资产及真正可复用的边界

| 已有代码 | 可直接沿用的原则或能力 | 不能直接视作平台能力 |
| --- | --- | --- |
| [commerce/identity.py](/Users/xiaoy/Documents/多agent协作/src/forgeharness/applications/commerce/identity.py) | 稳定 `user:<id>`、令牌只存摘要、邀请一次消费、每次重新读取身份与成员关系、管理操作在事务内重验调用者、登录与审批共用失败计数 | 存储为同步 SQLite；角色是 customer/support 等业务角色；workspace 同时充当 tenant；没有平台组织、服务身份或外部 issuer/subject |
| [commerce/identity_http.py](/Users/xiaoy/Documents/多agent协作/src/forgeharness/applications/commerce/identity_http.py) | 安全 Cookie、写操作 CSRF、严格请求模型、鉴权依赖、独立审批凭据、统一失败响应 | 不能把原路由原样挂到 Cloud；它产出本地 SDK `RunContext`，且依赖 commerce 权限与会话绑定 |
| [commerce/workbench.py](/Users/xiaoy/Documents/多agent协作/src/forgeharness/applications/commerce/workbench.py) | `_CurrentAccess`、`_CurrentPolicy` 和 `authorize_run` 展示了请求之外的实时撤权检查；服务端计算资源范围 | 当前查询依赖本地身份库和 consultation/knowledge 语义，不是 PG 平台授权器 |
| [sdk/session_control.py](/Users/xiaoy/Documents/多agent协作/src/forgeharness/sdk/session_control.py) | 可选 FastAPI adapter、可信鉴权回调、对象 ID 仅选择对象、错误分类、独立审批回调 | 底层是同步 `SessionService` 与本地会话/审批存储；路由及 resume/grant JSON 不是托管运行 HTTP 契约 |
| [cloud/hosted.py](/Users/xiaoy/Documents/多agent协作/src/forgeharness/cloud/hosted.py) | 不可变注册部署、实现与模型绑定摘要、可信结果验证器、固定工具与预算；不能由用户上传导入路径 | 注册表目前由可信 Python 程序构造，没有组织部署目录、启停、授权或版本发布服务 |
| [cloud/runtime.py](/Users/xiaoy/Documents/多agent协作/src/forgeharness/cloud/runtime.py) | `accept/inspect/trace/cancel/approve/advance` 已构成串行托管内部服务；原 owner、审批、恢复和预算通过 PG 接线 | 文件明确要求上下文来自已认证宿主；没有 Cloud 登录/API 路由或公共 DTO；不能接受请求 JSON 反序列化的 `TenantContext` |
| [cloud/store.py](/Users/xiaoy/Documents/多agent协作/src/forgeharness/cloud/store.py)、[schema.sql](/Users/xiaoy/Documents/多agent协作/src/forgeharness/cloud/schema.sql) | 事务内租户设置、FORCE RLS、连接池隔离、当前组织/成员状态、执行与审批权限分离、epoch/lease、请求幂等与 outbox | `members` 仅有 active/can_execute/can_approve；账户和成员由测试或管理员 SQL 提供，没有用户认证事实或成员管理产品 |
| [commerce/operations.py](/Users/xiaoy/Documents/多agent协作/src/forgeharness/applications/commerce/operations.py)、[sdk/harness.py](/Users/xiaoy/Documents/多agent协作/src/forgeharness/sdk/harness.py) | 旧应用状态与完整执行绑定不被其他应用接管；旧 state 不能自动认领；恢复前重验绑定 | 前者绑定本地目录，后者仍为本地 SDK 状态机制，不能拿文件 marker 代替 PG deployment/workspace 授权 |

优先抽取语义与合同测试，避免 Cloud 反向依赖 `applications.commerce`。旧模块与公开导入
保持兼容；不能为了“通用化”把旧 SQLite 状态隐式迁入新 PG，也不能同时双写两套身份事实。

## 3. 公开 HTTP 前必须补齐的真实 gap

### 身份与执行授权之间仍有断点

`HostedRuntime.accept` 当前从 registration 取固定 `roles` 和 `resources`，所有获准调用该
注册的 owner 使用同一模板。PG 事务重读成员状态，但 `owner active` 不是“当前仍能使用
这个工作空间、部署和资源”的完整证明。现有内部调用方必须可信，这是已声明的边界。

新平台必须增加服务器侧授权解析，得到有效的组织、工作空间、应用授权与版本。准入和
`mark_sent` 的同一权威 PG 事务必须重新检查这些当前授权，不能只在登录或 HTTP dependency
中检查一次。否则排队期间撤权后，旧运行仍可能发送调用。

扩权不应升级旧 run 的权限：保留受理时的有效 scope 与配置身份；续跑只能在仍有相应授权
时使用原 scope。撤权必须阻止新发送，同时保留已发送结果的记录与结算路径。

### 组织、工作空间和主体不是同一个字段

目前 Cloud `org_id` 和参考应用 `workspace_id` 不能直接互换。`ResourceScope.tenant_id`
继续代表组织；工作空间需要明确的持久绑定，而不是塞进客户端可改的 header 或任意 resource
字符串。第一切片每个 deployment 可绑定一个工作空间与固定资源包；成员先按该工作空间
整体授权。更细的逐资源授权是后续能力，不能宣称已经实现。

现有 `actor_id` 是可信宿主提供的组织内标识。不能把不同组织中同名 `owner` 自动合并成一人，
也不能按相同邮箱自动绑定外部账户。新账户使用稳定随机主体 ID；未来联合身份以
`issuer + subject` 唯一绑定。既有 run/approval 中的 actor 字符串不重写。

### 公共权限需要比 can_execute/can_approve 更细

至少区分组织/成员管理、部署使用、提交运行、读取及取消本人运行、读取待审提案、批准提案、
后台执行。`can_execute` 继续是服务执行权限，不等于普通用户的“可提交”按钮。
组织管理员默认不获得业务审批权；同角色的两人仍为不同 owner；服务主体不自动获得人工
审批身份。管理员、审批人和 worker 不能因复用一个数据库账号而合并授权含义。

### HTTP 与会话还有独立工作

普通客户端不能挑选 actor、角色、资源、预算上限、实现导入路径、模型账号或可信 grant。
应用/组织/workspace 标识可选择对象，服务端必须证明其关系。浏览器还需绝对及闲置过期、
退出撤销、CSRF、Origin/Host 部署策略、受控 CORS、失败限流与私有响应缓存策略。

复用邀请流程时，应让一次性秘密通过受控请求体消费，避免直接复制把邀请令牌放 URL path
的设计；访问日志、异常日志、重定向与审计不得记录密码、原始会话/审批/邀请令牌。
现有密码实现可以作为本地适配器起点，参数及接口需要独立审查，不能据此宣称 MFA 或外部
身份验证已完成。

`HostedRun` 与内部完整 checkpoint 不宜原样作为默认 API 响应。需定义最小公开 DTO，默认
只返回 run ID、状态、用量、归一化错误和有权读取的结果；提案审阅与完整 trace 单独授权。
目前 `store.events` 无公共分页合同，`trace` 会读取有界完整历史；首切片采用状态轮询即可，
不要把全部内部 outbox 内容直接改造成 SSE/WebSocket 广播。

## 4. 最小实施顺序与交付物

### A. PG 平台身份与授权合同

- 新增应用中立的主体、外部身份映射预留、组织成员、组织内工作空间、工作空间成员、
  deployment 授权、邀请、会话与身份审计。Schema 设计先写 ADR；身份表与执行表在同一 PG
  权威中，明确全局身份鉴证与租户业务数据的不同查询权限。
- 新增显式 schema2→下一版迁移，旧数据和版本保持可解释；不能修改已发布 migration 2。
  旧 `members` 的映射/兼容规则单独声明，不把任意旧成员当作已验证的人类账号。
- 运行角色继续无成员授权写权限。身份控制服务使用独立最小 DML 权限；迁移身份不暴露给
  API。成员授权的事实与执行权限投影如有两处记录，必须同一事务更新并有版本与对账合同。
- 离线 bootstrap 建立首个组织与管理员，公开服务不开放“首个请求成为管理员”。邀请管理
  不允许靠旧 Principal 快照放权；并发撤权与最后一名组织所有者保护均需事务验证。

### B. 通用邀请制身份适配器

- 将身份/权限模型与密码或外部登录的鉴证方式分开。该切片可提供本地邀请制登录适配器，
  复用已回归的令牌摘要、一次消费、原子节流、Cookie/CSRF 和撤权语义；数据存 PG。
- 固定返回服务器生成的 Principal，组织选择必须验证当前成员关系；切换组织重新签发/
  绑定会话，不接受客户端角色快照。关闭全局账号与撤销单个组织成员关系是不同操作。
- 外部 IdP、OIDC 授权码与 PKCE、MFA/恢复保留为明确的后续必需项；不能用只接一个
  `authenticate` 测试回调就把“生产登录完成”打勾。

### C. 托管运行 HTTP 服务与独立 worker

| 建议端点/入口 | 第一切片行为 |
| --- | --- |
| `GET /v1/me`、`GET /v1/organizations` | 当前主体及实际组织成员范围，无跨组织隐式默认 |
| `GET /v1/organizations/{org}/workspaces`、授权部署目录 | 按当前 membership/grant 过滤；不暴露服务器模型配置或代码导入位置 |
| `POST /v1/organizations/{org}/workspaces/{workspace}/runs` | 严格 task + deployment ID + Idempotency-Key；服务端解析身份与部署；PG 原子受理后返回 202 |
| `GET /v1/.../runs/{run}`、`POST /v1/.../runs/{run}/cancel` | 本人及明确授权的运维角色；外组织、错误 workspace 和未知对象统一不可用；取消与实际执行状态分开显示 |
| `GET /v1/.../runs/{run}/approval`、`POST /v1/.../runs/{run}/approve` | 独立审批人自己的已认证通道；审阅 PG 中的精确提案。无须共享 owner Cookie；不把客户端提交的 grant 当身份 |
| 单独的执行服务入口 | 使用服务主体竞争 PG lease，调用既有 `advance`；Web 用户不能调用内部执行接口；本机测试仍用独立进程与独立业务/模型 oracle |

若为端到端 Alpha 增加基础 worker loop，可先用 PG 调度 outbox/扫描与固定并发 1；不新增
Redis 或云队列采购。明确它仅保证已接受任务可被找到与接管，不等于租户公平调度、重试
SLO 或月预算已经实现。审批写入后由内部 worker 继续；HTTP 不同步调用外部副作用。

### D. 可复验的一条跨业务闭环

提供同一服务承载两个合成应用的安装包消费者：组织 A 两位同角色用户、独立审批人、
组织 B、两个工作空间。验证登录/受理/轮询/审批/执行/撤权/恢复全链路。应用采用注册能力
包和独立 oracle，名称与逻辑不依赖商品、工单或电商首发。

## 5. 必需测试与安全边界

| 合同 | 应保留或新增的有效回归 |
| --- | --- |
| 鉴证与秘密 | 错误密码/未知账号统一失败；过期/撤销/重放凭据拒绝；原始秘密不进入 PG 非凭据字段、trace、审计、HTTP 错误与日志；Cookie 属性及 CSRF 经真实 HTTP 检验 |
| 当前授权与竞态 | 旧 Principal 发起成员变更仍重验；API 通过后、受理前或模型返回后撤权，下一实际 tool/model 发送拒绝；撤权过程中已发送结果可继续记账，unknown 不清零 |
| 组织与工作空间 | 伪造路径/header/body、同组织不同 workspace、同角色不同 owner、连接池归还/复用、后台进程及分页查询均不能越权；所有新表实际 PG FORCE RLS 与最小权限验证 |
| 部署与 scope | 未授权部署不可用；被停用 deployment 不新发送；同幂等键改 deployment/task/scope 冲突；旧 run 不因账号扩权、注册配置变更或路由参数而升级/替换绑定 |
| 审批 | 管理员无默认业务审批；服务账号不冒充人；本人兼任多个角色仍不能自批；过期/撤销/不同动作 grant 拒绝；审批人不需 owner 凭据；并发审批和消费只准入一次 |
| 异步受理 | 两进程重复请求只建一个 run/outbox；API 202 后断连不丢任务；调用者无 can_execute；worker 无管理员 DDL/DML 权限；API 未暴露 advance/SQL/导入路径 |
| 运行可靠性 | 复用当前 unknown、旧 epoch、取消、deadline、超额实际结算与 EXHAUSTED 回归；身份失败不伪造零成本失败，也不重复发送模型/工具 |
| 展示与错误 | 默认 DTO 不泄漏完整输入或他人资料；审批审阅内容与真正消费指纹相同；错误不回显 SQL/DSN/任意异常；私有响应 no-store；OpenAPI 无可信身份字段 |
| 迁移与兼容 | schema2 的 run/approval/receipt/证据摘要及 owner 原样保留；失败 DDL 回滚；旧本地 commerce、SDK session/router 与导入不被破坏；无 SQLite/PG 双主身份 |
| 发布 | 新 ADR、当前/计划边界、安装包新路由与 migration 数据可用；freeze 后完整门禁、真实 PG、独立 wheel consumer 和干净副本复验，不拼接开发通过数 |

直接可借用的基线回归包括 `tests/unit/test_commerce_identity.py`、
`test_commerce_workbench.py`、`test_session_control.py`，以及
`evals/saas_postgres/tests/test_run_store.py`、`test_hosted_execution.py`、
`test_hosted_budget.py`。借用的是被验证的行为；新的平台接线仍需经真实 HTTP→身份 PG→
HostedRuntime→AgentRuntime→独立 oracle 的完整测试，不以单独 mock route 测试替代。

## 6. 该切片完成后的准确结论

可宣布：已有应用中立、邀请制内部账户与组织/工作空间授权的 PG 托管 API，两个合成业务
通过同一入口，异步受理、独立审批、撤权与串行恢复经过本机完整合同验证。

仍不能宣布：通用生产级 SaaS 或公开多租户 GA 完成。IdP/MFA、机器身份完整产品、组织月
预算与供应商价格、订阅支付、资料生命周期、公平调度、真实云部署/备份恢复、SLO 观察与
真实客户业务验收仍须继续完成。当前整数单位用量不能替代用户确认的 1000 元人民币月预算。
