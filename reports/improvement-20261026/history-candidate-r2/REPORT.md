# ForgeHarness 改进计划执行报告

日期：2026-10-26。结论：**0.8.4 本地工程候选通过，F1–F5 全计划尚未全部完成。**
正式真实模型对照、独立真人接入与个人无 AI 排障记录仍未完成。没有声称多 Agent 优势、
原生框架优势、真实商家/法律业务验收或生产发布。

工作区无 Git 元数据，使用已有 archive checkout-equivalent 冻结机制，没有补造提交。
最终源码身份：`b1ff8307efad740fd1d093905dce2683abc5dfd6f612d0977b3d608d39a839d1`。
实施与审查均为 Codex 自审；[发现分类与修复](AUDIT.md)记录首次失败及残余风险。

## 计划交付情况

| 工作包 | 已交付 | 尚未完成的边界 |
|---|---|---|
| F1 | [当前能力/兼容矩阵](../../docs/CURRENT_CAPABILITIES.md)、[两个宿主精确 wheel/API 清单](host-bindings-final-r2.json)；核对显式 import、SDK facade 与 wheel 内符号；更正历史 F1c 待办误读 | 反射/动态 import 不由静态扫描证明；长期模块级 API 兼容不作承诺 |
| F2 | [三组工具和协议草案](../../docs/STRATEGY_COMPARISON.md)、12 开发/30 评价合成诊断题、外部 JSON grader、共享预算、逐角色记账、聚类区间、失败分母和身份绑定；[36 次 contract 接线](contract-final-r2/summary.json)全部成功 | **没有可用真实模型基线；正式协议未封存，270 次未开始。** 当前短合成题也不能代替更广的真实开发/业务任务及适当人工评分 |
| F3 | [六类 SDK 故障各三轮](faults-final-r2/summary.json)、[两个真实宿主入口三轮隔离测试](hosts-final-r2/summary.json)、实际工单效果后进程退出与新进程对账、[恢复演示](recovery-demo-final-r2/summary.json) | 合成外部系统和测试租户；未逐类验收两个宿主的真实模型 HTTP/生产任务链路，无真实客户写入 |
| F4 | [注册只读工具的最小示例与迁移说明](../../docs/QUICKSTART_READONLY.md)、[新 wheel 最小安装下两进程自测](onboarding-selftest-r2/summary.json)、各宿主独立绑定回归 | 接入者待定；首次真人成功耗时未测，不能称独立接入验证 |
| F5 | [ADR 032](../../docs/adr/032-strategy-and-host-evidence.md)、[架构图/四主题时序图/三分钟演示/简历边界](../../docs/RECOVERY_INTERVIEW.md)、依赖修复、最终冻结与干净副本全门禁 | 个人无 AI 小缺陷定位待本人完成；原生完整框架故障对照继续未完成 |

本轮新增 19 个文件、修改 15 个冻结输入、没有删除原源码文件。运行时核心能力沿用原 R2，
本轮贡献集中于实验、演练、接入文档、证据治理和依赖安全；核心源码仅更新发行版本。
用 0.8.4 区分新构建，避免把不同 wheel 都称作 0.8.3。SDK API/state schema 没有变化，
但 R2 的完整身份绑定框架版本，旧运行仍须使用原 wheel，不能隐式交给新版本接管。

## 最终工程验证

权威结果：[candidate-r2 完整摘要](candidate-r2/verification/summary.json)、
[逐文件冻结清单](candidate-r2/source.json)、[完整门禁日志](candidate-r2-run.log)。

| 检查 | 实测结果 |
|---|---|
| 工程门禁 | **92/92 通过**；`qualified=true`、`source_unchanged=true` |
| 源码测试 | **1200 passed / 0 failed / 0 skipped** |
| 干净副本测试 | **1200 passed / 0 failed / 0 skipped** |
| 纯分支覆盖率 | 源码、干净副本均 **86.0332%**，门槛 85% |
| 依赖 | 核心、全 extras、两个独立比较环境均通过；原有 NLTK FH-019 单一例外未扩大 |
| 共同宿主框架组合 | 源码与干净副本各 **33/33**；原生完整故障比较仍 `false`，优势证据仍 `false` |
| 构建/安装 | wheel/sdist 内容与冻结身份一致，最小独立环境安装、API import 与 8 个示例通过 |
| 新 quickstart | 新 0.8.4 最小 wheel 下两个独立进程执行/重开成功；是作者自动化自测，非真人耗时 |

首轮 [candidate-r1](candidate-r1/verification/summary.json)为 87/92，通过部分不算资格。
失败包括源码/干净副本的全 extras 与 Pydantic 独立环境审计，以及随后修复造成的源码身份变化。
所有旧报告未改写。新公告修复详见 [依赖记录](../../docs/DEPENDENCY_REFRESH_2026-10-26.md)：
Brotli 1.2.0→1.2.1、setuptools 84.0.0→84.1.0、独立比较 Pydantic Harness
0.56.0→0.59.0（slim/graph 2.59.0）。只更新必要锁定项；版本协议在新运行前重封，
场景与判据没有变。两份业务宿主的 vendor、源码与 lock 未修改。

