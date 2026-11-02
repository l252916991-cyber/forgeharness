# 多 agent 协作执行报告

2026-10-26。SDK 0.8.6 / API v1。**本轮最小版本 M0–M5 已完成，独立候选 95/95
工程门禁通过。完整多 agent 后续路线尚未完成，真实模型收益未评测。**

候选源码身份：`caf801dedfdc6f1b30128b65be28b7938d24274fe651dfd5768fdce724844e29`。
工作区没有 Git 元数据，使用 archive checkout-equivalent 逐文件冻结，共 456 个文件。
权威证据：[冻结清单](candidate-r1/source.json)、
[验证摘要](candidate-r1/verification/summary.json)、[完整执行日志](candidate-r1-run.log)。

计划：[EXECUTION_PLAN_MULTI_AGENT](../../docs/archive/EXECUTION_PLAN_MULTI_AGENT.md)。
接入：[COLLABORATION](../../docs/guides/COLLABORATION.md)。
设计：[ADR 034](../../docs/adr/034-fixed-specialist-collaboration.md)。
审查与风险：[AUDIT](AUDIT.md)。

## 执行范围

| 批次 | 结果 |
|---|---|
| M0 | 计划、ADR 与模型释放完成 |
| M1 | 专家/任务/证据/报告契约与固定 R2 图完成；相同专家的不同任务独立标识 |
| M2 | 普通结算与对账共同校验 output schema；非法输出失败并保留实际消耗 |
| M3 | 协作入口、必需宿主 guard、任务树、终验和三进程示例完成 |
| M4 | 新增 55 项回归；发现分级与修复见 AUDIT.md，无未处理 blocker/high |
| M5 | 新版本、新冻结、源码/干净副本、依赖、对照组合与最小安装门禁通过 |

## 最终候选验证

| 检查 | 结果 |
|---|---|
| 工程门禁 | **95/95**；`qualified=true`、`source_unchanged=true` |
| 源码测试 | **1262 passed**，0 failures / errors / skipped |
| 干净副本测试 | **1262 passed**，0 failures / errors / skipped |
| 纯分支覆盖 | 两者均 **86.3381%**，门槛 85% |
| 依赖/格式/静态/类型 | 全部通过；核心、全 extras、两个比较环境重新审计；沿用原 NLTK FH-019 单一例外，无新增豁免 |
| R2 故障与共同宿主组合 | 源码和干净副本均通过；各 33 项框架组合通过，不推导框架核心优势 |
| 发行物 | wheel/sdist 成员和源码摘要匹配冻结；最小 wheel 安装、import、9 个示例通过 |
| 恢复示例 | 新进程查询已有合成回执，继续协调者；再启动进程恢复终态，未重复专家调用 |

未发布的候选发行物：

- wheel（`reports/multi-agent-20261026/candidate-r1/verification/runs/20261026T112014Z-8e4c5d29/dist/forgeharness-0.8.6-py3-none-any.whl`），
  SHA-256 `e071fecdee4c8f6de2a5c88073ff8c5fe90282db8c8106cf9d605cbba183875c`。
- sdist（`reports/multi-agent-20261026/candidate-r1/verification/runs/20261026T112014Z-8e4c5d29/dist/forgeharness-0.8.6.tar.gz`），
  SHA-256 `a1cfa85716da53335ff87c6da67b83b4d7fb0fc6b541594b02dc2e87d02b2ff8`。

第一轮开发源码运行 1255 tests 通过，纯分支覆盖 86.2586%；该轮发生在最后的 guard、
schema 边界加固与版本/打包调整前，只是开发诊断。最终候选必须独立重跑。

## 模型资源

已按用户授权停止本地 oMLX PID 94073（SIGTERM），后续核实该进程退出且 TCP 8000
没有监听。本轮不启动本地模型。所有工程测试使用 scripted responses 与合成回执，
无需真实推理、网络模型调用或付费模型凭据；依赖审计的网络访问不涉及推理。
完整工程验收后再次检查，未发现 oMLX / MLX-LM / Ollama / llama-server 模型进程，
TCP 8000 继续无监听。没有估算或宣称释放了某个具体 GiB 数值。

## 能力边界

交付单机、固定拓扑、串行、只读、每专家一次决策的协作；任务树里的 delegation 是
R2 持久节点，不是独立可恢复的自主 child run。共用宿主注册模型、预算、权限与 trace。
引用校验只验证来源身份和结构，不评估语义支持。

后续批次包括有界并发、自主子运行、隔离写入和真实任务收益评测，均未交付。
既有 270 次 A/B/C 实验保留原结论；新工程测试不能证明多 agent 的一般质量或成本收益。
没有升级 LexVault / ForgeCommerce 宿主，没有接管旧 wheel 的活动 R2 状态。

实施与审查为本次 Codex 自审。完整多 agent 路线与整个项目均不因最小版本完成而宣称结束。
冻结后的源码未再修改；最终结果与审查补记写在 reports 下，不改变候选源码集合。
