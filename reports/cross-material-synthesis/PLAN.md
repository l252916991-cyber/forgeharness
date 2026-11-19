# 计划书：需要跨材料综合时，协作值不值得

日期：2026-11-19。状态：**已实现、未运行**。脚本、题目和判定规则都已写定并有单元测试；
dev 运行、封存和正式集运行需要 DeepSeek 密钥，由项目作者执行。结果出来后写在同目录 `REPORT.md`，本计划不再修改。

## 1. 背景

前三轮实验（[规模](../collaboration-scaling/REPORT.md)、[消融](../collaboration-ablation/REPORT.md)、
[长上下文](../long-context-scaling/REPORT.md)）用的都是互相独立的材料，答案是每份材料判断结果的并集：

- 材料在约 5 万 token 以内时，一次调用就够；
- 材料多到一次调用读不准时，拆分能恢复准确率，但简单的程序拆分就够，专家协作只增加成本和延迟。

三份报告都写明了没有覆盖的情况：**答案需要把多份材料结合起来的任务**，例如「依赖了违规服务的服务」。
SDK 文档里「协作留给需要跨材料综合的任务」至今只是假设。本实验就是这个情况。

## 2. 要回答的问题

1. 在需要跨材料综合的任务上，程序拆分还够不够？
2. 如果不够，固定专家协作能不能补上？还是「模型抽取 + 程序合并」就够了？
3. 专家报告改成逐份判定（[ADR 051](../../docs/adr/051-per-material-assessments.md)，schema 2）有没有用？

## 3. 任务（`evals/synthesis_tasks.py`）

- 材料沿用审计任务的服务配置和 R1–R3 三条规则，另加 **R4 依赖风险**：一个服务本身不违反 R1–R3，
  但它当前调用（`calls_upstream`）的某个服务违反了，就判为 at-risk；本身违规的判为 violation，其余为 compliant。
- 原材料里与规则无关的 `dependencies` 行（服务名）换成 `calls_upstream`（服务 ID），保证「调用」只有一个出处。
- 约 30% 的服务在 changelog 里有一条「已移除对某违规服务的同步调用」，作为近似干扰项：历史不算当前调用。
- 规模 N = 16、32；分组固定为每 8 份一组。种子：dev 6601（每个规模 1 题），正式集 7702（每个规模 6 题）。
- 正式集共 288 个判断，其中 at-risk 61 个；**39 个 at-risk 服务的违规上游全部在别的组**（hidden），
  只看本组材料不可能判对。
- 单元测试确认：标签能从材料独立推出；题目与前三轮材料不重复；没有材料会被凭据脱敏改写；
  题目哈希固定（`tests/unit/test_experiments_synthesis.py`）。

这个生成器单独成文件，不修改 `evals/audit_tasks.py`，前三轮封存协议里的文件哈希不受影响。

## 4. 五个方案（`benchmarks/experiments/synthesis.py`）

| 方案 | 做法 | 每次运行的模型调用 |
|---|---|---|
| oneshot | 读完全部 N 份，一次调用，输出 violations 和 at_risk | 1 |
| split-naive | 每 8 份一组，每组就是 oneshot，答案合并。看不到跨组调用：**操纵检验** | N/8 |
| split-extract | 每 8 份一组，模型只报告本组的 R1–R3 违规和每个服务的 `calls_upstream`，由程序按 R4 合并。最强的程序基线 | N/8 |
| team-s1 | `Collaboration`：每个专家 8 份 + coordinator，报告 schema 1；专家按指令每个服务写一条 finding | N/8 + 1 |
| team-s2 | 同一团队，schema 2：每个服务一条结构化判定，先判定后结论，由 schema 强制 | N/8 + 1 |

所有方案同一模型、同一规则文本，单次输出上限 4096 token，温度 0，每题每方案一次。
team-s1 的 coordinator 只列违规和 at-risk 服务（schema 1 最多 30 条结论；正式集每题最多 19 个）。

## 5. 指标与判定规则（封存前写定）

**评分**

- **主要指标：标签错误数**：预测标签（violation / at-risk / compliant）与正确标签不同的服务数，按题累加。
- 次要指标：整题全对；hidden at-risk 服务的漏判数；成本和延迟只做描述。
- 任何单元没有可解析的答案，就视为它什么都没判：其中的服务都算 compliant，并单独报告失败单元。

**效果规则（[`benchmarks/experiments/rules.py`](../../benchmarks/experiments/rules.py)）**

消融实验的教训是：判定门槛不能用固定题数，要按基线自己的错误数来定。本实验所有比较都用同一条规则：

- 基线错误数 ≥ 5，且候选方案的错误数不超过基线的 50% → **better**；
- 角色互换后满足同样条件 → **worse**；
- 两者错误数都 < 5 → **insufficient_errors**（数据不足以判断方向，不等于「没有差别」）；
- 其余 → **no_clear_difference**。

**预先登记的比较（按全部题目合并判定，也分别报告 N = 16 和 32）**

| 比较 | 基线 → 候选 |
|---|---|
| 操纵检验：任务确实需要综合 | split-naive → oneshot 为 better |
| schema | team-s1 → team-s2 |
| 框架 vs 程序 | split-extract → team-s2 |
| 框架 vs 一次调用 | oneshot → team-s2 |
| 程序 vs 一次调用 | oneshot → split-extract |

**结论规则**：只有 team-s2 同时 better 于 split-extract 和 oneshot，才判定「在这个任务上协作值得」。
操纵检验不通过时，照实报告各项比较，但结论注明「这组任务没有迫使模型做跨材料综合」。

**测量有效性**：数据完整；重试后没有基础设施错误；trace 全部通过校验；用量全部已知；运行期间源码未变。
基础设施规则与前三轮相同：429/5xx 或无响应的单元从新状态重跑，最多 3 次；401/402 立即停止整轮。

## 6. 执行步骤

```bash
read -s "FORGE_API_KEY?DeepSeek key: " && export FORGE_API_KEY
# 1. dev（2 题 × 5 方案），只检查管线是否健康，不看结果调参
uv run --no-sync python -m benchmarks.experiments.synthesis --split dev --mode live \
  --output reports/cross-material-synthesis/dev
# 2. 封存协议：脚本、共享代码、规则、题目和协作模块的哈希，加上判定规则
uv run --no-sync python -m benchmarks.experiments.synthesis --seal \
  --dev-report reports/cross-material-synthesis/dev \
  --protocol reports/cross-material-synthesis/protocol.json
# 3. 正式集（12 题 × 5 方案 = 60 次运行），只跑一次
uv run --no-sync python -m benchmarks.experiments.synthesis --split evaluation --mode live \
  --protocol reports/cross-material-synthesis/protocol.json \
  --output reports/cross-material-synthesis/evaluation
```

不需要密钥的接线检查：`--mode contract` 用一个只回答「本单元能看到什么」的 oracle 跑完全流程。
正式集的 contract 运行结果是：split-naive 恰好漏掉 39 个 hidden at-risk 服务，其余方案 0 错。
这只说明接线正确，不是测量。

**预算估计**：N ≤ 32 时单次输入约 1.3 万 token；60 次正式运行、约 300 次调用，按公开价格估计不到 1 美元。
上一轮正式集因为余额用完作废过一次，开跑前请确认余额。

## 7. 局限（预先写明）

- 一个模型、一种合成任务；每个规模 6 题、每题一次。
- 只有一跳依赖，分组固定为 8；没有测其他分组或多跳传递。
- team-s1 的专家指令要求每个服务一条 finding，和 schema 2 的区别只在「有没有结构和强制」，
  不是和前几轮的 team 配置比较。
