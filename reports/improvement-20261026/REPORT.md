# ForgeHarness 改进计划执行报告

2026-10-26。**0.8.5 工程候选通过，正式 270 次已完成并核验，F1–F5 全计划仍未全部完成。**
独立真人接入、个人无 AI 排障、更多真实开发/业务任务与人工质量评审仍待完成。
实施与审查均为本聊天 Codex 自审，不是独立专业审计或生产发布。

当前源码身份：`828b20be6e7a2ba0dda9122ad4bfe310c0a64744857aaf4ec7b0914c0c4f1c46`。
工作区没有 Git 元数据，使用 archive checkout-equivalent 逐文件冻结，不补造提交。
[当前候选](candidate-r3/source.json)、[审计与发现](AUDIT.md)。
旧 0.8.4 报告保存在 [历史阶段](history-candidate-r2/REPORT.md)，所有旧候选/失败记录保留。

## 当前工程证据

权威摘要：[candidate-r3](candidate-r3/verification/summary.json)；[完整日志](candidate-r3-run.log)。

| 检查 | 本次结果 |
|---|---|
| 工程门禁 | **92/92 通过**，qualified / source_unchanged 均 true |
| 源码测试 | **1207 passed**，0 失败 / 错误 / 跳过 |
| 干净副本测试 | **1207 passed**，0 失败 / 错误 / 跳过 |
| 纯分支覆盖 | 源码 86.0421%；干净副本 86.0739%，门槛 85% |
| 依赖与类型 | 核心、全 extras、两个独立比较环境审计通过；原 NLTK FH-019 单一例外未扩大；静态与类型检查通过 |
| 共同宿主框架组合 | 源码与干净副本各 33/33；原生完整故障比较与框架优势均未证明 |
| 构建与安装 | wheel/sdist、最小环境/API import/8 个示例、文档 quickstart 两进程测试通过 |

候选发行物未发布、未替换业务宿主依赖：

- [forgeharness-0.8.5-py3-none-any.whl](candidate-r3/verification/runs/20261026T093827Z-1d23a686/dist/forgeharness-0.8.5-py3-none-any.whl)，SHA-256 `a1a98496dc9afcc727dac94d6e988fe06ffdc66c92b5a8558c88f5c1e4aca2af`。

- [forgeharness-0.8.5.tar.gz](candidate-r3/verification/runs/20261026T093827Z-1d23a686/dist/forgeharness-0.8.5.tar.gz)，SHA-256 `4dd005c9a6bc25cb98391f30adc072dcb583445a23823e0264defbfe7f713440`。

本次新增 `RunKernel.call_model(include_tools=False)`：在规范模型请求准备之前省略本次
工具目录，registry、权限、预算、trace 与共享计费路径保持原边界。默认行为未改变。
固定综合节点和多角色的 planner/worker/reviewer 使用此选项，避免向仅接收最终文本的
阶段暴露不能执行的自动工具调用。见 [ADR 033](../../docs/adr/033-model-tool-catalog-visibility.md)。
7 项新回归覆盖真实 adapter payload、trace、并发/用量、非法 flag、拒写与三角色接线；
35 项针对性测试通过后，重新冻结并从干净副本完成全套门禁。题集/评分/预算没有因结果改变。
旧 R2 运行仍使用原 wheel，框架版本属于运行身份；宿主不隐式升级。

## 本地模型与实验

模型为 **Qwen3.8-27B-oQ4e-mtp**，上游 `Jundot/Qwen3.8-27B-oQ4e-mtp`，
revision `b47cb89d5da40c67a334403330a826c7591fbc09`。
17 文件 / 16,994,900,596 bytes，权重与 tokenizer 对照固定 revision 的 SHA-256，
其他文件对照 Git blob；额外记录全部文件（含 chat template）的 SHA-256。
[下载核验](model-download-qwen38-27b/verified-install.json)、
[运行时锁定](qwen38-27b/runtime-lock.json)、[正式前核验](qwen38-27b/before-formal.json)。

