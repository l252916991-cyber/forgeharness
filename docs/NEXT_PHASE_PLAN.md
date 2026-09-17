# ForgeHarness 下一阶段计划书

**主题：从"实现了两种编排"升级为"可评测、可诊断、可安全恢复的 Agent Runtime"**

延续 [`DELIVERY_PLAN.md`](DELIVERY_PLAN.md) 的 M0–M7（M7 审计已通过）。本计划对应 **M8–M10，M11 为可选**。

> 修订记录 v2：修正 M10 的幂等语义（crash window 与 invocation journal）、M9 的 Replay 分层、M8 的 dev/holdout 划分与等资源/自然成本双视角、指标口径增加 logical/attempt 与序列维度。
> 修订记录 v3：M8 拆为 M8-A/B/C（A 已完成）；**benchmark cases 与 scorer probes 彻底分开报告**；**holdout 移出 CI**（`make gate` 只跑 dev）；M9 拆为 M9-A（投影，提前到 M8-B 之前）/ M9-B（Bad Case + 分层 Replay）；keyless 统一 evaluator **同时驱动两种 runtime** 并校验接线契约；合成 token 降级为 `pipeline_diagnostics` 且 `cost_metrics_valid: false`。
> 修订记录 v4：M9-A 已完成；**冻结 `logical_call_id` / `attempt_id` / `step_id` 三层标识**（为 M10 retry 预留，届时一决策对多尝试）；**`executor_step_budget` 与 `total_run_steps` 命名分离**，planner 步数授予明确为 adapter contract 而非等资源定义；**`qualified` 拆为 `measurement_valid`（数据是否可用）+ `keyless_gate_passed`（keyless 专用，live 为 `null`）**，避免"模型只成功 2/16 却显示 qualified"的误读。

---

## 0. 这份计划要回答的问题

第二个项目当前的技术点已经不少（双编排、工具校验、审批、Checkpoint、上下文装配、混合检索），但叙事停在"能力建设"：**能跑，但没有量化过"什么时候该用哪种编排"，也没有系统回答"Agent 为什么失败"。**

本阶段只补三件事：

1. **M8 Agent Benchmark** —— ReAct 与 Plan-Execute 在同一数据集上的成功率/成本差异，用真实跑出来的数字说话。
2. **M9 Trace 步级投影 + Bad Case + 分层 Replay** —— 失败任务可定位、可复现、可回归；且**分清"验证 Runtime 修复"与"验证模型行为改善"是两件事**。
3. **M10 Runtime Reliability** —— 让"任务能恢复"升级为"任务能**安全**恢复"。

**明确不做**：继续加 MCP / 多 Agent / Memory / 浏览器这类彼此松散的新功能。当前不缺功能面，缺的是深度和一个完整的"问题—实验—结论"闭环。

---

## 1. 现状核对：先分清"已有"和"要建"

建议稿里有几处把已有能力当成了新增工作。这一节是写作和排期的事实基线（依据 `docs/STATUS.md` 与源码，非计划）。

### 1.1 已经存在且可直接复用

| 能力 | 现状 | 证据 |
| --- | --- | --- |
| 双编排 | `AgentRuntime`（ReAct 式）与 `PlanExecuteRuntime`（一次受限规划 + 受控执行循环）均已实现并测试 | `runtime/loop.py`、`runtime/plan_execute.py`、`tests/unit/test_plan_execute.py` |
| 结构化 Trace | 已有哈希链 JSONL，事件类型覆盖 `run.started` / `model.request` / `model.action`（含 action、model_name、input/output tokens）/ `policy.decided` / `tool.completed`（含 ok、elapsed_ms、observation、metadata）/ `tool.unknown` / `context.compacted` / `verification.*` / `plan.created` | `observability/hash_chain.py`、`runtime/loop.py` |
| 预算治理 | steps / tool_calls / input_tokens / output_tokens / **max_context_tokens** 全部硬约束，越界落到 `EXHAUSTED` | `runtime/budget.py` |
| 上下文与成本控制 | 单请求装配、确定性历史压缩、observation 压缩、工具输出截断、工具子集裁剪 | `context/assembly.py`、`context/compression.py`、`tools/dispatcher.py`（ADR 005） |
| 超时与失败隔离 | 单工具 30s `asyncio.timeout`，异常转为结构化 observation，不泄漏进循环 | `tools/dispatcher.py` |
| 恢复与幂等雏形 | 乐观 CAS Checkpoint；**审批恢复路径在任何副作用之前先抢占 revision**，天然拒绝重复执行 | `state/checkpoint.py`、`runtime/loop.py:resume_approved` |
| 评测脚手架 | 报告型评测（FrozenModel → `reports/*.json` 含 `qualified` 阈值）、keyless 确定性 control 套件（`ScriptedModel` + manifest） | `evaluation/rag.py`、`evaluation/control.py`、`evals/control_cases.json`、`make gate` |
| 检索压测数字 | 5,000 chunks / 10 并发 / 100 请求，p95 = **358.33 ms**（目标 500 ms，qualified=true，绑定 revision `9617025a`） | `reports/retrieval-benchmark.json` ✓ 已核实 |

### 1.2 相对建议稿的三处校准

- **Trace 不是从零建**。步级字段（工具名、参数、结果、tokens、耗时、ok）**已经落在 trace 里**，缺的是：把事件流**投影**成步记录、补模型调用耗时（可由相邻事件 `timestamp` 求差，不必新增埋点）、把失败任务抽成 Bad Case、以及分层 Replay 通道。工作量比建议稿描述的小得多。
- **Context / Cost Governance 机制基本已在**（装配 + 压缩 + 截断 + 预算）。真正缺的是**对比实验与数据**，不是机制。因此建议稿的"第四优先级"降级为 **M11 可选**。
- **Circuit breaker 建议不做**。单机本地、单进程、无远程依赖池的场景，熔断器没有可熔断对象，属于 YAGNI。有价值的是错误分类、重试退避、分级超时、执行 deadline、以及下面的**调用日志（invocation journal）**。

