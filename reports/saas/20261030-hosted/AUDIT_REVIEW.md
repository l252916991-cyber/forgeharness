# 0.12.0 开发候选审查记录

审查日期：2026-10-30 至 2026-11-01（Asia/Shanghai）。这是实施团队的工程自审，
不是第三方安全认证、生产上线签收或完整 SaaS 完成声明。报告目录沿用本批开始日期。

## 基线、范围和证据身份

基线为只读历史候选 `reports/saas/20261029-state/candidate-r1/source.json`，
tree digest 为 `dc9ef875b25c814e11cd01b0b25d01fc0a057098243823213c7a0959dd15c213`。
本审查没有改写该候选、其 clean copy、发行物或历史报告。

`AUDIT_DIFF.json` 记录与该基线不同的全部路径。其审查点有 62 个新增文件、43 个改动文件、
没有删除文件；其中生产源码新增 18 个、改动 27 个。审查点摘要不是最终冻结身份；后续
根代理修复、版本/协议和候选准备仍可能改变源码。最终身份只读取新候选 manifest。

审查并不局限于 cloud。逐项确认基线之外还包括 todo board、任务/状态 pin、prompt cache、
FileBackend/local/container/composite、技能安装、spill、参数策略、CodingAgent 接线、
optional SDK 导出、发布工作流和文档。重点逐行追查这些模块的权限、发送、恢复、用量、
文件和发布边界；遗留代码追查到其相关调用链。未宣称逐行审计所有既有平台、业务应用
和第三方依赖实现。

## 架构及正确性结论

- 托管串行执行复用现有 `AgentRuntime` 和模型/工具边界。没有另写模型循环，也没有在
  PG 租约外围叠加 SQLite Harness。SQLite 在测试中仅用作独立合成业务效果账本。
- PG 保存输入、所有者/部署绑定、完整 checkpoint、逐维用量、调用准入/发送/收据、
  有序证据和 outbox。`RunResult` 的消息游标与物理结果事实分开推进，恢复先核账。
- 未知响应仍保留预留；只有可信 resolver 或已验证结果能结算。不把超时、撤权、取消或
  租约丢失解释为免费失败。旧 epoch 的事实可追加，控制状态仍受当前 epoch 隔离。
- 只有旧 epoch 仍为 admitted、从未取得发送许可的调用可产生 fenced-unsent 证明，
  原子释放预留并结束原运行；sent/unknown 不适用。重试须显式创建新运行。
- 策略拒绝、未知工具、无效参数使用独立逻辑用量事实，不伪造物理收据。保存时把完整
  typed physical result、不可变 receipt、单位费用和逻辑事实一并重新核对。
- 工具参数先按已注册 schema 规范化，审批、checkpoint、准入和实际派发使用同一参数；
  工具取得 PG 规范幂等键。发送闭包单次使用，返回/挂起后失效。
- 核心仍不依赖 LangGraph。外部框架只属于可选集成与比较环境。

## 已确认缺陷与修复分类

以下 blocker/high 都在本次开发树中修复；“已修复”指代码及定向回归状态，不提前替代
最终完整门禁和干净副本复验。最终报告必须绑定实际冻结身份与复验结果。

