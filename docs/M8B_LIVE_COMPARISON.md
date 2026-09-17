# M8-B 实测报告：ReAct vs Plan-Execute

**报告产物**：`reports/agent-eval-dev-equal-omlx.json`、`reports/agent-eval-dev-natural-omlx.json`
**revision**：`1be5f9219185978b9d9b981ede10f2ad57130881`
**模型**：`Qwythos-9B-v2-8bit-mlx`（本地 OMLX，唯一可用模型；35B 超出 Metal 内存上限）
**数据集**：`evals/agent_cases.json` 的 **dev split**（holdout 未使用，见文末）
**用例**：34 条 live-eligible（45 条 dev 减去 7 条 scorer probes 与 7 条 `budget_boundary` 中的 dev 部分）
**采样**：每用例 3 次，`judged_pass` 要求**3/3 全通过**

---

## 1. 核心结果

两个 profile 的**数值完全相同**（68/68 用例逐条一致），因为等资源预算从未生效（见 §3）。

| profile | arm | pass | pass rate | 平均步数 | 平均 input tokens | 平均 output tokens | 契约 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| equal | ReAct | 19/34 | 0.559 | **2.12** | **1317** | 91 | ✓ |
| equal | Plan-Execute | 19/34 | 0.559 | 3.26 | 1668 | 157 | ✓ |
| natural | ReAct | 19/34 | 0.559 | 2.12 | 1317 | 91 | ✓ |
| natural | Plan-Execute | 19/34 | 0.559 | 3.29 | 1705 | 156 | ✓ |

**结论：在这套任务与这个模型上，两种编排的成功率没有差别（19/34 vs 19/34），但 Plan-Execute 的代价明显更高——多约 1.1 步、多约 27% input token。**

这个结论只在以下范围内成立：单个 9B 本地模型、34 条任务、3 次采样。**不构成**对两种编排的一般性判断。

## 2. 分类别差异（真实 trade-off）

成功率持平，但**分布不同**——这是比总分更有价值的部分：

| 类别 | ReAct | Plan-Execute | 解读 |
| --- | --- | --- | --- |
| `long_horizon` | **0/5** | **2/5** | Plan-Execute 在长任务上占优，符合预期 |
| `retrieval_then_tool` | **2/4** | 1/4 | ReAct 在检索后接工具时更稳 |
| `single_tool` | 5/6 | 4/6 | 近似持平 |
| `no_tool` | 4/4 | 4/4 | 两者都不会乱调工具 |
| `tool_failure_recovery` | 4/5 | 4/5 | 持平 |
| `ambiguous_args` | 2/4 | 2/4 | 持平 |
| `multi_tool_serial` | 2/6 | 2/6 | 均偏弱，共同瓶颈 |

这与"Plan-Execute 长任务成功率更高，但平均 Token / latency 较高"的预期方向一致。**注意样本量**：`long_horizon` 只有 5 条，2/5 vs 0/5 的差异**不足以**做显著性主张，只能作为趋势记录。

## 3. 重要方法论发现：等资源预算没有生效

`EQUAL_TOTAL_RUN_STEPS = 12`，但实测**最大步数只有 7**（equal）/ 8（natural），**没有任何用例因预算耗尽而终止**（`exhausted_cases = 0`）。也就是说：

> 等资源 profile 与自然成本 profile 在这批任务上测的是同一件事。

这是设计而非缺陷（12 步是为防止无限循环设的宽松上限），但它意味着**当前两个 profile 无法区分**。要让等资源约束真正生效，需要把上限收紧到实测分布内（例如 5–6 步），或改用更长的任务。这应作为 M8-B 的后续项，而不是靠重新解释数字掩盖。

## 4. 共同失败模式（harness 无关，是模型能力）

两个 arm 在 12 条用例上**同样失败**，失败原因一致，指向模型能力而非编排：