---

## 2. 目标叙事

完成后这个项目能讲出一条完整链路：

> **Agent Runtime → Agent Evaluation → Bad Case 定位 → Runtime 优化 → 回归验证**

对比第一个项目（LexVault）形成互补：

| LexVault | ForgeHarness |
| --- | --- |
| RAG Evaluation | Agent Evaluation |
| Recall / MRR | Task Success Rate / Tool Set Accuracy |
| Rerank | ReAct vs Plan-Execute |
| Retrieval Bad Case | Execution Bad Case |
| 引用可信度 | Runtime Reliability（调用日志 / 超时 / 安全恢复） |

---

## 3. 里程碑

### M8 — Agent Benchmark：ReAct vs Plan-Execute

> **v3 修订：M8 拆成三段。** M8-A 已完成；M8-B/C 依赖 M9-A 的步级投影。执行顺序见文末附录。

#### M8-A — Schema + Dataset + Evaluator ✓ 已完成

**交付物（已落地）**

- `evals/agent_cases.json` —— 61 条固定用例（8 类全覆盖），带 `split: "dev" | "holdout"`；dev 45（其中 38 条 benchmark + 7 条探针）/ holdout 16。
- `src/forgeharness/evaluation/agent.py` —— schema、硬断言打分器、连续指标、报告模型。
- **统一 evaluator 同时驱动 ReAct 与 Plan-Execute**，并校验两个 arm 的接线契约（不得只跑 ReAct）。
- 报告字段把 **benchmark cases** 与 **scorer probes** 彻底分开，禁止出现 `52/61` 这类混合比率。
- keyless 报告标注 `token_source: "synthetic"` 与 `cost_metrics_valid: false`；合成 token 只出现在 `pipeline_diagnostics`，不作为 headline 成本指标。
- CLI：`forge eval-agent --split dev|holdout`。

**关键设计决定（已生效）**

- **探针只放 dev**：holdout 不含探针，避免负例占用留出名额。
- **步数语义显式化**：Plan-Execute 每个用例恰好比 ReAct 多 1 步（planner），报告可见；`_executor_budget` 授予 planner 步数，`judge_case` 同步偏移断言，否则 `max_steps=1` 会让两 arm 不可比。
- **三种调用计数**：`model_decisions`（trace 中的模型决策，含被预算拒绝的那次）、`logical_tool_calls`（`usage.tool_calls`，通过预算闸门被受理的调用，即 `max_tool_calls` 的口径）、`tool_attempts`（实际执行）。三者不得混用。

**已发现的真实问题（统一 evaluator 的价值）**

接线时立刻暴露两处：① 我最初假设 `usage.tool_calls == trace 中的模型决策数`，实际两者在预算边界必然不等；② `max_steps=1` 下 ReAct 能执行工具而 Plan-Execute 不能。两者都已按上述方式修正，并各有回归测试。

#### M8-B — 真实模型对照（接线完成，完整报告待产出）

**已落地**

- adapter：`forge eval-agent --profile equal|natural --model <name>` 走与 keyless 完全相同的 evaluator / projection / 打分契约，只有模型来源不同。
- 两个 profile 命名与语义分离（§ 下方命名约定），报告记录每个 arm 实际拿到的预算（`budgets`），使两种 profile 事后可区分。
- 重复采样为强制项（live 至少 2 次）；`judged_pass` 要求**每个样本都通过**，`pass_rate` 保留部分通过率，数值指标取中位数。
- `report_valid` / `measurement_valid` 与模型质量解耦；`keyless_gate_passed` 在 live 为 `null`。
- `--limit N` 支持分块运行；截断运行标记 `complete_split: false`，**不得**作为测量引用。

**接线过程中发现并修复的四个真实缺陷**

1. **`OpenAICompatibleModel` 的 loopback 请求被系统代理拦截（502）**：`OMLXClient` 等本地客户端都有 `trust_env=False`，它没有。现按 host 判断：loopback 不走代理，远程保留。
2. **`PlanExecuteRuntime` 解析 planner 响应过严**：实测模型会返回三种噪声形式——` ```json ` 围栏、**尾部一个游离反引号**（`{"steps":[...]}` `` ` ``）、以及 JSON 前后夹带说明文字。内容完全正确却因格式被判失败，会让 Plan-Execute 因**标记格式**而非编排能力失分，直接污染对照。现用 `json.JSONDecoder().raw_decode` 提取第一个 JSON 对象，然后**严格校验其结构**：空计划、形状不符、截断、无 JSON 均照旧拒绝。
   注意区分：实测另有两类 planner 输出是**真实模型错误**，仍被正确拒绝——一类只回显 XML 标签（`<start_marker>…`）而完全没给 JSON，另一类 JSON 内部多一个游离 `)`（`…"])}`）。这两类是质量发现，不应容错。
3. **步数断言对 Plan-Execute 不公平**：用例的 `max_steps` 描述的是 executor 阶段，但 planner 决策未偏移，导致 `max_steps=1` 的 no-tool 用例对 Plan-Execute **无论行为如何都不可能通过**。现在所有 profile 都偏移 planner 决策；等资源的压力改由共享预算表达（截断运行自己会落入 `EXHAUSTED` 并失败状态断言）。
4. **契约检查把模型质量当成接线故障**：原先检查 `plan_events == 1`（计划是否**有效**），而 live planner 答非所问属于**模型质量**失败。这会把整条 arm 的契约判为损坏，既冤枉 harness，又掩盖真实的失败信号。现改用 `planner_requests`（planner 是否被**调用**），并让 planner 抛异常时也计入一步——否则步数统计会随模型质量而变。
   同源问题：`budget_boundary` 用例在 live profile 下**结构性不可满足**（它们断言 per-case 预算覆盖下的 `EXHAUSTED`，而 live 故意共享同一预算以保持等资源），会造成恒失败噪声。现 live profile 排除 `budget_boundary` 与 scorer probes 两类用例。

