# M8-C-prep：校准与采样方法论（已冻结）

**状态**：规则已冻结，**具体阈值未定**——阈值留到 M10 完成后按本文件规则自动算出。
**冻结日期**：2026-09-17
**为什么先冻结规则**：#3 已证明仅把 tool error 从原始异常改成结构化 observation，就能让同一任务从 **4 步变成 11 步**。M10 还会加入 retry/backoff、deadline、journal，继续改变 steps / attempts / latency 分布。若现在按现有分布拍死 cap（如"最长 8，就设 6"），M10 后必然要改，而"边看结果边调阈值"会让最终 holdout 失去价值。

---

## 1. 两个 profile 的职责（已冻结）

| profile | 预算 | 回答的问题 |
| --- | --- | --- |
| **natural** | 宽（当前 steps=25、tool_calls=24） | 两种策略在"不容易撞预算"时，成功率 / Steps / Token / Latency 各是多少？ |
| **equal** | **单一主要紧约束** | 同样资源下谁做得更好？ |

**equal 只设一个主要紧约束**：`total_run_steps`。不同时勒紧 steps、tokens、wall-time——否则失败后无法归因究竟撞了哪个限制。

选 `total_run_steps` 的理由：它定义最成熟，且已明确把 Plan-Execute 的 planner 成本算进总成本（`run_case` 的 planner 偏移）。

## 2. cap 的计算规则（已冻结，数值待算）

1. 在 **dev natural profile** 上跑 ReAct + Plan-Execute；
2. **pooled** 汇总两 arm 的 `total_run_steps` 分布（含 planner）；
3. 取 **pooled P75 向上取整** 作为 cap；
4. **两个 arm 使用完全相同的 cap**。

**禁止**手写 cap（如"最长 8 所以设 6"）——那无法回答"这个阈值是不是为了让某个结果出现而挑的"。

### 生效判据（已冻结）

> equal-resource 运行中，至少 **10%** 的 run 受该 cap 约束（`steps >= cap`），否则报告标记 `constraint_effective=false`，**不得**据它做资源受限结论。

已实现为 `constraint_binding()` + `AgentEvaluationReport.constraint_effective` / `constrained_run_share`，非 equal profile 为 `None`（它们不声称约束）。

### 用当前数据验证规则自洽

判据是 **`steps >= cap`**（达到上限即视为受约束），与实现 `constraint_binding()` 及运行时语义一致：`while usage.steps < max_steps` 意味着步数等于 cap 的 run 可能正是被它截停的。

按 dev natural 分布（n=68，M10-B 后实测）测算：

| cap | 受约束 run | 占比 | 判定 |
| --- | --- | --- | --- |
| 2 | 59/68 | 86.8% | 生效 |
| 3 | 42/68 | 61.8% | 生效 |
| **4** | **20/68** | **29.4%** | **生效（P75 规则的结果）** |
| 5 | 8/68 | 11.8% | 生效 |
| 6 | 2/68 | 2.9% | 不生效 |
| 12（M8-B 时的手写值） | 0/68 | **0%** | **不生效** |

P75 规则产出 **cap=4**，满足 ≥10% 判据——规则与判据自洽，不是事后凑数。旧的手写 12 步上限被该判据正确否决。

> **修正记录**：本表早期版本用 `> cap` 计算，得到 cap=4 → 11.8%。那是**比较运算符与冻结规则文字不一致**，已按 `>= cap` 重算。规则文字（`steps >= cap`）与实现未变，只是测算口径统一。

### M10-B 后的重算结果（按冻结规则自动得出）

| 项 | 值 |
| --- | --- |
| pooled n | 68 |
| P75 | 4.00 |
| ceil → **cap** | **4** |
| 受约束比例 | 20/68 = **29.4%** |
| `constraint_effective` | **True** |

即 equal-resource profile 现在可以用 **cap=4** 运行，且该约束确实会绑定。

**注意**：M10-B 的 dev natural 分布与 M10-A 后**完全相同**（react 20/34 steps 2.24、plan_execute 18/34 steps 3.79），说明**本次基准运行没有触发 retry 路径**（未出现临时失败）。retry 的正确性由单元测试矩阵保证，不是由这次运行证明的。若将来 M10-C 改变分布，须再次按同一规则重算。

### 若 P75 算出来仍不生效（已冻结：不继续下调）

P75 是**预先冻结**的规则。若 M10 后的分布使 P75 产出的 cap 仍 <10% 生效（例如分布变得非常集中），处理方式冻结为：