- `missing_tools`（最常见）：模型只调用部分所需工具就作答。例如 `long-search-read-write-verify` 需要 search→read→write 三步，两个 arm 都只做了 1 步。
- `arguments:accuracy`：路径参数写错（如把 `app.py` 读成别的文件）、行号窗口不符。
- `sequence`：`recover-second-attempt-fails` 要求重复调用 3 次，两 arm 都只调 1 次。

**这正是 M9-B 的 Bad Case 素材来源**（见 §6）。

## 5. 接线过程中发现并修复的 4 个真实缺陷

这些是 M8-B 的实际产出，全部有回归测试（`tests/unit/test_plan_execute.py`、`test_agent_evaluation.py`）：

1. **loopback 请求被系统代理拦截（502）**：`OpenAICompatibleModel` 缺 `trust_env=False`，而仓库内其他本地客户端都有。现按 host 判断。
2. **planner 响应解析过严**：模型会返回 ` ```json ` 围栏、尾部游离反引号、JSON 前后夹带说明文字。内容正确却因格式判失败，会让 Plan-Execute 因标记格式失分。现用 `raw_decode` 提取首个 JSON 对象后严格校验结构。
   **反向确认**：另有两类输出是**真实模型错误**（只回显 XML 标签、JSON 内多一个游离 `)`），仍被正确拒绝——这是质量发现，不该容错。
3. **步数断言对 Plan-Execute 不公平**：用例 `max_steps` 描述 executor 阶段，未偏移 planner 会让 `max_steps=1` 的 no-tool 用例对 Plan-Execute **结构性不可通过**。现所有 profile 都偏移；等资源压力改由共享预算表达。
4. **契约检查把模型质量当接线故障**：原先检查 `plan_events == 1`（计划是否**有效**），live planner 答非所问属**模型质量**问题，却会把整条 arm 判为契约损坏。现改用 `planner_requests`（planner 是否被**调用**），并让 planner 异常也计一步。
   同源修正：`budget_boundary` 用例在 live 下结构性不可满足（断言 per-case 预算覆盖下的 `EXHAUSTED`，而 live 故意共享预算），现 live profile 排除该类与 scorer probes。

## 6. 对 M9-B 的直接输入

§4 的共同失败已具备 Bad Case 沉淀价值。最优先的三条：

1. `long-search-read-write-verify` —— 模型只执行部分计划步骤就收尾（**两 arm 同因失败**）。
2. `recover-second-attempt-fails` —— 单次调用失败后未按预期重试（当前无 retry，M10 会改）。
3. `retr-find-then-read`（仅 Plan-Execute 失败）—— 以 **natural profile** 为例为 8 步（该 profile 的最大值）；**equal profile** 下同一用例为 7 步（同样是该 profile 的最大值）。两次都出现 4 次 `search_code`（1 次符合预期 + 3 次重复），即 **loop 无进展**，是编排侧特有的失败模式。

> **口径提醒**：步数一律指 `usage.steps`。上表两处数字不同仅因 profile 不同（equal=7 / natural=8），引用时必须带 profile。Bad Case schema 的 `profile` 字段就是为了消除这类歧义。

第 3 条尤其值得固化：它是 Plan-Execute 独有的、可从 trace 步记录直接定位的循环行为。

## 7. 已知限制（不得越界引用）

- **holdout 未使用**。两个报告都是 dev split；holdout 保留给最终比较，且**未参与任何调参**。
- **单模型、单机**。结论绑定 `Qwythos-9B-v2-8bit-mlx`，不可外推到其他模型或生产环境。
- **样本量小**。34 条用例、3 次采样只够趋势判断，不足以支撑显著性主张。
- **等资源与自然成本当前无差异**（§3），不能引用为"两种资源配置下结论一致"。
- **本地 OMLX 无 API 账单**，只报 token 与墙钟，**不折算金额**。
- 运行资源受限：不限制单请求 `max_tokens` 会让 24GB 机器整机冻结，本阶段已发生一次；`--limit` 分块运行会被标记 `complete_split: false`，不可作为测量引用。
