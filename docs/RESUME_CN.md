# 简历与面试表述（中文）

## 项目名称

ForgeHarness：面向软件工程任务的可评测 Agent Harness

## 一句话介绍

从零实现不依赖 LangGraph 等编排框架的 Agent Harness，将模型调用、Agent Loop、工具执行、权限审批、上下文、状态、记忆、MCP、Sub-agent 和评测统一到可验证的运行时中，并落地代码仓库缺陷修复场景。

## 可直接用于简历的项目要点

- 基于 Python 3.12 自研模型无关的 Agent Harness，支持 ReAct 与 Plan-Execute 两种 Loop，以确定性状态机统一管理成功、失败、预算耗尽及等待审批等状态，并通过 step/tool/token 硬预算约束失控循环。
- 设计类型化 Tool Registry 与 Dispatcher，统一 Pydantic/JSON Schema 参数校验、风险分级、超时和输出限制；实现工作区路径约束、无 Shell 的固定测试命令、环境变量白名单及基于文件 SHA-256 的乐观并发写入。
- 实现与 task + tool + canonical arguments 精确绑定、带过期时间且仅可消费一次的审批机制，以及基于 SQLite Compare-And-Swap 的版本化 Checkpoint，避免审批漂移和旧状态覆盖。
- 构建面向代码修复的垂直 Agent：生成 AST 仓库地图，完成文件检索、带行号读取、审批后原子修改、测试执行与 Git Diff 检查；使用真实临时 Git 仓库完成无密钥端到端回归测试。
- 接入版本化 Skills、符合 2026-07-28 规范语义的 stateless HTTP MCP 工具，以及缩减工具权限的 Sub-agent；子任务的 steps、tool calls 与 tokens 全量计入父任务预算。
- 实现审核后才可检索的长期 Memory 和敏感字段递归脱敏、fsync 落盘、SHA-256 链式校验的 JSONL Trace；构建 7 类无密钥控制评测并输出带 manifest hash 与 Git revision 的机器可读报告。
- 建立 ruff、strict mypy、pytest、分支覆盖率门禁及 GitHub Actions CI；最终数字必须以当前审查版本的 `make review` 和评测报告为准。

## 与 Agent Harness 岗位的对应关系

| 岗位关键词 | 项目证据 |
| --- | --- |
| Agent Loop / Planning | ReAct runtime、Plan-Execute runtime、明确终止状态与预算 |
| Tool Use / Harness Engineering | Registry、Schema、Policy、Dispatcher、Approval、Timeout |
| Context Engineering | Token budget、相关性选择、AST repository map、压缩与 provenance |
| Memory / State | 审核型长期 Memory、SQLite revision checkpoint |
| MCP / Skills | stateless HTTP MCP adapter、标准 `SKILL.md` 加载与能力校验 |
| Sub-agent / Multi-Agent | 缩减权限、父预算记账、子 Trace 回传；不夸大效果 |
| Agent 产品判断力 | CLI 审批交互、API 检查面、可重现评测、清晰的已实现边界 |

## 面试演示顺序

1. 用 `forge demo` 说明模型决策与工具执行是两个边界。
2. 展示 Runtime 状态机、Tool Policy 和 Approval fingerprint。
3. 运行代码修复集成测试，强调修改前暂停、审批精确绑定、恢复后测试和 diff 证据。
4. 篡改一条 JSONL Trace，运行校验并展示 hash mismatch。
5. 运行 `forge eval-control`，解释为什么控制评测与真实模型评测必须分开报告。
6. 主动说明 OS sandbox、SWE-bench 和 multi-agent 增益仍未实现/验证，体现工程边界意识。

## 不应写入简历的表述

- “SWE-bench 达到 X%”：当前没有固定数据集和真实模型报告。
- “Multi-Agent 显著提升成功率”：当前只有权限和预算机制，没有对照实验。
- “安全执行任意命令”：当前仅避免 Shell 拼接并过滤环境，不是 OS 级隔离。
- “完整支持 MCP”：当前只支持 stateless HTTP JSON 响应子集。