> **不把规则改到 P70 / P60 去凑生效。直接标记 `constraint_effective=false`，并放弃 equal-resource 结论。**

理由：事后把分位数从 P75 调到 P70，本质上是"为了让结论出现而挑阈值"，与本规则存在的目的（防挑阈值）直接冲突。**可接受的结局是承认"当前任务集无法用步数做出有效的资源受限对比"**，而不是把规则改到能出结论为止。

此时报告应：

- 保留 `constraint_effective=false` 与实测 `constrained_run_share`；
- 在结论区显式写明"equal-resource 与 natural 在本任务集上不可区分"；
- 若确实需要资源受限对比，改用**更长/更难的任务**来拉开分布，而不是放宽分位数规则。

## 3. 采样规则（已冻结）

| 阶段 | 采样 | 用途 |
| --- | --- | --- |
| dev 调试 | 少量（当前 3） | 定位与诊断，**不作为最终数字** |
| **M8-C holdout 终评** | **每 case × 每 arm = 5 次真实运行** | 最终结论 |

最终报告**必须报告**：

- success rate；
- **每 case 的 5 次成功次数**（如 `Case A: ReAct 2/5, Plan-Execute 4/5`），而不只一个聚合比值；
- Steps / Token / Latency 的 **median**；
- **离散度**（IQR 或 min–max）；
- arm 间 **paired difference**。

理由已在实践中验证：单次 3-sample 跨运行可差 1 个 case（`long-serial-reads` 在不同 profile 与运行间 4 步 ↔ 11/12 步），不足以判断回归。5 次是个人项目里成本可接受的平衡点；不必做到 20 次并做重显著性检验。

## 4. 实验身份字段（已冻结并实现）

报告现在绑定：

| 字段 | 当前值 | 变更时机 |
| --- | --- | --- |
| `revision` | git HEAD | 每次运行 |
| `model_name` | 如 `Qwythos-9B-v2-8bit-mlx` | 换模型 |
| `model_parameters` | temperature / max_tokens / enable_thinking 等 | 改推理参数 |
| `observation_contract_version` | **1** | 模型可见的失败反馈形态变化 |
| `recovery_semantics_version` | **0** | retry / 恢复语义变化（**M10 提升**） |
| `benchmark_version` | **1** | 用例集变化 |
| `budget_profile_version` | **1** | 预算定义变化（重新校准 cap 时提升） |
| `samples_per_case` | 每次运行 | — |

**为什么必须有**：不能把"原始 Python exception 时代的 19/34"和"structured observation + retry/backoff 时代的 22/34"画在同一条"Agent 越来越强"的曲线上而不说明 Runtime 行为已变。这些字段让跨版本比较**在数据上就显示为跨版本**。

## 5. 执行顺序（已冻结）

```
现在：冻结本规则（规则已定，数值未定）        ✓ 已完成
  ↓
M10：改变 Runtime reliability semantics
  ↓
重新跑 dev natural，按 §2 规则自动算 equal cap
  ↓
冻结 Runtime / Prompt / Observation / Budget
  ↓
最后才打开 holdout，按 §3 用 5 samples/case
  ↓
终评后不再根据 holdout 调整任何东西
```

**holdout 在此之前不得运行**（除已完成的 1 次 keyless 探针验证外，未曾用于任何调参）。

## 5.1 终评中止规则（已冻结）

> **如果 holdout 运行期间发现的是评测基础设施 Bug（而不是模型质量问题），不要边看 case 边修然后继续跑。**

具体处理：

1. **立即中止**本次终评（不要为了"跑完再说"而继续）。
2. 修复基础设施 Bug。
3. **如果已经查看过 holdout 的逐题质量结果**，严格来说这套 holdout **已被消费**——应换一批/轮换题目再做 final，而不是在原题上重跑。
   - 若只看到聚合失败（如崩溃、schema 错误）而**未看到逐题质量结果**，可在修复后重跑同一 holdout；此时须在报告中注明这次中止。
4. 无论哪种情形，**修复必须在报告中留痕**：中止原因、修复的 commit、是否轮换 holdout。

这条规则的目的：避免最后一次评测又退化成 dev——即"看了结果再改，改完在同一批题上宣布通过"。

## 6. 残余限制

- P75 是任意但**预先冻结**的选择；它产出的是可辩护的规则，不是"正确"阈值。
- n=68 的分布来自单模型、单机、3 采样，M10 后必须重算。
- `constraint_effective` 只衡量"cap 是否真的绑定"，不衡量"这个 cap 是否合理"。
- 5 采样仍不足以支撑显著性主张；报告只描述观察到的差异与离散度。
