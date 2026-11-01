# 产品化执行状态

日期：2026-10-27。状态：SDK 0.9.2 工程候选已冻结并通过源码及 checkout-equivalent 干净副本资格验证；尚未取得真实宿主或业务资格。

## 已实现并有回归覆盖

| 工作包 | 当前结果 | 证据入口 |
| --- | --- | --- |
| P0 范围与契约 | 选择电商咨询与人工工单、单机 POSIX/SQLite、内部受控部署；工程、接入和业务结论分开 | `SCOPE.md`、`application-bindings.json`、`acceptance-contract.json` |
| P1 操作闭环 | 控制台可提交咨询、查看历史、审批并继续、查看轮次与 hash-chain 事件；重复 request id 复用原轮次 | `src/forgeharness/applications/commerce/console.html`、`tests/unit/test_commerce_control.py` |
| P2 缺失业务分支 | 无 SKU/无证据且用户明确请求人工时，可走独立审批创建通用工单；租户内 support 可查询；业务请求去重 | `docs/adr/037-general-commerce-handoff.md`、`tests/unit/test_consultation.py` |
| P3 恢复安全 | 待审批通用工单经过停机备份、删除原状态、同路径恢复后可审批续跑；重复续跑被拒绝且只生成一个工单 | `tests/unit/test_commerce_operations.py` |
| P4 本地运维 | 增加 live/ready 探针、服务互斥锁、SQLite 一致性备份、成员与摘要验证、仅原绝对路径原子恢复及操作手册 | `docs/COMMERCE_OPERATIONS.md`、`examples/commerce_state.py` |
| 安全门禁 | 新增的通用转接、访问控制和恢复场景已加入业务安全节点清单，产品化选择 69/69 通过 | `evals/business/safety.json`、`development/GATE.md` |
| P6 工程候选 | candidate-r1 源码及干净副本各 1339/1339；wheel、sdist、最小安装、示例、审计和比较协议均通过 | `candidate-r1/verification/summary.json` |

直接咨询是一个同步 HTTP/SessionService 轮次，没有另外伪造后台任务对象。因此控制台不展示无效的“后台任务/取消”区域；它展示真实的持久轮次、审批和事件证据。长时间后台导入仍属于现有 TaskStore 路径，不是本应用首版咨询主链路。

## 已发现并修复的高价值缺陷

恢复演练发现：当深层文件先恢复时，中间目录可能继承系统 umask，而不是会话所要求的 `0700`，导致恢复后应用拒绝启动。恢复实现现在逐级创建并强制所有目录为 owner-only；端到端恢复测试固定该行为。

## 尚未完成，不能声称已落地

| 阻塞项 | 需要的外部输入 | 当前结论 |
| --- | --- | --- |
| 真实身份与即时撤权 | 宿主认证接口、角色/租户映射、撤权查询 | 演示身份，不是生产 IAM |
| 实时商品、库存、价格和 FAQ | 授权数据源及版本/撤回协议 | 合成数据参考实现 |
| 真实工单渠道 | 支持业务幂等键、状态查询和超时核对的接口 | 本地 SQLite 工单参考实现 |
| 目标模型质量 | 固定模型、参数、预算及真实运行环境 | 仅确定性 fixture 与合成开发集 |
| 业务验收 | 业务负责人、独立使用者、未用于调参的终评集 | 未开正式终评，业务资格未验收 |
| 运维目标 | 部署负责人、RTO/RPO、保留期限、通知渠道 | 只证明受限本地恢复机制，不证明生产目标 |

这些不是继续增加 Agent 功能可以解决的问题。取得外部输入前，合理出口是“工程参考应用”，不是“完整生产项目”。

## 下一门禁

1. 外部依赖到位后接入真实宿主认证、业务数据和工单接口。
2. 固定目标模型、预算、业务评分和独立终评集，先运行开发集，再开不可复用的正式终评。
3. 由独立使用者按运维手册完成安装、主链路和恢复演练，并由负责人接受 RTO/RPO 与残余风险。