| 编号 | 原严重度 | 可复现问题 | 修复及回归位置 |
| --- | --- | --- | --- |
| H-01 | High | 模型/工具未知响应可与消息游标、费用分离，错误恢复可能重复发送或遗漏收费 | PG 完整调用事实、可信核验/恢复、epoch fence；`test_execution_calls.py`、`test_hosted_execution.py`、`test_hosted_http.py` |
| H-02 | High | policy-denied/unknown-tool 递增 runtime tool_calls，没有物理调用，物理账本一致性检查拒绝保存 | 独立 `execution_logical_usage`，新鲜 proposal/observation/trace 绑定和防重放；`test_execution_logical_usage.py` |
| H-03 | High | 外部 schema 无效但策略允许时，准入重新校验抛错，authorized checkpoint 恢复继续失败 | hosted 确定性 INVALID_ARGUMENTS 拒绝，计逻辑用量；BoundPolicy 原生验证拒绝用 typed rule 区分；runtime 与真实 PG 回归 |
| H-04 | High | 已证明未执行或旧 admitted 调用每次恢复都挂起，没有可重用输出且无法退出 | fenced `retire_admission`、零费用证明、当前租约内结算，控制器明确失败；`test_execution_retirement.py` 5 个真实 PG 回归 |
| H-05 | High | backend 构造时重新执行 owner 授权，撤权可能阻断已发生费用的核账 | 结构绑定校验与新发送授权分离，当前执行服务可核账，物理发送仍重新授权；backend/revocation 回归 |
| H-06 | High | `SkillInstaller` 默认 LocalBackend，下载包的 smoke_command 直接在主机执行 | 不配置 backend 即拒绝 smoke；显式 LocalBackend 才是宿主的可信代码选择；`test_skill_install.py` |
| H-07 | High | urllib 自动重定向绕过初始 HTTPS/host allowlist | 每跳重新检查 scheme、host、无凭据 authority 和总 hop 上限；真实回环 HTTP 测试确认被禁目标未被请求 |
| H-08 | High | 技能升级先删除旧目录，新 rename 失败丢失完整旧包；并发 writer 和目标 symlink 破坏边界 | per-skill flock、no-follow dirfd、随机备份、失败恢复、隐藏 staging/backup 不加载；失败/并发/5 类 symlink 回归 |
| H-09 | High | 技能来源 query/userinfo 的秘密可进入普通 trace recorder | 日志去 query/fragment/userinfo，完整来源 SHA-256 保留身份，全部 payload 强制 CredentialMask；trace 回归 |
| H-10 | High | spill 目录/临时文件 symlink 或硬链接把自动 compaction 写向工作区外 | no-follow 目录链、独占随机临时文件、single-link regular 读取、内容与字节上限核对；spill 安全回归 |
| H-11 | High | container stdout 使用 communicate，无界主机内存；pool 无 idle 时无限创建容器 | 有界持续 drain、timeout/cancel 清理、live+starting 容量约束，失败清理不释放未知容量；`test_container_resources.py` |
| H-12 | High | todo replay 把被拒绝/尚未执行的 completed proposal 当成功状态，恢复可清空 finish gate | 按成功且相关联的 TOOL/ToolEvidence 事实恢复，严格 revision/metadata；`test_todo_recovery.py` |
| H-13 | High | EXPLICIT cache 在发送时改写已准入 payload，PG/trace/预算与实际 wire 不同 | marker 在 prepare 阶段冻结，专用格式严格验证，ModelBridge 透传 telemetry；prepared/cache/真实 HTTP 回归 |
| H-14 | High | LocalBackend 先 resolve 再以父路径读写，目录 swap→outside symlink 可重定向；同工作区 secret symlink alias 绕过 lexical deny | 全 component no-follow dirfd，regular/single-link 读取与哈希，parent-relative atomic write；真实目录交换及 alias 回归 |
| H-15 | High | ContainerBackend.search 接受绝对路径/`..`，可搜索镜像根目录；旧 RepositoryMap 父目录 race 读取外部 Python | container search 复用 safe bind-mount scanner；map 复用 safe walk/read；绝对/parent/alias 与 map race 回归 |
| D-01 | High | SDK eager Skills import 让 base/cloud consumer 隐式要求 yaml | Skills public exports lazy 导入；禁 yaml 的 fresh process 验证 base SDK/cloud contracts 可导入；实际 wheel 仍须完整 consumer 门禁 |
| R-01 | High | 新 release workflow 独立重建后可绕过完整候选门禁发布；手动 input 曾直接插入 shell | workflow 先 source+clean qualification，仅 stage 该次 SHA 核对的 wheel/sdist；inputs 通过 env；release-artifact 定向回归 |
| R-02 | Medium | 新增功能误放在 0.11 packaging note，并称 First published；Quickstart 误援旧 wheel 验证 | 新增内容归 Unreleased 0.12，去无证据发布/签收措辞；保留历史 0.11 原冻结证据 |

模型响应 body 上限及 release-artifact 输入文件 regular/no-follow/bounded 读属于根代理
另行确认的安全修复；最终关闭状态和回归结果读取本批最终 FINDINGS/门禁报告。本报告没有
用某次较早开发测试成功冒充这些后续改动已经完整复验。

## 定向验证与局限

本审查直接运行的最后一组文件/容器/Coding 回归为
`local-backend-regression.xml`：以其 tests/failures/errors/skipped 字段为准。
它包含 directory swap 读/哈希/写/遍历/搜索、secret alias、container scope、repository
map、搜索取消和既有 coding 消费者行为。LocalBackend、ContainerBackend、RepositoryMap
的 Ruff 和 strict mypy 定向检查通过。

技能安装/pipeline/optional import 定向回归也通过。真实 PG retirement 的 5 个回归证据为
`reports/saas-postgres/20261030T153620Z-retirement/summary.json`，临时服务已显式 cleanup。
早先失败开发运行保留；其中有一次因测试启动期间 state/store 接口切换导致 setup 失败，
不作为通过证据。没有连接用户云资源、没有付费模型调用、没有推送或发布发行物。

