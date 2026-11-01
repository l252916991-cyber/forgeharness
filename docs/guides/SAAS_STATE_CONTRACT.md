# SaaS 状态仓储首切片事务契约

日期：2026-10-28，2026-10-29 更新。状态：完整目标契约；部分操作已在独立 Cloud 层实现，见
[已实现子集](CLOUD_STATE.md) 和 [ADR 040](../adr/040-postgres-execution-state.md)。
尚未接入 SDK/Harness，也未完成本契约的全部故障矩阵；SDK 的 SQLite 支持范围不变。

## 1. 范围

第一切片只验证平台运行受理和发送治理，不接入现有 Harness，不执行外部业务写入。现有 SQLite 运行保持其版本与物理路径约束。真实 PG 环境通过后，再将同一契约接入 SessionService、审批、checkpoint、R2 和 provider 调度。

权威存储须覆盖组织、部署版本、run、逻辑节点、attempt、lease、approval、预算预留、调用意图、回执、provider capacity claim、outbox、证据清单与删除墓碑。不能只抽取 sessions/chat_messages。

## 2. 信任与标识

可信调用方提供 authenticated org/actor/scope；请求体中的 org_id/owner_id 不构成授权。每次操作需要当前组织/成员和对象权限检查。

所有关联键带 org_id，run 唯一身份绑定 app/policy/deployment/config 版本；逻辑 invocation 标识稳定复用，物理 attempt 单独编号。新 SaaS 身份不绑定机器目录。每个新的 owner 取得严格增加的 epoch；时钟由数据库提供。

## 3. 必需原子操作

| 操作 | 同一事务必须完成 | 冲突/失败行为 |
| --- | --- | --- |
| accept_run | 校验当前授权/配额；比较业务键和 payload 摘要；创建 run + 请求幂等行 + 调度 outbox | 同键同摘要返回原 run；不同摘要冲突；事务失败无半个任务 |
| claim_run | 锁定可执行对象；检查状态、取消/期限；更新 owner/epoch/lease | 重复消息最多一个 owner 成功；不可接管未知调用后盲重发 |
| heartbeat | 以 org/run/owner/epoch 和未过期租约作条件更新 | 过期 owner 不能续租或恢复旧权限 |
| authorize_call | 当前授权/版本/epoch/期限重验；消费精确未过期审批；预算预留；写 invocation + dispatch intent | 任一步失败全部回滚，审批不能已消费而调用不可查询 |
| mark_sent | 当前 epoch/准入条件及持久 invocation 比较；持久发送事实 | 无法确认是否已发送时标记不确定，不当作未发送 |
| ingest_receipt | 按 invocation/attempt/receipt_id 去重追加事实和来源摘要 | 对不可信回执做边界验证；同键不同内容拒绝并报警 |
| settle_call | 当前 owner/可信核对者验证 receipt；结算实际用量/预留/物理容量；保存规范化输出与证据引用 | unknown 继续保守预留；超额实际用量仍记录但阻止新准入 |
| finish_run | 所有依赖和副作用可判定；更新终态 + 用量/结果 outbox | 不因缺回执显示成功；stale epoch 不能改运行投影 |
| request_cancel | 持久取消事实、停止新准入、写事件 | 已发送请求进入核对；取消不等于回滚 |

事务接口应声明可重试的冲突及幂等行为，避免上层对“任何数据库异常”重放整个外部动作。网络调用不能被放进数据库事务以假装原子提交。

## 4. 租约、容量与迟到结果

claim 超时可允许其他 owner 接管控制流，但不清除 provider 已发出/unknown 的 claim。provider-account 容量域是集群共享的；单机 flock 获得独占后清理本地 reservation 的逻辑不适用于 PG 集群。

迟到回执追加入口与运行状态写入分开：旧 owner 不能结束 run，但经过鉴证的真实用量不能丢失。receipt 的内容、来源、接收时点和原 invocation 保留；当前 owner 消费这些事实时再次检查身份、schema 和业务终验。

## 5. 租户数据库约束

RLS 在读/写上均默认拒绝；应用角色无 superuser/BYPASSRLS/表所有者权限。设置租户上下文必须限定在当前事务，连接池释放/异常路径后不得串用。org 组合外键阻止跨租户引用。迁移角色与应用角色分开，迁移使用独立发布步骤。

## 6. 故障验收矩阵

| 注入点 | 必须可复核的结果 |
| --- | --- |
| accept 事务提交前/后崩溃 | 全部不存在，或原 run 和可重建调度均存在 |
| 两进程同业务键同时提交 | 一个业务 run；改 payload 冲突 |
| 两 worker 同时 claim | 一个有效 owner；epoch 单调 |
| 暂停旧 worker → lease 过期 → 新 worker 接管 → 恢复旧 worker | 旧写入拒绝，旧外部请求必须按原标识核对 |
| approve 与 authorize_call 并发 | 最多消费一次，预留/准入无拆分状态 |
| 发送前撤权/取消 | 不进入下一外部调用，已有用量不丢 |
| provider 收到请求但响应丢失 | unknown，保留预算/容量，不换 invocation 重发 |
| 旧 owner 迟到合法用量 | 收据去重保留，由当前 owner 核对，不覆盖新状态 |
| 数据库切换/提交响应丢失 | 按稳定键查询事务结果，不把异常当作未提交 |
| Redis 队列丢失/重复 | PG outbox/扫描恢复调度，不丢已受理 run |
| 连接池复用/异常回滚 | 组织 B 看不到 A，错误不携带外租户内容 |
| 删除墓碑在恢复前后存在 | 旧备份不能重新暴露被删租户；已完成副作用不再执行 |

每个用例必须在真实 PG、真实应用角色、至少两个独立进程上运行；mock SQL 或仅 SQLite 通过不能认证 PG。产物绑定 schema/镜像、代码摘要、配置和故障时间线。

## 7. 本轮环境

本机有 asyncpg 客户端依赖，但系统无现成 postgres/psql/initdb；docker 客户端指向的 daemon 当前不可连接。后续通过独立 uv 工具环境中的 pgserver 0.1.4 成功启动一次临时 PostgreSQL 16.2，仅 Unix socket，验证后关闭并移除合成状态。该旧版本只作隔离环境探测，不作为生产选型或本项目依赖；来源与证据见本批报告。

随后锁定 embedded-postgres 18.6.3，使用实际 PG 18.6、随机独立数据库与受限运行角色验证本轮首切片。
独立进程竞争受理/claim、事务回滚、RLS、撤权、预算和迟到回执测试纳入发布门禁。
数据库切换、Redis 丢消息恢复、资料墓碑、provider capacity 和完整 SDK 恢复仍未验收。
没有启动现有 ForgeCommerce 虚拟机或使用其数据库。