**实测结果（M8-B 完成）**

完整分析与限制见 [`M8B_LIVE_COMPARISON.md`](M8B_LIVE_COMPARISON.md)。摘要：

| arm | pass | 平均步数 | 平均 input token |
| --- | --- | --- | --- |
| ReAct | 19/34 | 2.12 | 1317 |
| Plan-Execute | 19/34 | 3.26 | 1668 |

- **成功率持平**（各 19/34），但 Plan-Execute 多约 1.1 步、多约 27% input token。
- 分类别出现**反向差异**：`long_horizon` ReAct 0/5 vs Plan-Execute 2/5；`retrieval_then_tool` 2/4 vs 1/4。样本仅 5 条与 4 条，**只作趋势记录**。
- **等资源与自然成本两个 profile 结果完全相同**（68/68 逐条一致），因为 12 步上限从未生效（最长 7 步，无预算耗尽用例）。当前两个 profile **无法区分**；需把上限收紧到实测分布内或改用更长任务。

**未完成项**

- **holdout 尚未 live 运行**，未参与任何调参，未授权任何 holdout 结论。
- 等资源约束收紧（见上）。
- 单模型、34 条用例、3 次采样：不做显著性主张。

**命名约定（防止混淆）**：`executor_step_budget` 是 executor 循环可花步数，其 `+1` 是对 planning 的结构性补偿（adapter contract）；`total_run_steps` 才是等资源比较要共享的量。所有 profile 的**断言**都偏移 planner 决策（因为断言描述的是执行阶段），而**预算**是否偏移则区分两种 profile。

#### M8-C — 重复评测与留出集终评

- dev 上迭代；holdout **只在最终比较时跑一次**。
- 每用例多次采样，报中位数与离散度；不做显著性主张。
- 报告：`reports/agent-eval-holdout-omlx.json`。

**退出标准**

- M8-A：`make gate` 加入 dev keyless 版本且稳定通过（已完成）。
- M8-B：两个 arm 在同一数据集、同一预算口径下产出可比较报告，且 planner 成本可见。
- M8-C：holdout 报告绑定 revision 与采样次数，并声明未参与调参。
- 全部报告均不含"混合通过率"字段；策略结论只出现在 omlx 报告。

**预估**：A 已完成；B 2–3 天（含 adapter 与双 profile）；C 1–2 天（含重复采样脚本）。

---

### M9 — Trace 步级投影 + Bad Case + 分层 Replay

> **v3 修订：拆成 M9-A / M9-B，并提到 M8-B 之前。** M8-B/C 真跑「60 条 × 两策略 × 多次采样」后，没有步级投影就只能翻原始 JSONL，因此投影必须先做。

#### M9-A — Trace → Step Projection ✓ 已完成

**交付物（已落地）**

- `src/forgeharness/observability/steps.py` —— 把 trace 事件投影成步记录（不新增持久化层）。
- `forge trace-steps <task_id>` —— 人读执行轨迹，`--json` 输出完整投影。
- 与 `evaluation/agent.py` 的 `ObservedRun` **共用同一份投影逻辑**（evaluator 已改为调用 `project_events`），不存在第二套 JSONL 解析。

**冻结的三层标识（为 M10 retry 预留）**

| 字段 | 含义 | 当前 | M10 后 |
| --- | --- | --- | --- |
| `logical_call_id` | 模型一次工具调用**决策**，跨 retry 稳定 | 来自 trace `call_id` | 不变 |
| `attempt_id` | 一次实际执行尝试，形如 `<call_id>#<n>` | 一决策对一尝试 | 一决策对多尝试 |
| `step_id` | 展示层序号，**禁止用于 join** | — | 不变 |

**实测发现的三个真实问题（均已修复）**

1. **evaluator 变更曾把 `tool_sequence` 语义改错**：投影初期用"已执行"序列，导致 `budget-holdout-no-calls` 从通过翻成失败。已拆成 `tool_sequence`（决策序，含被预算拒绝者）与 `executed_sequence`（实际执行序），并加回归测试。
2. **`policy.decided` 对所有决策都触发**，不能直接当拒绝信号；改为只在 `decision == "deny"` 时闭合该调用。
3. **审批挂起≠未闭合**：`awaiting_approval` 现在是独立状态，不计入 `incomplete_calls`。

**其他关键设计**

- **两种延迟严格分开**：`model_latency_ms_derived`（由 request→action 事件时间差推导，命名即声明非 SDK 原生）与 `tool_latency_ms`（dispatcher 实测）。
- **`budget_snapshot`**：记录每步之后剩余的 steps / tool_calls / input_tokens / output_tokens，使耗尽可直接诊断（例：planner 花掉 1 step → steps 归零 → 第三次 model decision 被拒）。
- **未闭合不静默成功**：有开启无完成的调用投影为 `incomplete`，`tool_attempts` 不计入。
- **确定性**：同一事件序列两次投影 byte-equivalent；哈希链校验失败的 trace 直接拒绝投影。
- 为支撑上述字段，trace 事件新增 `call_id`（policy / tool / approval 事件）、`budget` 余量快照、`model.request` 的 `phase` 标签；planner 现在也记录自己的 request 事件与用量，使 planner 成本可归因。

**退出标准（已满足）**

- 任一次运行都能凭 `task_id` 输出完整步记录，且哈希链校验通过。
- 评测报告与 `trace-steps` 对同一 run 得出一致的 steps / 工具序列 / attempts（有专门测试断言两者相等）。