M4 Pro / 48 GiB；oMLX 0.7.0，MLX 0.32.2，mlx-lm 0.31.4.dev132+g94cdcae13；
本地 127.0.0.1:8000，温度 0、thinking 关闭、MTP 未启用、每次最多 256 输出 tokens。
没有付费 API；token 指标是服务报告的用量，不是人民币/电费/设备折旧测算。
保留缓存、轮换组顺序；桌面其他负载未隔离，不作普适时延/SLO 声明。

A 为单 Agent ReAct 自行读取材料；B 为确定性读取全部材料后一次综合；C 为
planner 安排材料顺序、确定性读取、worker 综合、reviewer 复核，三角色全记账。
三组使用相同注册工具、权限、资料、外部 JSON 精确 grader 和总预算。
A 暴露工具目录，B/C 文本节点省略目录，这是明确披露的编排差异，非相同 prompts。
kernel 调用串行；C 不进行自由任务拆解，不能证明并行加速或一般多 Agent 收益。

开发集 12 题：A **9/12**、B **7/12**、C **7/12**，36 次完整、用量已知、无基础设施错误。
[开发摘要](qwen38-27b/dev-r2/summary.json)、[正式前判据审核](qwen38-27b/pre-formal-review.json)、
[已封存协议](qwen38-27b/protocol-r1.json)。开发后未调整阈值、题目、评分或预算。
正式 30 题（parallel/chain/error 各 10）× 3 组 × 每题 3 次 = **270 次**；
重复样本按任务聚类，不当作 90 个独立业务任务。

正式完整结果：[原始汇总](qwen38-27b/evaluation-r1/summary.json)、
[独立重算/逐次 trace 核验/失败样本](qwen38-27b/analysis.json)、
[正式后模型与运行时核验](qwen38-27b/after-formal.json)。

| 组 | 成功 | 成功率 | 任务聚类 95% 区间 | 全样本 p50 / p95 秒 | 每成功任务 tokens（包含失败消耗） |
|---|---|---|---|---|---|
| A 单 Agent | 42/90 | 46.7% | [30.0%, 63.3%] | 29.31 / 37.72 | 9001.29 |
| B 固定流程 | 36/90 | 40.0% | [23.3%, 56.7%] | 1.53 / 2.06 | 290.33 |
| C 多角色 | 39/90 | 43.3% | [26.7%, 60.0%] | 3.60 / 6.17 | 811.77 |

270 个预期唯一样本与全部 trace 哈希均核验通过，统计独立重算一致。
正式逐任务累计耗时约 **45.04 分钟**，基础设施错误 0，用量缺失 0。
源码/权重未变；17 文件、服务 PID/版本/设置的正式后核验通过，成本字段完整有效。
人工介入率指运行中没有人帮助解题，不能代表人的首次接入体验。

C 相对 A 的配对成功率差 95% 区间：**[-23.3, +16.7] 个百分点**；
相对 B：**[0.0, +10.0] 个百分点**。
封存规则的“双基线质量提升”：**未通过**；
每成功任务 tokens 不超两基线各自 1.20 倍：**未通过**。
各任务族延迟例外详见 analysis.json；仅各 10 题，无多重比较校正，均为探索性分析。
三个任务族均未通过延迟例外。C 的每成功任务 tokens 是 B 的 **2.80 倍**。
结果不支持把多角色作为通用默认方案。具体 A/B 选择须按质量目标和资源约束判断。

失败原因另见 [描述性分类](qwen38-27b/failure-review.json)：A 的 48 次失败均为工具
预算耗尽；B 的 54 次失败中，30 次规范字符串不匹配、18 次无效 JSON、6 次答案类型
不匹配；C 的 51 次分别为 30、15、6。字符串不匹配项未独立作语义重评分。
reviewer 带来的 3 个通过样本属于同一个任务的三次重复：移除 `retrieve` 答案的
Markdown 代码围栏，答案语义没有变化；见 [原始修正](qwen38-27b/reviewer-corrections.json)。
这不能作为“纠正了三种推理错误”的证据。

