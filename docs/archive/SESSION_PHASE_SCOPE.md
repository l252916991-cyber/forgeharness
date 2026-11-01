# 下一阶段首轮范围与验收（S0＋S1）

日期：2026-10-14。来源：`reports/MODULAR_NEXT_PHASE_PLAN.md`。

本文件定义首轮范围。通过证据是候选绑定的
`reports/session-release-audit.json` 与 `reports/session-verification/`（冻结清单
`reports/session-candidate-source.json`）；本文件、测试计数和示例本身都不构成通过结论。
接口说明见 [SESSIONS.md](../guides/SESSIONS.md)，设计决定见
[ADR 014](../adr/014-durable-scoped-sessions.md)。

## 计划审查结论

总体方向可执行，无需推翻分层或阶段顺序。当前 257 个冻结文件与 0.3.0
候选哈希全部一致，基线树摘要为
`0ba8f040a3d480c12a3c800e47bd2950c9d5795428e20bb46107419c2d2620dc`。
原计划第 5、6 节允许 S0＋S1 独立交付，本次按该停止点推进。

S0 必须先补齐的实施细节已归入 [ADR 014](../adr/014-durable-scoped-sessions.md)：
原运行 binding 与当前身份分离；owner/approver 区分；请求去重与跨进程单写者；
历史依赖失效；会话副本的清洗与删除；普通运行与 workflow 的统一关联。
这些是落实既定验收条件，不是新增真实业务范围。

## 需求与复用映射

| 需求 | 复用 | 首轮新增与可观察结果 |
| --- | --- | --- |
| 可信身份与资源隔离 | Actor / ResourceScope / RunContext | SessionService 每次授权，固定会话归属；跨案件/商家拒绝 |
| 会话与轮次 | 旧 knowledge 仅作反例与兼容基线 | 新 SQLite 契约、请求键、顺序与关闭；重启保留历史 |
| 多轮上下文 | ContextProvider / ContextCompiler / runtime assembly | 有界历史、不可信材料、版本校验、实际 model.request |
| 运行与审批 | Harness / SQLite ledger / invocation journal | 每轮稳定 run、独立审批身份、失败与待审批可区分 |
| 多节点工作 | WorkflowRunner | workflow_id 与轮次关联、状态/事件/恢复 |
| 引用与已审核知识 | RevisionStore / PublishedContextProvider | 由宿主校验当前材料，禁止过期资料经历史重新进入模型 |
| 可选入口 | 现有 control_router 的认证模式 | session_router，身份不从正文读取；合成客户端示例 |
| 证据删除与隐私 | EvidenceTransform / Harness.delete / workflow delete | 会话副本清理、墓碑、衍生历史失效 |
| 验收 | 原质量与 keyless gates / snapshot | 新候选清单、独立审查、两次完整门禁与构建安装 |

各步骤的可执行证据：服务级契约 `tests/unit/test_sessions.py`，HTTP 与身份再认证
`tests/unit/test_session_control.py`，两业务会话示例 `examples/session_demo.py`，
候选冻结与干净副本复验 `benchmarks/session_verification.py`。

## 角色、资料和数据流

法律示例：合成案件拥有者、独立 supervisor、另一案件/租户的无权用户。
资料只有示例生成的文本和版本，不读取 LexVault 或真实案件。电商示例：
合成商家拥有者、另一商家无权用户；模拟资料和 API，不接真实店铺。

流程：宿主认证 → 当前资源授权 → 固定会话 → 稳定轮次登记 → 校验版本及历史
→ Harness/WorkflowRunner → 运行证据 → 轮次对账 → 当前授权后读取结果。
审批流程另需当前 owner 和 approver；审批授予不是业务资料审核。

## 完整验收脚本

| 步骤 | 法律会话 | 电商会话 | 预期 |
| --- | --- | --- | --- |
| 1 | 创建合成案件会话 | 创建合成商家会话 | 持久身份绑定；正文无法覆盖 |
| 2 | 询问合成材料 | 询问模拟商品 | 成功轮次关联真实 run/workflow |
| 3 | 连续追问 | 连续追问 | 实际请求含预算内前轮内容与版本 |
| 4 | 同请求键重试/冲突正文 | 同左 | 不新建运行；不同正文冲突 |
| 5 | 另一个实例同时提交 | 同左 | 单写者拒绝，轮次不交叉 |
| 6 | 触发待审批笔记步骤 | 复用受控流程作审批验证 | 状态可查；未批准不写 |
| 7 | 重建服务，再由独立审批者恢复 | 重建服务继续多轮 | 状态来自磁盘；不重复副作用 |
| 8 | 跨案件/撤销 owner 或 approver | 跨商家/撤权 | 历史、事件、审批、恢复拒绝 |
| 9 | 来源版本变更或撤回 | 商品资料版本变更 | 旧历史/恢复不能绕过失效 |
| 10 | 删除证据，再读/恢复/追问 | 同左 | 墓碑阻止复用；副本不回流 |
| 11 | 关闭会话 | 关闭会话 | 阻止新提交，活动轮次处理明确 |

以可执行测试和示例为准；表格不替代通过证据。确定性测试证明隔离、状态、幂等和
输入证据；真实模型事实/引用质量与人工专业判断分别留给 S3–S5。

## 持久化与迁移边界

新会话表使用独立 schema version；旧 knowledge 表不迁移、不重新授权旧资料。
保留公共 SDK API v1，新增类型采用增量导出。版本上调至 0.4.0 候选后重建锁文件
中的项目元数据，无意变更依赖版本。S1 保持本地可信宿主模型，认证回调不是自带 IAM。
资料校验、身份认证与实时工具权限查询由宿主提供，服务启动时拒绝缺少所需校验器的
外部 provider。来源失效采取拒绝策略，不自动生成未经评估的摘要替代原文。

## 首轮之外

S2 版本化导入与持久任务、S3 LexVault 接入与领域对照、S4 完整电商 MVP、S5
封存业务终评仍未交付。既有示例不替代新阶段验收。不得将本轮通过理解为旧增强平台
FH-015–018 关闭。外部仓库、真实账号、真实模型与专业评审另按计划要求接入。

原 124–188 工时是全阶段粗估，不作为本次工具运行的耗时承诺；后续阶段在首轮审计
确认公共契约后重新估算。