**验证**：`tests/unit/test_trace_steps.py`（27 个测试，覆盖你指定的 6 类验收场景 + 健壮性分支），投影器分支覆盖 40/40。

#### M9-B — Bad Case 捕获与诊断（M9-B1 ✓ 已完成；Replay 与修复未做）

**本阶段范围（用户明确限定）**：只做**捕获 + 诊断**。不实现 Replay，不改 Runtime 行为。目的先取得"修复前红"的证据，否则后面没有真正的 red → green。

**已落地**

- `src/forgeharness/evaluation/bad_case.py`：`BadCase` schema（绑定 `revision / profile / arm / model / sample_id / trace_hash`）、failure 分类、replay script 冻结（**仅作为候选声明**）、no-progress 与 fixture-gap 诊断。
- 评测器在失败时把**首个失败 sample** 的原始 trace 落盘为哈希链 JSONL，并写 Bad Case 记录。产物根目录取自 `output_path.parent`（不是仓库根）——否则测试会污染工作区（此 bug 已发生并修复，有回归测试）。
- artifact identity 冻结为 `{profile}-{arm}-{case_id}-s{sample_id}`：同一 case 在 equal/natural 下行为可不同，多次采样也可能只有某次失败，缺任一项都会**静默覆盖证据**。
- 诊断派生字段：`repeated_call_signature / repeat_count / observation_hashes`，供 M9-B3 修复后用数字验收，而不是肉眼读 trace。
- 定向重跑：`--only-cases <ids>`（并标记 `complete_split: false`）。

**no-progress 的严格判据（重要）**：必须同时满足相同 tool_name + 相同 canonical args + **相同 observation**，才判为空转。仅"调用多次"或"参数相同但结果不同"都不算。这条判据已防止一个错误归类：`retr-find-then-read` 看起来像重复空转（5 次调用、3 次 `search_code`），但实测 query 各不相同（`retry policy` / `retry` / `max_retries`），**不是空转**，被正确归为 `model_quality`。

**已捕获的三条 Bad Case（对应三个层，这是选材的目的）**

| 层 | artifact | failure_class / point | 证据要点 |
| --- | --- | --- | --- |
| **协议层** | `equal-react-retr-two-searches-s0` | `protocol_violation` / `model_adapter` | 模型一次返回 2 个 tool call（`search_code(helper)` + `search_code(run)`），适配器抛异常 → 整个 run `steps=0` 失败。未 admission、未 dispatch。`error_type=multiple_tool_calls_not_allowed` |
| **Planner 层** | `natural-plan_execute-multi-echo-then-read-s0` | `harness_protocol` / `planner` | planner 只回显 XML 标签（`<start_marker>…`），完全没给 JSON；run 在 executor 行动前就结束 |
| **Planner 层** | `natural-plan_execute-multi-search-then-read-s0` | `harness_protocol` / `planner` | planner 返回的 JSON 内部多一个游离 `)`（`…"])}`），解析失败 |
| **执行循环层** | `natural-plan_execute-retr-find-then-read-s0` | `model_quality` / `grading` | 前两步已满足契约，之后超额调用 3 次（5 > max 2）。**不是** no-progress（query 各异） |

三条分别落在**协议层 → Planner 层 → 执行循环层**，而不是三条同类的"工具没调出来"。另捕获 5 条 `model_quality` 作为对照（两 arm 同因失败）。

**第一条的 observability gap（记录，不修）**：`usage.steps=0` 但投影层 `model_requests=1`。模型推理确实发生，步数口径未反映。

**第二条的两个附加发现**（`harness_gap` 与 `case_design_note` 分别记录，不并入主分类）：
- 同一 tool error 重复 2 次，以**原始异常字符串**回给模型（`FileNotFoundError`），不是可行动反馈。
- fixture 的 README 写着 "retry policy lives in runtime."，但 fixture **没有** `runtime/` 目录——模型去找它并非幻觉，是跟着 fixture 文本走。

**no-progress 的严格判据（重要）**：必须同时满足相同 tool_name + 相同 canonical args + **相同 observation**，才判为空转。仅"调用多次"或"参数相同但结果不同"都不算。这条判据阻止了一个错误归类：`retr-find-then-read` 表面像重复空转（5 次调用、3 次 `search_code`），但实测 query 各不相同（`retry policy` / `retry` / `max_retries`），**不是空转**，被正确归为 `model_quality`。若按"调了多次"就归类，会写出一条假的 no-progress Bad Case。

**分类规则是结构性的，不靠字符串匹配**：provider 抛的协议错误带稳定 `code`（如 `multiple_tool_calls_not_allowed`），分类读 `code` 而非 message，所以改写文案不会改变结论。分类顺序：`protocol_violation`（可恢复的边界违规被升级为终态失败）→ planner 阶段失败（`plan.failed`）→ 其他边界终止 → 完成但违约（`model_quality`）。

**未做（下一阶段）**：`RecordedModel` 确定性重放、live 重评、以及任何修复。`replay_mode` 目前写 `recorded_model_candidate`，即"声明候选"而非"已实现"。

#### M9-B3 — Replay 与修复（待做）

两层 Replay 不可互相替代（见下方表格）；`RecordedModel` 只证明 Runtime / Schema / 状态机修复，Prompt 与 planner 行为必须 live 重评。

**交付物**

