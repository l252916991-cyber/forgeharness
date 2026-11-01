# 0.4.0 候选持久会话审查记录（S1）

日期：2026-10-14。方式：维护者式源码审查、故障/边界回归、候选冻结与干净副本复验。
没有独立外部安全认证，也没有真实模型、真实租户或真实法律/电商质量验证。

本轮对应下一阶段计划的 S0＋S1（`reports/MODULAR_NEXT_PHASE_PLAN.md` 第 5、6 节）。
S2–S5 未交付；S0 的范围、复用映射与验收脚本见
[SESSION_PHASE_SCOPE.md](../SESSION_PHASE_SCOPE.md)，设计决定见
[ADR 014](../adr/014-durable-scoped-sessions.md)，公共契约见
[SESSIONS.md](../SESSIONS.md)。

## 审查范围

* 架构：会话层只通过公开 `Harness`/`WorkflowRunner` 执行、查询、审批、恢复与删除，
  不读取框架内部表；基础 SDK 与 wheel 不依赖 FastAPI，可选 router 单独导入。
* 绑定与授权：会话固定 application/策略版本、tenant、owner、资源与工作区；
  每个操作按当次认证上下文重新询问宿主；正文不能提供角色、租户或范围。
* 并发与恢复：每会话单写者、挂起轮次阻塞下一轮、稳定请求键去重、崩溃后用稳定
  run ID 对账；无法确认的状态不报成功。
* 隐私与删除：材料先经 `EvidenceTransform` 再入库；删除写墓碑并清理副本，
  已删身份不得复用，恢复不使用已删证据。
* 测试：服务级契约、HTTP 身份再认证、验证驱动自身的回归，以及两条业务会话示例。
* 依赖与构建：无新增运行依赖；最小 wheel 安装不含 Web/框架 extras；sdist 白名单。
* 文档：以 `docs/STATUS.md` 为实现/计划边界，并修正 S1 引入的文档矛盾。

## 发现分类

| ID | 等级 | 状态 | 修正与回归 |
| --- | --- | --- | --- |
| SESS-001 | high | fixed | 已完成的轮次收到重放的审批授予时返回 200 成功，调用方会误以为本次授予刚被消费；改为明确拒绝（409），保留授予一次性语义，`test_http_delegated_approval_reauthenticates_and_restarts`。底层账本本身仍拒绝重复消费，属纵深防御 |
| SESS-002 | medium | fixed | 已删除会话的身份被复用创建时返回 409，暗示改个名字即可；改为 403 且身份永久退役，`test_deleted_session_retires_its_identity`、`test_http_session_lifecycle_restarts_and_retains_history` |
| SESS-003 | medium | fixed | 验证驱动在源码树已不可完整哈希时（例如门禁自身删除了文件）丢弃失败摘要，恰好丢掉失败证据；改为记录失败并照常落盘，`test_driver_exception_persists_failed_result`（参数化两种形态） |
| SESS-004 | medium | fixed | 发布门禁的 Ruff 调用使用 `--exclude reports`，该参数会替换 Ruff 默认排除项并扫描整个 `.venv`（约 9.6 万条），lint 门禁在树内不可能通过；改用 `--extend-exclude` 并在 `pyproject.toml` 固定排除 `reports/` |
| SESS-005 | medium | fixed | 新增会话代码从未格式化和 lint（24 条发现、4 个文件需重排），且 `benchmarks/` 不是可导入包，导致验证测试模块连收集都失败；补齐格式化、lint、包声明与 pytest `pythonpath` |
| SESS-006 | low | fixed | README 与范围文件把 `SessionService` 记为公开 SDK，但 `forgeharness.sdk` 未导出；按“增量导出”约定导出会话类型（`session_router` 仍只在子模块，避免基础 SDK 依赖 Web） |
| SESS-007 | low | fixed | 架构测试的可选适配器豁免若按目录扩展，会顺带放过整个 `sdk` 包；改为逐模块豁免，并新增“适配器必须声明自身平台依赖”和“`import forgeharness` 不加载可选平台包”的断言 |
| SESS-008 | low | fixed | `docs/BUSINESS_MODULES.md` 仍称会话未迁移为通用模块，`docs/SDK_GUIDE.md` 仍以 0.3.0 为适用发行且只记录 `control_router`；更新为 0.4.0 候选口径并补会话入口与 schema 版本 |
| SESS-009 | high | fixed | 工作流会话里宿主适配器抛错时，轮次只停在 `starting`：既不记录工作流已持久化的挂起状态与错误，也不说明原因，后续只能靠删除轮次恢复；改为按工作流自身的持久状态如实记录（含 `node_control_error`），并用原轮次 `resume` 继续，`test_workflow_adapter_failure_does_not_lose_durable_state` |
| SESS-010 | medium | fixed | sdist 会带上打包工具默认加入的 `.gitignore`，而它不在冻结身份里，于是“sdist 每个成员都必须已冻结”的门禁永远无法通过；把 `.gitignore` 纳入内容寻址身份（缺失文件不参与摘要，删除仍会改变身份），保持“sdist 只含已审查输入”的规则不放宽 |

SESS-001–003 与 SESS-009 是行为缺陷，均在候选冻结前修复并带回归测试；
SESS-004–008、SESS-010 属门禁、构建与文档一致性缺陷，同样在冻结前修复。

## 验证结果

* 确定性测试 699 项全部通过（0.3.0 基线 654 项，本轮新增 45 项），
  无跳过、无预期失败；框架纯分支覆盖率约 86.6%（阈值 85%），合并覆盖率约 92.1%；
  精确数值以候选审计 `reports/session-release-audit.json` 记录的门禁输出为准。
* keyless 控制评测 7/7；keyless Agent Benchmark dev 拆分 38 用例 × 2 策略与
  7 项评分探针通过，token 为合成值、不进入成本结论。
* 依赖审计：默认 10 个运行依赖无已知漏洞；全 extras 仅剩已接受的
  `PYSEC-2026-3740`（可选 `frameworks` extra 的 NLTK 传递项，见 FH-019）。
* 构建：wheel 与 sdist 的成员与冻结清单逐一比对；隔离消费者环境只装 wheel，
  不加载 FastAPI/LangGraph，并运行三个示例。
* 干净副本复验：冻结输入逐文件复制并校验哈希后重跑同一组门禁；两次结果一致，
  验证结束后源码树与冻结清单仍一致。

## 接受的剩余限制

* 单机 POSIX、本地 SQLite、可信宿主；单写者是非阻塞文件锁，不是分布式 lease，
  两台主机共享同一状态目录不受支持。
* 认证是宿主回调，不是自带 IAM；HTTP 适配层必须置于真实认证之后，且默认面向本机。
* 不迁移旧 knowledge 会话与记忆；会话内容不会自动成为可信长期事实。
* 删除覆盖会话自身副本与其运行证据；宿主侧备份、外部附件与领域保留期不在范围内。
* 可观察结果只到确定性框架行为与合成应用；多轮质量、引用适用性、真实租户隔离与
  真实业务结论未验证，属于 S3–S5。
* 历史增强平台 FH-015–018 与可选 NLTK 接受项不因本轮通过而自动关闭。

## 冻结与放行依据

基线保存在 `reports/session-baseline-source.json`，本轮候选逐文件冻结在
`reports/session-candidate-source.json`。当前目录来自源码导出，无 Git 元数据，
不编造提交哈希。验证命令、日志、覆盖率、依赖审计、构建摘要与干净复验结果保存在
`reports/session-verification/`，结论以 `reports/session-release-audit.json` 为准。
门禁失败时不得仅凭本文标为完成。