发行候选（未发布、未替换宿主依赖）：

- [forgeharness-0.8.4 wheel](candidate-r2/verification/runs/20261025T222317Z-c43b36b1/dist/forgeharness-0.8.4-py3-none-any.whl)，SHA-256 `0bb0184df0c7af53f81f04ef8bf0203b2684a2af5fca591a96901bf14bcae2b2`。
- [0.8.4 源码分发包](candidate-r2/verification/runs/20261025T222317Z-c43b36b1/dist/forgeharness-0.8.4.tar.gz)，SHA-256 `2b20caab1882a7a7a371f5afdb560962decb0735e3aea84206873e43195abd79`。

## 故障与宿主证据

SDK 六组各 3 个独立 pytest 进程，共 **18 轮 / 84 次参数化测试执行全部通过**。
模型超时/非法结构、工具前/检查点退出、外部提交后失确认、批准身份/撤权、分支/join、
取消/预算/访问权均有日志、trace、合成 SQLite 回执和文件摘要。
模型超时后的新进程保持 unknown 与预算预留，不自动重发。失败终态分支的结果被保留，
没有声称失败节点可以无条件自动重试。

宿主使用各自的已绑定版本，框架新版本不自动迁移业务应用：

| 宿主与入口 | 三轮实测 | 范围 |
|---|---|---|
| LexVault 0.8.3 exact wheel；隔离宿主/领域任务、实际 sidecar | **46×3=138 passed，无跳过** | 含 SIGKILL 边界、已提交回执对账、无回执保留 unknown、审批/取消/版本/领域引用恢复；没有生产 HTTP/UI 或真实法律质量结论 |
| ForgeCommerce 0.5.0 exact wheel；实际 HTTP ASGI | workflows 28×3 + identity 7×3 = **105 passed** | 本地合成测试租户、工单/审核/身份/撤权等；不是真实商家外部系统 |
| 新电商进程 drill | **3 次效果后退出 + 每次 2 个新进程恢复，全部通过** | 工单已落库、SDK 已成功而宿主投影未保存时 `os._exit(91)`；恢复和再确认均复用同一 ticket_id，每轮仍只有 1 张工单 |

合计 **243 项宿主测试执行通过**，加 9 次电商独立进程调用。宿主摘要中
`host_code_unchanged=true`，所有最终演练绑定与本轮最终源码相同的 identity。
测试数据/缓存/报告均在本工作区；没有改外部宿主源码、没有打开客户业务数据库。
第一次新 driver 错把 venv Python resolve 成基础解释器的失败完整保存在 `hosts-final/`，
修复与回归后 `hosts-final-r2/` 全量重新验证，不能混用两次结果。

重现演示：

```bash
uv run --no-sync python -m benchmarks.improvement_demo --output reports/recovery-demo-fresh
```

新目录中可看到：已提交后进程退出 → resume 保留 `needs_reconciliation:write` →
查询原回执后 succeeded → 再次 resume 不重复写，合成外部回执数始终 1。
这说明对账策略，不构成外部副作用 exactly-once 保证。

## F2 本地模型的实际阻碍

最初 [36 次开发尝试](live-dev-r1/summary.json)均收到 HTTP 507。
原 runner 的测量 true 标记已明确[分类作废](live-dev-r1-classification.json)，原始数据未改写；
不得把三组的 0 成功当作模型/策略能力测量。回归覆盖 507、preflight、synthetic 成本排除、
完整分母与重复样本。最终 contract 36/36 只证明接线，`strategy_measurement=false`、
`cost_metrics_valid=false`，没有把脚本 tokens 当成本。

[最终 preflight](live-preflight-final-r2/preflight.json)仍失败：现有 9B 模型权重
7,102,807,238 bytes，服务动态准入上限 6,044,213,290 bytes；早期上限曾仅为 1 byte。
没有修改全局模型服务设置或改用付费 API。正式模型版本/费用/耗时基线因此无法成立，
**正式协议未封存，正式 270 次运行未开始，没有策略胜负结论。**

后续先恢复本机模型可用内存，再按 [策略协议](../../docs/STRATEGY_COMPARISON.md)完成开发基线、
审查/封存阈值与样本数，最后采样。若以真实开发能力为求职证据，还需补实际开发/业务任务，
不能把当前微任务改名成仓库修复 benchmark。独立接入者和本人无 AI 记录由真人完成。

## 当前可用的简历表述

“设计并实现单机 Agent 运行时，提供持久状态、预算控制、审批内容绑定与工具 trace；
通过 1200 项自动化测试及干净副本验证，并在隔离环境完成六类故障各三轮演练，
以回执对账处理不确定执行结果；分别验证两个既有 SDK 宿主的恢复入口。”

必须随附范围：本地 POSIX/SQLite、合成隔离环境/测试租户、自审，非生产业务验收。
多 Agent 收益、原生框架优越性、独立接入体验、真实商家/法律质量、分布式 HA 和
外部 exactly-once 均不能作为已取得成绩。原计划还有这些条件相关工作，不能称全项目已完成。
