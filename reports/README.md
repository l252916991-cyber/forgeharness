# 报告索引

本目录保存每一轮实验、审计和候选验证的原始记录。报告写完后不再修改结论；后来的变化以注释或新报告的形式补充。
当前能力和边界以 [docs/CURRENT_CAPABILITIES.md](../docs/CURRENT_CAPABILITIES.md) 为准。

只有人能读的报告（`*.md`）和顶层摘要会提交；大部分运行产物（trace、状态库、候选副本）只保留在本地，
见 `.gitignore`。

## 当前证据（0.13–0.14）

| 报告 | 内容 |
| --- | --- |
| [deepseek-flash/REPORT.md](deepseek-flash/REPORT.md) | 真实模型基线、失败分类、改进前后对比与消融 |
| [collaboration-scaling/REPORT.md](collaboration-scaling/REPORT.md) | 固定专家协作与单 Agent 的规模对照（4–32 份材料） |
| [collaboration-ablation/REPORT.md](collaboration-ablation/REPORT.md) | 专家漏报的消融实验，含判定规则自身的疏漏 |
| [long-context-scaling/REPORT.md](long-context-scaling/REPORT.md) | 材料加到 42 万 token：一次调用变差，程序拆分就够（计划书见同目录 `PLAN.md`） |
| [saas/20261101-control-plane/REPORT.md](saas/20261101-control-plane/REPORT.md) | 0.13.0 控制面候选的工程资格、测试与覆盖率 |

实验脚本在 `benchmarks/experiments/`，题目生成器在 `evals/audit_tasks.py`。每份实验报告都写明了封存协议对应的提交；
按封存校验复现时先检出那个提交。

## 历史记录

以下按时间倒序，保留开发过程的原始上下文，内容不一定反映当前实现。

| 目录或文件 | 内容 |
| --- | --- |
| `saas/`（2026-10-28 至 10-31 各批次）、`saas-postgres/` | 托管执行、PostgreSQL 状态层和控制面的逐批执行与验证记录 |
| `application-20261028/` | 应用产品化 W0 + W1 执行记录 |
| `parallel-execution-20261027/` | 有界模型并行的实现、审查与候选证据 |
| `productization-20261027/` | 产品化执行状态 |
| `improvement-20261026/`、`multi-agent-20261026/` | 改进轮次与固定专家协作的自审 |
| `framework-comparison/`、`GITHUB_FRAMEWORK_COMPARISON_2026-10-16.md` | 与其他 Agent 框架的组合比较和来源 |
| `upstream-design-study-2026-10-16/`、`UPSTREAM_DESIGN_*.md` | 上游框架设计研究 |
| `request-preflight-2026-10-17/`、`REQUEST_PREFLIGHT_EXECUTION_STATUS_2026-10-17.md` | 请求预检的实现记录 |
| `release-0.8.3-r1/`、`next-candidate/`、`business-verification/` | 早期候选与业务验证产物 |
| 其余顶层 `*.md` | 早期阶段计划与状态快照 |
