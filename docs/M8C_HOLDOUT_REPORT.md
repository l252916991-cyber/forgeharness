# M8-C 终评报告：holdout 集

**这一份是 ForgeHarness 的最终评测证据。** 跑之前已冻结实验身份（`reports/freeze-manifest.json`），跑完**不再调整任何东西**。

---

## 1. 实验身份（与冻结 manifest 逐项一致）

| 字段 | 值 |
| --- | --- |
| `git_revision` | `1be5f9219185978b9d9b981ede10f2ad57130881` |
| `model_name` | `Qwythos-9B-v2-8bit-mlx` |
| `model_parameters` | temp 0.0 / max_tokens 1024 / thinking off |
| `benchmark_version` | 1 |
| `observation_contract_version` | 1 |
| `recovery_semantics_version` | **2** |
| `budget_profile_version` | **2** |
| `holdout_case_ids_hash` | `9090918f…` |
| `samples_per_case` | **5** |
| `equal_step_cap` | **4**（按冻结规则自动得出，绑定 39.3%） |

两份报告均验证与 `reports/freeze-manifest.json` **完全匹配**（版本 / holdout hash / 采样数 / revision）。

**holdout 未被污染**：这是它第一次被运行。0 探针；14 条 live 可跑（`budget_boundary` 按 live 规则排除，属规则性排除而非事后剔除）。dev 上做过的所有调参都发生在 holdout 之外。

## 2. 逐 case 成功次数（natural，5 采样）

| case | ReAct | Plan-Execute | |
| --- | --- | --- | --- |
| `ambig-holdout-nested` | 0/5 | 0/5 | |
| `ambig-holdout-search-scope` | 5/5 | 5/5 | |
| `long-holdout-five-step` | 0/5 | 0/5 | |
| `long-holdout-four-step` | 5/5 | 0/5 | **ReAct +1.0** |
| `multi-holdout-list-search` | 5/5 | 5/5 | |
| `multi-holdout-read-write` | 5/5 | 5/5 | |
| `notool-holdout-a` | 5/5 | 5/5 | |
| `notool-holdout-b` | 5/5 | 0/5 | **ReAct +1.0** |
| `recover-holdout-error` | 5/5 | 5/5 | |
| `recover-holdout-timeout` | 5/5 | 5/5 | |
| `retr-holdout-search-read` | 5/5 | 5/5 | |
| `retr-holdout-search-write` | 0/5 | 5/5 | **Plan-Ex +1.0** |
| `single-echo-holdout` | 5/5 | 5/5 | |
| `single-read-holdout` | 5/5 | 5/5 | |

**配对结果**：ReAct 更高 2 例，Plan-Execute 更高 1 例，**11 例持平**。

> 关键读数：**14 例中 11 例两个编排完全一致**。差异集中在 3 个用例上，方向还不一致。

## 3. 汇总

| profile | ReAct | Plan-Execute |
| --- | --- | --- |
| natural（宽预算） | **11/14**（rate 0.786） | **10/14**（rate 0.714） |
| equal（共享 cap=4） | **11/14**（rate 0.786） | **10/14**（rate 0.714） |

`measurement_valid=true`、`contract_ok=true`、两 profile 均通过。

## 4. 成本（median [IQR]，natural）

| arm | steps | input tokens | output tokens | latency ms |
| --- | --- | --- | --- | --- |
| ReAct | **2.0** [1–3] | **1134** [707–1959] | **82** [34–108] | **8639** [4182–10716] |
| Plan-Execute | 4.0 [3–4] | 1906 [1212–2680] | 164 [96–206] | 13153 [9992–16952] |

**Plan-Execute 在几乎相同的成功率下，中位步数约为 ReAct 的 2 倍、input token 约 1.7 倍、延迟约 1.5 倍。** IQR 显示两个 arm 的分布几乎不重叠（steps：ReAct 上界 3 vs Plan-Execute 下界 3；latency 亦然）。

## 5. equal profile：共享 cap=4 改变了什么

`constraint_effective = **True**`，`constrained_run_share = **0.393**`（39.3% ≥ 10% 判据）。

共享上限实际改变的**只有 3 个 plan_execute 用例**，全部表现为"步数被压到 4 并落 `exhausted`"：

```
ambig-holdout-nested       plan_execute  passes 0/5 -> 0/5  steps 5 -> 4
long-holdout-five-step     plan_execute  passes 0/5 -> 0/5  steps 5 -> 4
long-holdout-four-step     plan_execute  passes 0/5 -> 0/5  steps 6 -> 4
```

**关键观察：这 3 例在 natural 下本来就全部失败（0/5）。** 所以共享 cap 在**成功率上没有净影响**——它没有让任何原本成功的用例失败，只是压缩了已经失败的用例的步数，并让失败原因从"内容不对"变成"预算耗尽"。

**这修正了 dev 上得出的印象。** dev 上 cap=4 曾让 2 个 Plan-Execute 用例从通过变失败（20/34 → 16/34），看起来像 cap 惩罚了 planner；holdout 上该现象**没有重现**——Plan-Execute 的失败全部由内容质量驱动，而非资源。

## 6. 可以下与不可以下的结论

**可以（有 holdout 证据支持）**

> 在这套自建 holdout 集（14 例、5 采样、单模型）上，ReAct 与 Plan-Execute 的成功率**实质相当**（11/14 vs 10/14；11 例完全持平），但 Plan-Execute 的中位步数约为 ReAct 的 2 倍、input token 约 1.7 倍、延迟约 1.5 倍，且分布几乎不重叠。

**不可以**

- 不能说谁"更好"——14 例中 3 例有差异且方向不一致，**不做显著性或普适主张**。
- 不能把 equal 的 0.393 约束绑定读成"planner tax 被证实惩罚了成功率"：holdout 上它只压缩了已失败用例的步数。
- 不能外推到其他模型：全部数据来自单个本地 9B 模型。
- 不能把 dev 上的 cap=4 结果（Plan-Execute 掉 4 个用例）当作结论：那发生在 dev 上，且 holdout 未重现。
- token / latency 数字**不折算金额**（本地推理无账单）。

## 7. 限制（必须随数字一起引用）

1. **单模型**：`Qwythos-9B-v2-8bit-mlx`（本机唯一可用模型；更大的模型超出 Metal 内存上限）。
2. **自建 holdout**：14 例是我们自己设计的控制集，不是公开基准，不代表真实任务分布。
3. **样本规模**：14 例 × 5 采样。5 采样足以观察"稳定通过 / 稳定失败 / 偶发"，不足以做显著性检验。
4. **本机运行**：本地 OMLX、单机、单进程；延迟数字包含本机模型服务开销，不是服务端性能指标。
5. **dev 与 holdout 都可跑 live**，但结论只引 holdout 数字（dev 已用于调参）。

## 8. 过程留痕

- 本轮**未触发中止规则**：未发现评测基础设施 Bug，运行完整跑完。
- 跑前修正了两处**非行为**项（不改 Runtime）：deadline 改称 soft/admission；版本号 `recovery 1→2`、`budget 1→2`，使 M10-A/B/C 的行为变化可从报告身份区分。详见 [M8C_FREEZE_RECORD.md](M8C_FREEZE_RECORD.md)。
- 已知记录缺陷（不追溯）：M10-B/C 的 dev 回归报告标 `recovery_semantics_version=1`，因版本升级发生在它们之后；这些历史文件刻意不重写。