精确字符串评分对命名变体敏感：例如 eval-p05 的路径形式可能与规范答案语义一致，
仍按封存规则判失败。本轮成功率是规范答案精确匹配率，不等同独立人工语义质量。
后续题面/评分改进需新协议，不追改本轮主指标。

这些是作者自建的短合成诊断题，作者构建时见过全部答案；不是未见仓库修复或真实
业务任务，没有独立人工质量评分。即便用真实模型跑完，也不代表 F2 更广范围已验收。

历史失败均保留：9B HTTP 507 批次属于基础设施诊断（旧 true 标志已另行分类作废）；
后续 9B 开发 A4/B2/C0 亦非正式终评；27B [dev-r1](qwen38-27b/dev-r1-classification.json)
在发现工具目录接线问题后中止，不能与修复后结果混合。contract oracle 只证明接线，
synthetic token 不进入成本，不把失败或未知用量填零。

## 既有故障、宿主与接入证据

以下三轮新增演练绑定较早的 **0.8.4 / candidate-r2** 源码
`b1ff8307efad740fd1d093905dce2683abc5dfd6f612d0977b3d608d39a839d1`。
不能将它们改称 0.8.5 的三轮演练；新候选另有完整工程门禁中的 R2 故障回归。

| 证据 | 范围与结果 |
|---|---|
| [SDK 六类故障](faults-final-r2/summary.json) | 各 3 个独立 pytest 进程，18 轮 / 84 次测试执行全通过；合成回执、审批/取消/预算/分支/未知效果 |
| [两个宿主测试](hosts-final-r2/summary.json) | LexVault 0.8.3 exact wheel：46×3=138；ForgeCommerce 0.5.0 exact wheel：(28+7)×3=105；共 243 次通过 |
| 电商真实进程退出 | 3 次工单落库后 os._exit(91)，每次 2 个新进程恢复，复用同一 ticket_id，每轮仍 1 张工单；本地合成测试租户 |
| [只读 Quickstart 自测](onboarding-selftest-r2/summary.json) | 0.8.4 最小 wheel 两进程执行与重开；0.8.5 文档对应代码另由本次 source/clean 测试执行；非独立真人耗时 |
| [恢复演示](recovery-demo-final-r2/summary.json) | 查询原回执后对账，重复 resume 不重新写；非外部 exactly-once 保证 |

两宿主源码、vendor、lock 未修改；测试数据/缓存/报告在本工作区，未打开客户数据库。
宿主采用各自原 wheel，当前 SDK 工程通过不代表宿主已升级或真实业务验收。
静态导入/wheel 清单见 [绑定索引](host-bindings-final-r2.json)。

## F1–F5 交付与剩余

- F1：能力矩阵、精确宿主 wheel/API、历史文档状态纠正已交付；反射导入与长期模块兼容另验。
- F2：三组运行器、完整分母/用量/trace、开发与正式封存协议已交付；广泛真实开发任务、独立材料与人工质量评审仍缺。
- F3：六类故障三轮、两个既有宿主入口和工单进程对账演练已交付；真实模型生产 HTTP/UI 与外部客户系统未验收。
- F4：注册只读工具的指南与可执行自测已交付；独立接入者待定，真人首次成功耗时未测。
- F5：ADR 032/033、架构/时序图、恢复演示和简历边界、冻结全门禁已交付；本人无 AI 小缺陷定位仍需本人完成。

接受的残余边界：单机 POSIX/SQLite、本地 owner fencing、可信回执、未知成本保留、
取消不回滚已提交效果、失败分支不自动重试、路径限制不是 OS sandbox、outbox 至少一次。
原生完整框架故障比较、框架核心优势、分布式 HA、独立安全审计、法律/商家专业质量与
生产 SLO 不能从本轮自动化结果推出。不得称全项目或全部求职改进计划已完成。
