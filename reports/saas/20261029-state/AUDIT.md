# PostgreSQL 状态首切片审查

日期：2026-10-29。性质：实施团队多 agent 交叉审查与真实数据库故障回归，非第三方渗透测试/合规认证。
候选：0.11.0 candidate-r1。冻结后不改源码；最终结论读取 verification/summary.json。

## 范围与结论

审查架构、授权/RLS、运行角色、事务原子性、租约/epoch、精确审批、预算、收据和 outbox、
测试真实性、依赖、wheel/sdist 内容、基础依赖隔离、版本与公开声明。

独立审查 agent 用临时真实 PG 复现 4 项 P1 和 1 项 P2，实施 agent 修复并补充回归。
审查 agent 对最终实现只读复核：未发现新的高风险问题。不是完整生产安全认证。

| 编号 | 等级 | 已确认问题 | 修正与回归 |
| --- | --- | --- | --- |
| PG-001 | P1 | 已存不可变 not_executed 收据的 admitted 调用仍可发送，随后真实支出与旧收据冲突 | mark_sent 先锁 invocation 再查收据；顺序与锁等待回归拒绝后续发送 |
| PG-002 | P1 | 初次验租约后等待审批/调用锁至过期，仍可准入/发送/更新投影 | admit/send 在全部锁后再验 DB 时钟租约/期限；unknown/settle 再验租约；4 个过期与2个deadline回归 |
| PG-003 | P1 | 管理员 DSN 通过 startup role 伪装低权限 current_user，可 SET ROLE NONE 恢复高权限 | 拒绝 current_user != session_user；保留超级用户/所有者/NOINHERIT 提权成员拒绝；真实连接回归 |
| PG-004 | P1 | 前次实际用量超出预留后，已准入但未发调用仍可发送 | 发送前检查 used+reserved<=max；实际7/12、总额10、剩余预留4的两场景均拒绝，保留预算直至可信核对 |
| PG-005 | P2 | receipt outbox FK 的 run key-share 与 sender 的 run→invocation 锁顺序相反，导致死锁 | receipt 先 run KEY SHARE 再 invocation；用实际 receipt/send API 的两等待者屏障回归，验证无死锁 |

PG-005 中 PostgreSQL 会回滚受害事务，未确认错误数据提交，按 P2 分类；本批也完成修复。
开发回归还修正了观察事务缓存 pg_stat_activity 的测试问题：每次观测先清理统计快照，
同步真实数据库锁等待，不依赖猜测的调度延迟。失败开发产物保留在 reports/saas-postgres/。

## 架构和权限

- 独立 Cloud 包，不把 SQLite 机械替换为网络连接；平台操作各自一个事务，无外部请求在事务内。
- 每个 tenant 关联键携带 org；强制 RLS；事务局部上下文；连接池异常/复用回归；当前成员和组织以行锁重验。
- RLS 设置不是用户认证。运行角色只给可信服务，普通用户没有 can_execute、SQL 或回执写入权限。
- provisioning 独立管理员，运行角色不拥有 schema/table，不具有可提权成员资格，不能更新权限/收据。
- immutable receipt 是事实，只有当前 epoch 可改运行投影；取消、组织停用、owner 撤权仍结算真实成本。
- unknown 和旧 epoch admitted 调用不会自动释放或重发；待原调用身份核对。
- outbox 与状态一同提交；本批没有交付投递器、扫描调度器或客户事件订阅。

## 验证质量

实际 PG18.6、低权限运行身份、私有 Unix socket、随机独立数据库；缺失依赖直接失败，禁止skip。
两个独立进程验证 submit/claim 竞争，其余并发多为独立连接；不能扩大为完整跨机故障矩阵。
源码与干净副本都执行核心和 PG 测试，再用联合覆盖证据检查 combined/pure branch各>=85%。
独立 wheel consumer 验证 packaged SQL、迁移、授权、精确审批、结算；基础 consumer 不依赖asyncpg/Web。

依赖审计检查 core/all extras/两个框架环境；只沿用历史 NLTK optional 比较路径的例外。
测试服务器由 embedded-postgres 18.6.3 提供，来源/许可证见 ADR040；不是生产数据库交付。

## 已声明的剩余限制

1. 只实现通用平台状态首切片，M1/GA 未完成；Harness/SessionService、完整控制面、认证/IdP、真实 provider 仍未接线。
2. `record_receipt` 的真实性由可信宿主/供应商鉴证承担，不能开放给用户或把超时当作未执行。
3. 未实现组织月额度、provider-account 容量、逻辑节点/attempt/checkpoint、共享 trace/输入输出证据、资料墓碑和留存策略。
4. 未实现调度/队列恢复和事件投递、跨节点崩溃/提交响应丢失、数据库切换或云备份故障矩阵。
5. 单个 run 整数单位预算不等于1000元/月总账；需模型/价格绑定、组织额度、计量核对和账单模块。
6. 未连接用户已有服务器/模型服务，没有试点真实业务、商业收费或持续SLO证据。下一步先完成通用平台，不强制电商首发。
7. 租约与 send intent 无法撤销外部已发请求；实际 provider 必须有鉴权/幂等/回执与独立恢复协议。
8. 当前 finish 允许实际已完成且全结算的调用在取消/owner停用后收口；这是保留真实结果，不表示新授权或副作用回滚。

以上是待补能力，不是给完整 SaaS 的风险豁免。发布范围扩展时必须实现并重新验收。