- Bad Case 沉淀：失败/越界用例自动落 `evals/bad_cases/<case_id>.json`，含任务、期望、实际步记录、trace 校验结果、以及**触发失败的归类**（模型决策错？工具 Schema 错？Runtime 状态机错？）。
- **分层 Replay（两层，不可互相替代）**：

  | 层 | 机制 | 能验证什么 | 不能验证什么 |
  | --- | --- | --- | --- |
  | **Deterministic Execution Replay** | `RecordedModel`：从原 trace 提取并冻结模型动作序列，按序重放，精确复现原来的工具选择与参数 | Runtime 状态机、Tool Schema 校验、Checkpoint、重试/幂等修复、异常恢复 | **模型行为是否改善**——决策已被脚本替代，原问题未真正复现 |
  | **Live-model Re-evaluation** | 重新跑 OMLX，用相同 Bad Case 输入，比较修复前后 | Prompt / System Prompt / Planner 策略改动是否真的让模型表现得更好 | 不能保证逐位可复现，需多次采样 |

- CLI：`forge replay --bad-case <id> --mode recorded|live`。
- 回归集：`RecordedModel` 重放纳入 keyless 门禁；**live 重评单独报告**，不进 CI。
- ADR 008：**Bad Case 存储、脱敏，与两类 Replay 的适用边界**。

**退出标准**

- 至少 3 条真实发现的失败用例进入回归集，并在修复后验证：**先红后绿**（`RecordedModel` 层）。
- 若修复涉及 Prompt / Planner，**必须附 live 重评报告**，不得用 keyless replay 声称已验证模型行为改善。

**预估**：2–3 个工作日（沉淀来自 M8-B/C 的真实失败，不再需要临时造失败）。

---

### M10 — Runtime Reliability：安全恢复

> **本节是 v2 修订重点。** 原计划写"幂等键保证副作用不重复执行"，这是过强的表述：CAS 只能覆盖"completed 已落库"的情形，无法覆盖最危险的 crash window。通用 Runtime 无法仅靠 SQLite 实现任意副作用的 exactly-once。

#### M10.1 先承认的边界

存在一个 CAS **无法判定**的窗口：

```
tool invoked → claimed 落库 → 副作用已发生（文件已写 / 命令已跑）
             → 进程崩溃 → completed 尚未落库
             → 重启后库中状态看似"未执行"
```

此时数据库不知道副作用到底发生没有。对**非幂等**副作用，再执行一次就是重复副作用。
因此本阶段的目标不是"exactly-once"，而是：

- **只读工具**：可安全重试（至多一次无效重试，无副作用）；
- **声明了稳定幂等键的工具**：可安全重放（由下游去重，Runtime 复用同一 key）；
- **无法确认结果的非幂等副作用工具**：调用已 `started` 后崩溃，**禁止自动重放**，标记 `indeterminate_side_effect`，fail 或等待人工处置。

#### M10.2 工具三类分级（由工具 spec 显式声明）

| 类别 | 声明值 | 崩溃于 `started` 后的恢复行为 |
| --- | --- | --- |
| 只读 / 纯函数 | `read_only` | 安全重试 |
| 支持稳定幂等键 | `idempotent` | 用同一 key 重放 |
| 非幂等副作用 | `non_idempotent` | **不自动重放**，落 `indeterminate_side_effect`，fail / 待人工 |

**默认值 fail-closed**：未显式声明的工具按 `non_idempotent` 处理。现有只读工具（`search_code`、`read_file`、`list_files` 等）补声明为 `read_only`；写文件、执行命令类补为 `non_idempotent`。

#### M10.3 Invocation Journal（取代"只记 completed_calls"）

SQLite 表 `tool_invocations`，与 checkpoint 同库：

```
task_id · call_id · tool_name · args_hash · tool_class · idempotency_key
state ∈ {claimed, started, completed, failed, indeterminate} · attempt
result_ref · started_at · completed_at
```

**关键顺序（这是整个设计的支点）**：

```
写入 claimed（fsync）
  → 写入 started（fsync）  ← 必须在副作用开始之前
      → 执行工具
          → 写入 completed + result（fsync）
```

`started` 先于副作用落盘，恢复时才能知道"副作用可能已发生"。

恢复判定表：

| 恢复时看到的 state | `read_only` | `idempotent` | `non_idempotent` |
| --- | --- | --- | --- |
| `completed` | 返回已存结果，不重执行 | 同左 | 同左 |
| `failed`（retryable） | 重试 | 用同 key 重放 | 按策略重试（需显式允许） |
| `started` | 重试 | 用同 key 重放 | **`indeterminate_side_effect`，禁止自动重放** |
| `claimed` | 安全执行 | 安全执行 | 安全执行 |

#### M10.4 其余可靠性项（按依赖顺序，逐项独立可测）

1. **错误分类**：`retryable` / `non_retryable` / `timeout` / `invalid_args`，进入 `ToolOutput.metadata` 与 trace。
2. **重试 + 指数退避**：仅在 retryable 上生效；`max_attempts_per_call`、`backoff_*`、`max_total_attempts` 进 `RunBudget`；逐次 attempt 入 trace 与 journal。
3. **分级超时**：超时从 dispatcher 常量改为工具 spec 可声明（保留 30s 缺省）。
4. **执行 deadline**：`RunBudget.max_wall_seconds`；超时落 `FAILED`（`error_type=deadline_exceeded`），不落 `SUCCEEDED`。
5. **ADR 009**：**恢复语义**——三类工具各自的允许动作，以及为什么不做 exactly-once 主张。

**明确不做**：熔断器、多进程分布式租约、`DEAD` 状态（`FAILED` + `error_type` 足以表达，`CANCELLED` 已存在）。

#### M10.5 退出标准（核心测试必须是难的那个）

- 测试 A（基础）：`completed` 已落库的副作用调用，恢复后**不再执行**。
- 测试 B（**核心**）：**副作用已发生、`started` 已落库、`completed` 未落库时进程崩溃，恢复后非幂等调用不得被盲目再次执行**——必须落 `indeterminate_side_effect`。
- 测试 C：`read_only` 工具在同一崩溃场景下**可以**安全重试，且不产生副作用。
- 测试 D：`idempotent` 工具复用同一 key 重放，下游去重生效（用可控假工具验证 key 传递）。
- 每个错误类型与新状态有 trace 事件；`STATUS.md` 的"能恢复"措辞升级为"能安全恢复"，附证据路径，并**显式保留"非幂等副作用为 at-most-once 而非 exactly-once"的残余风险**。

