# M8-C：终评冻结记录

**冻结时刻**：2026-09-17，revision `1be5f9219185978b9d9b981ede10f2ad57130881`
**冻结 manifest**：`reports/freeze-manifest.json`（由 `forge freeze-manifest` 从**代码常量**生成，非手写，因此不会与代码漂移）

本文件记录**冻结了什么**、**为什么可以冻结**、以及**冻结后不许再改什么**。

---

## 1. 冻结 manifest 的内容

| 字段 | 值 | 含义 |
| --- | --- | --- |
| `git_revision` | `1be5f92…` | 哪一版代码 |
| `model_name` | `Qwythos-9B-v2-8bit-mlx` | 哪个模型 |
| `model_parameters` | temp 0.0 / max_tokens 1024 / thinking off | 哪套推理参数 |
| `benchmark_version` | 1 | 哪套用例 |
| `observation_contract_version` | 1 | 模型看到的失败反馈形态 |
| `recovery_semantics_version` | **2** | Runtime 的恢复/重试/超时语义 |
| `budget_profile_version` | **2** | 预算 profile 定义（equal cap=4） |
| `holdout_case_ids_hash` | `9090918f…` | **哪一套 holdout** |
| `holdout_cases` | 16（live 可跑 14） | 留出集规模 |
| `samples_per_case` | 5 | 采样数 |
| `natural_budget` | steps 24 / calls 24 / tokens 200k/40k | 自然成本预算 |
| `equal_step_cap` | **4** | 等资源主约束（按冻结规则自动得出） |

`holdout_case_ids_hash` 是**把报告绑到具体 holdout 集**的字段：只 hash 排序后的 holdout id，所以即使 dev 用例被编辑、holdout 未动，holdout 报告仍能匹配同一冻结 manifest。

## 2. 版本号升级记录（本轮收尾）

冻结前核查发现一个真实缺口：**M10-A/B/C 三份报告全部标 `recovery_semantics_version=1`**，行为变化无法从报告身份区分。已升级：

| 字段 | 旧 | 新 | 理由 |
| --- | --- | --- | --- |
| `recovery_semantics_version` | 1 | **2** | 1 只代表 journal（M10-A）；2 = journal + retry/attempts（M10-B）+ soft deadline（M10-C） |
| `budget_profile_version` | 1 | **2** | 1 对应**从未生效**的手写 cap=12；2 对应按冻结规则得出的 cap=4 |

生成代际已写进代码注释（`domain/models.py`）。

**已知记录缺陷（不追溯修改）**：M10-B、M10-C 的 dev 回归报告是在版本号升级**之前**跑的，因此它们标 `1`，尽管实际执行的是 retry/deadline 语义。这些是历史文件，**刻意不重写**——重写会让证据与当时的代码不符。只有**从本次冻结起**产生的报告携带 `2`。

## 3. deadline 命名修正

M10-C 严格来说**不是硬 wall-clock deadline**。实现是"步间检查、不抢占进行中的工具"，所以一个 run 可能超出 `max_wall_seconds` 最多一个工具自身的 timeout。

已全面改称 **soft run deadline / admission deadline**，并在代码注释、STATUS、M10-C 文档三处写明：

> deadline **stops new work but does not preempt in-flight side effects**。

**不得**声称"任务绝不会运行超过 N 秒"。这个超额是"绝不留下未知副作用状态"的代价，是有意接受的。

## 4. 冻结范围（用户确认）

自冻结起**不再改动**：

- Runtime 行为（loop、journal、retry、deadline）
- Prompt 与 planner 协议
- parser 宽容度
- observation format
- `ToolSpec`（含 effect class 与 timeout）
- retry/backoff 规则
- evaluator scoring

holdout 后**不得**因结果不佳回头调整上述任何一项。需要继续开发就开新版本 + **新的 holdout**。

## 5. cap=4 的依据（不再是手写值）

按 `M8C_CALIBRATION_RULES.md` 的冻结规则，从 M10-C 后 dev-natural 分布自动得出：

| 项 | 值 |
| --- | --- |
| pooled n | 68 |
| P75 | 4.00 |
| ceil → cap | **4** |
| 受约束比例 | 20/68 = **29.4%** ≥ 10% |
| `constraint_effective` | True |

**不得**因为 holdout 上 Plan-Execute 掉得多或少而改成 5。规则冻结先于结果。

## 6. 终评执行配置

- split：`holdout`（16 条，0 探针；`budget_boundary` 按 live 规则排除 → **14 条可跑**）
- 采样：**5 次/case/arm**
- profile：**natural 与 equal 都跑**，同一模型参数、同一 revision、同一 benchmark version、同采样数
- 一次性由脚本连续生成两个报告（`/tmp/holdout_final.sh`），**中间不允许手工调整配置**
- 报告：`reports/agent-eval-holdout-{natural,equal}-omlx.json`

## 7. 中止规则（已冻结，见 M8C §5.1）

> 若发现的是**评测基础设施 Bug**而非模型质量问题，**立即中止**本次终评；修复后，若已查看过 holdout 逐题质量结果，该 holdout 视为已消费，应轮换题目再做 final。

并在报告中留痕：中止原因、修复 commit、是否轮换 holdout。