未在本子审查运行真实 Docker daemon、真实模型账户、长时间互联网负载或供应商账单对账。
容器命令与资源边界的定向测试使用可控 runner 和真实子进程；这不能认证生产 Docker
daemon/image。依赖漏洞库结果、完整覆盖率、框架比较、安装包与 clean-copy 结论必须
读取新冻结候选的完整验证，不重复继承历史 waiver 或历史通过数。

## 明确接受的剩余风险及后续边界

1. **Medium：技能升级硬崩溃窗口。** 两次目录 rename 之间崩溃可留下隐藏 backup 且没有
   active 包。旧内容保留，需管理员检查恢复；不声称跨崩溃 atomic upgrade。目标目录必须
   由宿主管理，flock 只协调遵守同一协议的 installer。
2. **Medium：主机文件操作不是 OS sandbox。** 任意 host command、特权同 UID 进程、挂载
   变化及恶意移动已打开目录，不由此库提供进程隔离。单链接检查也不能阻止复制秘密内容。
   不可信代码必须使用明确容器边界；真实部署另验。
3. **Medium：扫描与兼容性。** literal scanner 不读取 ignore 文件，按 Unicode 字符计算
   column，跳过 hidden/binary/超过 1MB 文件，oversize 标记 truncated。目录枚举虽可取消，
   超大单目录排序仍没有独立内存配额。宿主应限制工作区规模。
4. **Medium：恢复需要确定性 model preparation。** 同一 logical step 若自定义 adapter
   产生随机 nonce/timestamp 的新 prepared body，会因与原 PG 输入不一致而 fail closed。
   不能因此重新发送；这类 adapter 需明确的已存准备请求恢复契约。
5. **Medium：迁移运维。** schema 1→2 需要管理员停/drain 旧 worker 后升级。当前迁移与
   恢复证据是临时真实 PG 的事务测试，不是已生产数据的演练或零停机升级证明。
6. **Low：平台与供应链声明。** 文件锁、dirfd/no-follow 和容器运维实际为 POSIX 路径；
   metadata 中 OS Independent 不能当 Windows consumer 资格。GitHub Action 版本 tag
   和实际 PyPI environment/OIDC 设置仍由宿主仓库配置控制，本地测试不能证明其远端配置。

当前没有从本子审查继续开放的已确认 blocker/high 文件边界问题；以上不是对其它代理
尚在完成的补丁或最终候选门禁的提前放行。根代理必须待所有代码稳定后冻结、运行全部
quality/evaluation gate、修复新发现并从干净副本复验。

通用 SaaS 的身份/组织开通、正式 API/UI、资料生命周期、组织月预算与真实价格账单、
部署运维、备份恢复演练和真实云接入仍按总计划推进。当前内部 trusted TenantContext
不是客户登录认证；单位预留不是人民币 1000 元月总预算。即使本候选工程门禁全部通过，
也不能据此宣称用户的完整生产级 SaaS 已完成。

## R1 正式门禁补充：公有 SDK 架构边界

R1 完整 core 检查报告 1970 项中有两项失败。其一是本审查新增的
`coding/repository.py` 直接导入 `backends` 并使用私有目录/walk helper，违反既有
reference-application 仅使用 public SDK 的架构门禁。文件边界的定向回归通过没有覆盖
这项依赖方向要求；此处明确更正先前子审查的放行范围，不能把定向成功当作全门禁成功。
第二项为 sdist 显式 allowlist 断言未同步已声明的新 hosted demo，由根代理处理。
两项都阻断候选资格，不通过放宽 allowed modules 或跳过正式门禁解决。

根代理要求 R1 在收集完整 gate 时保持源码不变。因此本子任务只准备
`R1_ARCHITECTURE.patch`（apply_patch 格式，另附 unified diff），没有修改冻结源码。
补丁增加 public `LocalBackend.iter_files`，仅暴露 `FileEntry` 而不暴露目录描述符；
`list_files` 复用它；repository 仅从 public SDK 导入并通过 `closing` 消费生成器。
其 Python 文件数量上限仍只计 `.py`，不会误用一般 list 默认 200 项上限。

补丁已在 reports 下的隔离临时副本应用验证：112 项相关/架构测试通过，
Ruff/format/strict mypy 通过。该定向命令明确排除了尚由根代理修正的 sdist allowlist
断言，不能代替全量门禁。新增回归包括超过 200 项的非 Python 前置文件、提前关闭
生成器回收 held descriptor、public traversal 保持 no-follow/default skips。
`R1_ARCHITECTURE.json` 记录原文件 SHA、测试结果和 `source_unchanged=true`。
实际应用补丁后必须重新冻结并从完整源码/干净副本复验，R1 本身不获得资格。