**预估**：4–6 个工作日（journal + 恢复判定表 + 崩溃注入测试占大头）。

---

### M11 — Context / Cost 治理对比实验（可选，不建议现在做）

三种配置对照：无预算控制 / 固定预算 / 动态压缩。指标沿用 §4，产出 `reports/context-governance.json`。
机制已存在（§1.1），此步只是加一组对照数据。**优先级低于 M8–M10**，且在 M9 完成后做更省力（可直接复用 Bad Case 做失败归因）。

---

## 4. 指标口径（必须先冻结，否则数字不可信）

| 指标 | 定义 | 判定方 | 数据来源 |
| --- | --- | --- | --- |
| Task Success Rate | `status == succeeded` **且**用例硬断言全部通过 | Harness 判定，不用模型自评 | `RunResult.status` + 用例断言 |
| **Tool Set Accuracy** | 实际调用工具**集合**与期望集合的匹配率（`no_tool` 用例期望空集） | 用例 `expected.tools` | 步记录 `tool_name` |
| **Tool Sequence Match**（新） | 实际调用序列与 `expected.tool_sequence` 的**顺序**一致（含重复次数） | 用例 `expected.tool_sequence` | 步记录顺序 |
| **Forbidden Tool Violation**（新） | 是否调用了 `expected.forbidden_tools` 中的任一工具（违反即该用例硬失败） | 用例 `expected.forbidden_tools` | 步记录 `tool_name` |
| Argument Accuracy | 期望参数的子集匹配，**按调用序号对齐**（见 §5） | 用例 `expected.args_contains` | 步记录 `tool_args` |
| Steps | `usage.steps`；**Plan-Execute 的 planner 那一步计入并可加偏移**（见下） | Harness | `Usage` |
| **Model Decisions**（新） | trace 中模型发出的工具调用决策数，**含被预算闸门拒绝的那一次** | Harness | `model.action` |
| **Logical Tool Calls** | `usage.tool_calls` —— 通过预算闸门被**受理**的调用数；这是 `max_tool_calls` 的口径 | Harness | `Usage` |
| **Tool Attempts**（新） | Runtime 实际执行**次数**，含 retry；独立计数器，不受 `max_tool_calls` 约束 | Harness | invocation journal |
| Token Consumption | input/output tokens 分开报，不合并；**只统计模型 token，retry 不产生模型 token** | Harness | `Usage` / `model.action` |
| Latency | 端到端墙钟；模型耗时由相邻 trace 事件 timestamp 求差得到；工具耗时含重试，累加**每次 attempt** | 测量 | trace 时间戳 / journal |
| Recovery Success Rate | 仅在"首个工具调用被注入失败"的用例上统计：最终仍 `succeeded` 的比例 | Harness | run 内失败后再成功的用例 |
| **Indeterminate Rate**（新，M10 后） | 落 `indeterminate_side_effect` 的用例占比 —— 衡量非幂等工具在崩溃下的保守程度 | Harness | journal 状态 |

**口径冻结声明（重要）**：`max_tool_calls` 恒等于 **logical** 次数，与 retry 无关；retry 由 `max_attempts_per_call` / `max_total_attempts` 单独约束。M10 引入 retry 后，M8 的 baseline 与 M10 之后的数字在**同一口径**下可直接比较，不允许隐式改变。`model_decisions` / `logical_tool_calls` / `tool_attempts` 三者含义不同，**禁止混用**：M8-A 实测已证明前者与后两者在预算边界必然不等。

**步数语义（M8-A 实测发现）**：`RunBudget.max_steps` 管的是 executor 循环，而 Plan-Execute 在循环之前先花一步做规划。若不处理，`max_steps=1` 会让 ReAct 能执行工具、Plan-Execute 一步都执行不了，两 arm 不可比。因此：授予 Plan-Execute 额外一步（`_executor_budget`），断言同步偏移（`judge_case`），并在报告中让 planner 成本**可见**——实测每个用例 Plan-Execute 恰好比 ReAct 多 1 步。

**报告约束**：每个数字必须能追到用例 id 与 trace 文件；聚合值必须同时给出用例数；ReAct 与 Plan-Execute 必须跑同一数据集、同一模型，并分别按 §3 M8 的两种资源视角报告。**报告禁止出现把 benchmark cases 与 scorer probes 混合的单一通过率**（如 `52/61`）——探针是负例，混入会读成任务成功率。

---

## 5. 数据集设计（`evals/agent_cases.json`）

61 条，覆盖建议稿的 8 类。**benchmark cases 与 scorer probes 是两类不同资产，必须分开统计**：

| 类别 | 条数 | 用途 |
| --- | --- | --- |
| **Benchmark cases** | 54 | 真正用于 Task Success / Tool / Argument 指标 |
| **Scorer probes** | 7 | 只用于验证 evaluator 能检出错误；**不计入任何 headline 指标** |

探针覆盖：缺工具、禁用工具、顺序错误、参数错误、no-tool 却调用工具、超调用上限、`EXHAUSTED` 伪装成 `SUCCEEDED`。若打分器"什么都给通过"，`grading_agreement` 会低于 1.0，门禁直接红。

### 5.1 开发集 / 留出集划分（防 benchmark leakage）

