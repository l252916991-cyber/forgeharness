# 0.3.0 公共业务模块审查记录

日期：2026-10-12。方式：维护者式源码审查、故障/边界回归、冻结源码与干净环境复验。
没有独立外部安全认证或真实法律/电商模型质量验证。

## 审查范围

* 架构：SDK 模块不导入业务应用；所有参考应用只依赖公开 SDK 和自己的业务模块。
  原有 Coding 与商品/工单兼容路径保持；新流程通过 Harness 执行，不复制调度/审批/journal。
* 正确性：图结构、预算预留、父/子状态衔接、来源版本依赖、业务审核与执行审批分离。
* 测试：已有完整测试与新故障/并发/跨范围/过期/错误审批/引用篡改回归。
* 安全：身份由宿主提供；本机锁、私有目录、数据命名空间、默认遮盖与当前权限检查。
* 依赖：无新增运行依赖；完整依赖门禁以新报告为准。
* 文档与项目描述：只主张合成参考应用与确定性框架行为；更新旧计划的时点说明。
* 构建：最小 wheel 与 sdist 白名单；从隔离消费者环境运行新旧示例。

## 发现分类

| ID | 等级 | 状态 | 修正与回归 |
| --- | --- | --- | --- |
| MOD-001 | high | fixed | 节点各自成功不证明流程结束时来源仍有效；加入 WorkflowVerifier，`test_workflow_final_verifier_catches_source_change_between_nodes` |
| MOD-002 | high | fixed | 派生稿可能在来源变更后继续批准/发布；加入事务内 RecordDependency 检查及发布读取时传递失效，`test_dependency_versions_and_review_preconditions`、两应用共享草稿测试 |
| MOD-003 | medium | fixed | 无效审批者不应把仍可处理的等待流程永久改为 FAILED；保留可恢复父状态，`test_bad_approval_keeps_workflow_resumable` |
| MOD-004 | medium | fixed | 重复依赖可能放大递归校验开销；同事务缓存、深度/数量上限，`test_cycle_rejected_and_shared_dependency_checked_once`、`test_dependency_expansion_has_a_total_bound` |
| MOD-005 | medium | fixed | 节点完成事件先清空 active，导致事件缺节点名；保留完成前节点身份，`test_dag_order_input_handoff_and_aggregate_usage`，red 证据见 `reports/modular-event-red.log` |
| MOD-006 | medium | fixed | 全 extras 的 LlamaIndex 间接依赖 banks 2.5.0 命中 GHSA-556j-vv39-8rqv；锁定升级到 2.5.1，重跑对照兼容测试与完整依赖门禁 |

MOD-006 的版本与影响依据：[上游安全公告](https://github.com/masci/banks/security/advisories/GHSA-556j-vv39-8rqv)。
该依赖只来自可选 `frameworks` extra；新业务模块不使用 DirectoryPromptRegistry。

父子事务不能跨数据库原子提交；以稳定子 ID、已有调用日志和可重入父推进衔接。
该限制有 `test_completed_child_reconciled_after_parent_checkpoint_crash` 与
`test_draft_effect_before_journal_crash_recovers_with_no_duplicate` 验证，
不外推为任意外部业务系统的 exactly-once。

## 接受的剩余限制

* 单机 POSIX、本地 SQLite、可信宿主/插件；无分布式 lease、OS 沙箱或完整 IAM。
* 顺序 DAG；没有并行调度、动态计划、流程级主动取消与通用后台任务服务。
* 所有节点预算静态预留；沿用节点模型用量与 soft deadline 的既有语义。
* 图摘要不等于 Python 实现指纹；宿主必须对代码、模型配置、策略与隐私变更升级版本。
* 精确引文只证明当前来源和字符区间，不证明答案全部主张、法条现行有效或法律适用。
* 资料依赖只检查本地版本账本；外部附件、API 和素材许可由应用核验。
* 凭据遮盖是启发式的；PII、容量配额、外部备份和物理擦除归宿主治理。
* Workflow 删除只清理运行证据；业务资料有自己的 delete/保留周期，不隐式级联删除案件。
* 参考应用仅含合成资料与确定性模型；未迁移 LexVault 原工作台，未交付完整电商平台。
* 历史增强平台 FH-015–018 与可选 NLTK 接受项不因本轮通过而自动关闭。

## 冻结与放行依据

基线保存在 `reports/modular-baseline-source.json`；本轮候选在
`reports/modular-candidate-source.json` 逐文件冻结。当前目录来自源码导出，无 Git 元数据，
不编造提交哈希。验证命令、日志、覆盖率、依赖审计、构建摘要和干净复验结果保存在
`reports/modular-verification/` 与 `reports/modular-release-audit.json`。
门禁失败时不得仅凭本文标为完成；最终结论以候选绑定的报告为准。