| 划分 | 条数 | 用途 | 约束 |
| --- | --- | --- | --- |
| `dev` | 45（38 benchmark + 7 探针） | 开发可见；沉淀 Bad Case；可进 CI；**允许**据此调 Prompt / Schema | — |
| `holdout` | 16（**0 探针**） | **冻结**；不参与任何调参；真实模型最终比较才跑 | 一旦跑过并据此改动过 Prompt，即视为已污染，需换题 |

**执行纪律（不只是划分）**：`make gate` **只跑 dev**。holdout 若每次提交都跑，开发者就会看到逐题失败并据此改 Prompt，它就退化成第二个 dev——划分只剩形式。holdout 只在 M8-C 正式比较时跑一次，报告单独生成（`agent-eval-holdout-omlx.json`）。探针只放 dev，避免负例占用留出名额。

这是最简单有效的防污染手段。数量不大，但**方法论上远干净于"同一批题既当开发集又当最终评测集"**——哪怕一条题都没改，反复看同一批题本身就会过拟合这套任务分布。报告必须显式记录 holdout 条数与未参与调参的声明。

### 5.2 用例分布（实际落地：61 条）

| 类别 | 条数 | 考察点 |
| --- | --- | --- |
| 单工具直接调用 | 9 | 工具集合准确性 |
| 多工具串行 | 10 | 计划长度、依赖**顺序** |
| 需检索后再调用工具 | 6 | 知识库作为工具的接入 |
| 参数歧义 | 7 | 参数准确性（相对路径、缺省值、同名多次调用） |
| 工具失败需恢复 | 7 | Recovery Rate / 三类工具的恢复行为 |
| 长任务多步依赖 | 7 | 编排策略差异的主要来源 |
| 不应调用工具 | 7 | 过度调用 / 幻觉调用（`forbidden_tools`） |
| 预算边界 | 8 | 越界是否准确落 `EXHAUSTED` 而非谎报成功 |

（含 7 条分布在各类的 scorer probes。）

### 5.3 用例 schema（最小字段）

```
id · category · split · task · tools（可用工具集）
expected:
  status
  tools                 # 集合级
  tool_sequence         # 顺序级，可省略
  max_tool_calls
  forbidden_tools
  args_contains         # 与 tool_sequence 逐位对齐的列表，null 表示不约束
  no_tool               # 期望零调用
  max_steps
inject_failure          # 确定性失败注入配置
```

**参数对齐规则**：`args_contains` 是**与 `tool_sequence` 等长的列表**，第 i 项约束第 i 次调用；同一工具被调用多次时按序号区分，不存在"expected args 对应哪一次"的歧义。

**与已有 `evals/agent_test_cases.json` 的关系**：那个是 6 条**编码任务**数据集，服务于 `framework-compare` 的 native vs LangChain 对比，目的不同。两者**并行保留、不合并**——本阶段测的是编排策略，不是框架实现。

**失败注入**：通过确定性包装工具实现（第 N 次调用返回超时/500/非法 JSON/`started` 后崩溃），无需改运行时代码，keyless 可复现；崩溃注入直接操纵 journal 行状态。

---

## 6. 方法论风险（写进计划的硬约束）

1. **keyless 层不能用来比较编排策略的"智能程度"**。`ScriptedModel` 走脚本，两种模式的差异只来自预算与接线。所以：**keyless 验证指标管线、预算交互与两种 runtime 的接线契约；策略结论只能来自真实模型运行。**
2. **Benchmark leakage 是 M8 最大的风险，不是样本量**。仅做 dev/holdout 划分不够：**holdout 必须同时移出日常 CI**，否则逐题失败信息会回流到调参，划分退化成形式。用 §5.1 的划分 + 执行纪律处理；holdout 一旦被用于调参即作废换题。
3. **负例探针不得污染主结果**。探针是 evaluator 自测资产，必须与 benchmark cases 分开统计；报告禁止出现混合通过率（§4 报告约束）。
4. **Replay 的适用范围必须说清**（§3 M9-B）。`RecordedModel` 能验证 Runtime / Schema / 状态机修复，**不能**验证 Prompt 改动让真实模型变好了——后者必须 live 重评。
5. **跨 runtime 的指标语义必须在 keyless 阶段就对齐**。M8-A 实测已发现两处不一致（调用计数口径、`max_steps` 下的 planner 步数），若不先对齐，OMLX 真跑出的差异分不清是"策略差异"还是"接线差异"。
6. **样本量**。61 条只够做趋势判断，不足以支撑"Plan-Execute 长任务成功率显著更高"这类强表述。真实模型建议每用例 3 次，报中位数与离散度；不写显著性主张，除非做了相应检验。
7. **"同一预算"必须双视角报告**（§3 M8-B）。只做等资源约束，等于人为给 Plan-Execute 多收一笔 planner 税，可能在比较预算而非编排能力；只做自然成本，则无法回答"资源受限谁会赢"。
8. **真实模型运行不可复现**。keyless 与真实模型**分报告**，前者进 CI，后者只做一次性证据并绑定 revision 与采样次数。
9. **成本口径**。本地 OMLX 无 API 账单，报告只报 token 与墙钟，**不得折算成金额**；keyless 的合成 token 永不出现在成本图中（`cost_metrics_valid: false`）。
10. **M10 后所有历史数字需重跑或标注口径**。retry 改变了 tool call 与耗时的含义（§4 已拆为 logical/attempts）；M8 baseline 若早于 M10 产出，必须在报告中注明其口径版本。

---

## 7. 工程落点（文件级）

**新增**

- `evals/agent_cases.json`、`evals/bad_cases/`
- `src/forgeharness/evaluation/agent.py`
- `src/forgeharness/observability/steps.py`
- `src/forgeharness/state/invocations.py`（M10 invocation journal + 恢复判定表）
- `models/recorded.py`（M9 `RecordedModel`，与既有 `models/scripted.py` 并列）
- `docs/adr/007-agent-benchmark-metrics.md`、`008-bad-case-and-replay-layers.md`、`009-recovery-semantics.md`
- 测试：`tests/unit/test_agent_evaluation.py`、`test_trace_steps.py`、`test_invocation_journal.py`、`test_reliability.py`（含崩溃窗口断言 A–D）

**修改**

- `runtime/loop.py`：journal 接线、重试编排、deadline、幂等键传递（改动集中在 dispatch 前后与 `_drive`）
- `tools/dispatcher.py`：错误分类、retry 策略、分级超时、attempt 计数
- `tools/base.py`、`tools/builtin.py`、`coding/*`：补 `tool_class` 声明（默认 fail-closed）
- `runtime/budget.py`：`max_attempts_per_call` / `backoff_*` / `max_wall_seconds`
- `cli.py`、`Makefile`（`gate` 加 keyless agent eval）、`README.md`、`docs/STATUS.md`、`CHANGELOG.md`

**复用（不要重写）**：`evaluation/rag.py` 的报告模式、`evaluation/control.py` 的 keyless 脚本模型模式、`observability/hash_chain.py`、`state/checkpoint.py` 的 CAS。

---

## 8. 退出标准（沿用 AGENTS.md 完成规则）

1. 每个里程碑独立通过 `make check` 与 `make gate`，且新增行为都有测试。
2. 每条对外数字绑定 revision 与报告文件路径，可在 clean checkout 重跑。
3. `docs/STATUS.md` 同步更新：已实现的进"Implemented and tested"，未验证的留在"Remaining gates"，**不得把计划写成已实现**。
4. 非平凡决策落 ADR（007–009）。
5. 保留残余风险清单：benchmark 是自建控制集而非公开基准；**非幂等副作用为 at-most-once 而非 exactly-once**；无 SWE-bench 结论；本地单机无高可用主张。

---

## 9. 简历映射与可写边界

**现在就可以写（有证据）**

- 双编排 + 预算/审批/Checkpoint/工具校验的基础设施叙述（对应 `STATUS.md` 已实现表）。
- 检索压测：5,000 分块 / 10 并发 / 100 请求，本地检索 p95 ≤ 358.3 ms ✓（`reports/retrieval-benchmark.json` 已核实）。

**M8 完成后可写**

> 构建覆盖单工具、多工具协作、知识检索、参数歧义、失败恢复、长任务依赖与预算边界的 **Agent Benchmark**，划分开发集与留出集以避免评测污染，统一 **Task Success Rate / Tool Set Accuracy / Tool Sequence Match / Argument Accuracy / Steps / Logical Tool Calls / Tool Attempts / Token / Latency / Recovery Rate** 口径，在**等资源约束与自然运行成本两种视角**下量化比较 **ReAct 与 Plan-Execute** 的差异。

**M9 完成后可写**

> 对 Planner、工具选择、参数生成、工具执行与最终输出建立结构化 Trace 投影，记录 Token、延迟、异常类型与 Checkpoint revision；将失败任务沉淀为 Bad Case，通过**确定性 Replay** 验证 Runtime / Tool Schema 修复，通过**真实模型回归**验证 Prompt 与编排策略调整。

**M10 完成后可写**

> 实现错误分类、重试退避、分级超时与执行 deadline，并建立 `claimed → started → completed` 调用日志；按只读 / 幂等 / 非幂等三类分级恢复语义，保证崩溃恢复时**已完成的副作用不被重复执行**，且**无法判定结果的非幂等调用不被盲目重放**。

**永远不写**：SWE-bench 分数、生产 SLO、多 Agent 优越性、云高可用、成本节省比例、任意副作用的 exactly-once 保证（`STATUS.md` 已明确未授权）。

---

## 10. 明确不做（本阶段）

- MCP 生态扩展、多 Agent 编排、长期记忆增强、浏览器自动化。
- 熔断器、分布式租约、`DEAD` 状态。
- 任意副作用的 exactly-once 语义——通用 Runtime 在 crash window 下做不到，只做分级 at-most-once。
- 公开基准（SWE-bench 等）复现——复现是另一个量级的工作，且需防止与自建集混用。

---

## 附：执行顺序与依赖

```
M8-A  Schema + Dataset + Evaluator            ✓ 已完成
  │
  ├─> M9-A  Trace → Step Projection            ✓ 已完成
  │        │
  │        ├─> M8-B  ReAct / Plan-Execute 真实模型对照（等资源 + 自然成本）  ← 下一步
  │        │        │
  │        │        └─> M8-C  OMLX 重复评测 + holdout 终评（只跑一次）
  │        │                 │
  │        │                 └─> M9-B  从真实失败沉淀 Bad Case + 分层 Replay
  │        │                          │
  │        │                          └─> M10  Reliability（journal + 三类分级 + 安全恢复）
  │        │                                   └─> M11（可选：Context/Cost 对照实验）
```

**为什么不是机械的 M8 → M9 → M10**：

- M8-A 冻结了指标口径，这是后面一切的前提，已完成；
- **M9-A 提到 M8-B 之前**：真跑「60 条 × 两策略 × 多次采样」后，没有步级投影，碰到失败用例只能翻原始 JSONL，诊断体验极差；
- M8-B/C 产出**真实失败**，M9-B 才有东西可沉淀——先造失败再沉淀是本末倒置；
- M10 的退出标准（副作用已发生但未落库时不得盲目重放）依赖 M9-B 的注入与重放通道来构造和证明；
- **M10 必须先改设计（journal + 三类分级）再写代码。**

**贯穿原则**：本阶段所有工作只服务一句话——**让 Agent 的执行行为可量化、失败原因可诊断，并在中断与重试下保持安全。** 不再新增 Memory / Multi-Agent / Browser / MCP / 更多 Tool。闭环是 **Measure → Diagnose → Fix → Replay → Verify**。
