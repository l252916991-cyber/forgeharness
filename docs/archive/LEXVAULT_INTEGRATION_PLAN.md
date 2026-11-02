# LexVault × ForgeHarness 改进计划

> 状态：v0.1 草案（2026-10-18），**未执行、未提交**。
> 范围：LexVault 0.3.0（`~/Documents/本地律所`）与 ForgeHarness 0.5.0（`~/Documents/多agent协作`）。
> 定位：这不是“把 LexVault 换成 ForgeHarness”的迁移计划，而是“先证明值不值得换，再把确定有价值的部分吸收进来”的证据计划。
> 2026-10-18 补充：如果目标是“把 ForgeHarness 做成承载律所/电商的框架”，本文件应降级为 F2 的 host 验证子计划；框架化路线见 [FRAMEWORK_STRATEGY.md](FRAMEWORK_STRATEGY.md)。

## 0. 一页结论

1. **默认运行时不变。** LexVault 的原生 DAG 继续作为默认；ForgeHarness 只做只读旁路，不进入 `agent_runs/agent_steps`，不写记忆，不改 UI 默认路径。
2. **两条主线并行，另设对照轨。**
   - Track A：在 LexVault 内做低风险硬化（精确引用、预算、trace 完整性）。这些改进即使 sidecar 失败也有独立价值。
   - Track B：在 ForgeHarness 侧做一个 **独立 Python 3.12 进程**的只读 sidecar，消费 LexVault 导出的“证据包”，不直接读 `law_review.db`。
3. **先做 2–3 周的最小可信对照，再决定是否投入完整对照。** Stage 1 只回答“值不值得继续”；Stage 2 才回答“移不迁移”。
4. **硬门槛先写死。** 引用原文区间 100%、跨案/撤回 0 失败、sidecar 对 LexVault 0 写入、未验证答案不得成功；启用 A2 后，模型自报引用也必须 100% 可定位。质量只要求“不劣于 native”，不预设 ForgeHarness 更强。
5. **最可能的结局：** 吸收 2–4 项能力（精确引用、预算、哈希链，可能加模型引用契约），不整体迁移；LangGraph 是否退役单独决策，避免同时维护三个运行时。
6. **一句话：** 用 Stage 1 的有限成本，买一个“继续/停止”的可靠证据；不要用确定的迁移成本换未证的收益。

---

## 1. 目标、非目标与成功标准

### 1.1 目标

- 回答三个问题：
  1. ForgeHarness 的运行时治理、引用校验和评测能力，能否给 LexVault 带来**可测**的收益？
  2. 哪些能力可以低成本移植，哪些必须整体迁移？
  3. LangGraph 这一层是否应该退役？
- 产出一份可复现的三路对照报告（native / langgraph / sidecar）和一份决策记录。
- 至少落地一项独立于迁移决策的 LexVault 改进。

### 1.2 非目标

- 不整体替换 LexVault 原生 DAG。
- 不把 ForgeHarness 加进 LexVault 的运行时依赖。
- 不引入第三个默认运行时；对照结束后最多保留两个。
- 不把 ForgeHarness 的 commerce、Skills、MCP、Qdrant、Postgres、编码 Agent 带入法律阅卷。
- 不在本计划内实现写入型“法律笔记 + 审批流”；那需要单独的业务立项。
- 不声称引用校验能证明语义支持或法律结论正确。

### 1.3 成功标准

| 阶段 | 成功意味着 |
|---|---|
| Stage 1 | 有可复现的 8 题对照；能明确回答“继续/停止”；A1 来源区间校验通过测试；sidecar 对 LexVault 0 写入、0 跨案 |
| Stage 2 | 16 题 + 人工复核 + 故障注入完成；硬门槛全部有结论；形成 ADR；至少一项 LexVault 改进已合并 |
| 失败也算成功 | 如果 sidecar 不达标，保留报告并停止，只吸收已验证的 A1/A3/A4；不为了“用上新框架”放宽门槛 |

### 1.4 优先级总表

| 优先级 | 改进项 | 工作流 | 依赖 sidecar？ | 说明 |
|---|---|---|---|---|
| P0 | 基线与冻结 | A0 | 否 | 不做就无法复现/回滚 |
| P0 | 引用来源区间（offset + digest） | A1 | 否 | 最大、最确定的收益 |
| P0 | 预算与截止时间 | A3 | 否 | 防止长跑、超时、上下文失控 |
| P0 | 只读 sidecar 最小闭环 | B0–B3 | 是 | Stage 1 的核心实验 |
| P1 | 模型引用契约 | A2 | 否 | 达到 ForgeHarness 同级引用校验 |
| P1 | trace 哈希链 | A4 | 否 | 审计证据增强，非防篡改 |
| P1 | 人工复核 + 故障注入 | C2/C3 | 是 | Stage 2 的决策证据 |
| P2 | 迁移决策 / LangGraph 处置 | A5/G4 | 视结果 | 最后做，避免先射箭后画靶 |
| 不排期 | 多 Agent 辩论 / 动态路由 | — | — | 只有单 Agent + 校验器出现天花板才立项 |

> P0 表示“决策前必须完成”，不表示都在 Stage 1。Stage 1 只做 A0 + A1 + sidecar 最小闭环；A3/A4 放在 Stage 2 开头，避免最小对照被预算/哈希链分散注意力。

---

## 2. 事实基线（为什么不能直接替换）

| 事实 | 证据 | 对计划的影响 |
|---|---|---|
| LexVault 的专家节点基本是确定性代码，DAG 内只有 Critic 调一次本地 LLM | app/agents.py（`本地律所/app/agents.py#L569`）、app/agents.py（`本地律所/app/agents.py#L818`） | 把专家改成 LLM 子 Agent 会增加成本/方差，能力未必增加；优先保留确定性 |
| 引用校验只查编号、页存在、quote 非空；明确不查语义和金额 | app/agents.py（`本地律所/app/agents.py#L53-L72`） | 这是可移植的起点，但不能把 ForgeHarness 的校验说成“法律正确性” |
| `best_quote` 会归一化空白、截断并加省略号 `…` | app/services.py（`本地律所/app/services.py#L589-L598`） | **现有 `contexts[].quote` 不是精确原文片段**，不能直接喂给 ForgeHarness 的 `CitationVerifier`；A1 必须先产出原始偏移 |
| `pages` 只有 `(document_id, page_no)`，没有版本/摘要列 | app/db.py（`本地律所/app/db.py#L149-L156`） | 需要 `snapshot_id + text_sha256` 才能表达“当前发布版本”；否则只能校验“当前内容”，不能证明版本未变 |
| 原生 DAG 显式并行 fan-out Facts/Evidence 等，测试也断言并行 | app/agents.py（`本地律所/app/agents.py#L777-L816`）、tests/test_04_langgraph_runtime.py（`本地律所/tests/test_04_langgraph_runtime.py#L78`） | ForgeHarness `WorkflowRunner` 是顺序执行，不能作为 drop-in DAG 替代 |
| ForgeHarness 的 `WorkflowRunner` 顺序遍历 `ordered_steps()` | [sdk/workflows.py](../../src/forgeharness/sdk/workflows.py#L330-L449) | 只适合顺序审批/笔记流，不适合替换并行专家图 |
| ForgeHarness 法律适配器的 `search` 是字面匹配，且是合成样例 | [adapter.py](../../src/forgeharness/applications/legal/adapter.py#L165-L218) | 不能拿它当 LexVault 的检索层；对照必须用“证据包回放”隔离检索差异 |
| ForgeHarness 的 `CitationVerifier` 只接受当前发布版本 + 精确字符区间 | [sdk/evidence.py](../../src/forgeharness/sdk/evidence.py#L32-L63) | 这是最值得吸收的能力；但它不判断语义支持，也不判断法律推理 |
| 两边 Python 要求冲突：LexVault `>=3.11,<3.15`，ForgeHarness `>=3.12,<3.14` | `本地律所/pyproject.toml#L6`、[ForgeHarness/pyproject.toml](../../pyproject.toml#L16) | sidecar 必须独立进程/独立 venv；不能把 ForgeHarness 变成 LexVault 依赖 |
| 两个工作副本都没有 Git 元数据 | 本次检查 `git status` 均返回 not a git repository | 先做本地快照/哈希清单；否则改动不可审、不可回滚、不可复现 |
| ForgeHarness 的业务/生产资格均为 false，尽管工程门禁通过 | [business-release-audit.json](../../reports/business-release-audit.json)（`business_qualified=false`、`production_qualified=false`、`gate_count=72`） | 工程门禁 ≠ 业务资格；不能拿“测试多”当迁移理由 |

### 2.1 三个直接结论

1. **不能直接移植 `CitationVerifier`。** 它要求 `quote == content[start:end]`；LexVault 当前 quote 带省略号且经过空白归一化。A1 必须先改检索上下文的引用表示，而不是只加一个校验函数。
2. **不能拿 sidecar 的检索结果和 native 比检索。** 公平做法是：native 先检索，导出“证据包”，sidecar 回放同一批上下文；这样差异只来自运行时、提示词和校验。
3. **不能把 ForgeHarness 装进 LexVault 的 venv。** Python 版本冲突 + 依赖面 + 无 Git 元数据，决定了它只能是独立进程。

---

## 3. 两条主线、一个对照、四道门

```
Track A（LexVault 硬化）   A1 来源区间 ──> A3 预算 ──> A4 哈希链 ──> A2 模型引用契约（可选） ──> A5 LangGraph 决策
Track B（ForgeHarness）    B0 隔离/打包 ──> B1 证据包 ──> B2 回放适配器 ──> B3 只读服务 ──> B4 trace 摘要
Track C（对照）            C0 数据集/口径 ──> C1 三路运行器 ──> C2 人工复核 ──> C3 故障注入
门                          G0 输入冻结 ──> G1 继续/停止 ──> G2 sidecar 就绪 ──> G3 报告冻结 ──> G4 迁移决策
```

| 门 | 时点 | 通过条件 | 不通过的动作 |
|---|---|---|---|
| G0 | 第 1 周末 | 数据集、评分口径、阈值、环境、源码哈希全部冻结 | 不跑对照；先补冻结 |
| G1 | Stage 1 末（第 3 周） | A1 测试通过；sidecar 8/8 完成（成功或正确拒绝）；对 LexVault 0 写入、0 跨案；来源区间校验 100%；至少一项质量指标（contract_pass / source_span）非劣 | 停止 sidecar，只保留 A1；记录原因 |
| G2 | Stage 2 中（第 5 周） | 16/16 完成（成功或正确拒绝）；来源区间 100%，启用 A2 时模型引用 100%；故障注入全部 fail-closed；人工复核无 P0 安全发现 | 修复或停止；不带病进入决策 |
| G3 | Stage 2 末（第 7 周） | 报告冻结；所有硬门槛有结论；负面结果也保留 | 不决策；补证据 |
| G4 | 第 8 周 | ADR 批准；运行时数量 ≤ 2；迁移/吸收/停止三选一 | 维持现状，不擅自加第三运行时 |

---

## 4. Track A：LexVault 运行时硬化

> 这一轨不依赖 ForgeHarness 是否最终被采用。排序按“风险低、收益直接”优先。

### A0. 基线与冻结（P0，2–3 人日）

- 对两个仓库做源码快照：推荐本地 `git init`（不推远端），先检查 `.gitignore`，确认不提交 `data/`、`output/`、模型凭据、案件原文；若不接受 Git，则生成 `snapshots/<date>/source-manifest.json`，记录每个文件的 sha256。
- 冻结运行环境：Python 版本、`uv.lock` 哈希、模型 endpoint/model、temperature、最大 token、硬件、并发。
- 记录现有 LangGraph 使用情况：`SELECT runtime, COUNT(*) FROM agent_runs GROUP BY runtime`、恢复次数、失败类型。这个数据决定 A5，不要凭感觉退役。
- 产出：`benchmarks/lexvault_compare/README.md`、`source-manifest.json`、`baseline.json`。

**验收：** 同一输入、同一环境重复跑 3 次，答案契约结果一致；延迟分布被记录（允许波动，但要可解释）。

### A1. 精确引用：从“来源存在”到“原文区间”（P0，4–6 人日）

**问题：** 当前 `quote` 是展示用摘要，不是原文精确片段；`validate_review_answer` 只检查 `quote` 非空。要吸收 ForgeHarness 的精确区间校验，必须先把数据表示改对。

**改动点：**

1. 检索层增加“定位”而不是“截断”：
   - 新增 `locate_quote(text, terms) -> (start, end, quote)`，在**原始页文本**上定位，不归一化、不省略。
   - `best_quote` 保留为 UI 展示函数，但不再作为引用校验的输入；展示省略号在 UI 层加，不进入 `quote`。
2. 上下文字段扩展：
   ```json
   {
     "document_id": 12,
     "page_no": 3,
     "name": "…",
     "quote": "原始页文本的精确子串",
     "quote_start": 120,
     "quote_end": 260,
     "page_text_sha256": "…",
     "document_updated_at": "…",
     "snapshot_id": "…"
   }
   ```
3. `validate_review_answer` 增加可选的只读 `page_resolver`：
   - 校验 `1 <= index <= len(contexts)`；
   - 校验 `quote_start < quote_end` 且 `quote == page_text[start:end]`；
   - 校验 `sha256(page_text) == page_text_sha256`；
   - 校验页面仍属于当前 case、文档未被删除；
   - 任一项失败即 `valid=false`，并在 `issues` 中给出具体项。
4. 持久化：`agent_runs.citations_json` 写入 offset、digest、snapshot_id；UI 引用卡增加“原文区间已核验 / 仅来源存在”两种标识。
5. 语义边界：返回值继续保留 `semantic_entailment_checked=false`，新增 `source_span_checked=true`；不要把“区间正确”显示成“引用支持结论”。

**测试：**

- 精确子串通过；空白差异失败；带 `…` 的展示 quote 失败；越界 offset 失败；摘要不匹配失败；跨案 page 失败；页面删除后失败。
- 现有 4 个问题的 answer contract 不回归。
- 至少 1 个对抗用例：模型引用一个不存在的页，必须失败关闭。

**验收：** 所有成功答案的引用都能在原文中复现；失败时 `issues` 可定位；UI 不出现“语义已核验”的误导性文案。

**边界（很重要）：** A1 证明的是“被引用的资料卡确实对应原文区间”，**不是**“模型引用的就是支持结论的原文”。要达到 ForgeHarness `CitationVerifier` 的语义，必须做 A2；不做 A2 时，UI/报告只能写“来源区间已核验”，不能写“引用已核验”。

### A2. 模型引用契约：让模型给出它引用的原文（P1，3–4 人日，可选）

> A1 只证明“检索片段是真的”，不证明“模型引用的就是它读到的”。A2 才把校验推进到模型输出。

- 新增一种 Critic 输出契约（JSON）：
  ```json
  {
    "answer": "…",
    "citations": [
      {"index": 1, "quote": "模型声称支持结论的原文片段"}
    ]
  }
  ```
- 校验：`quote` 必须是所引页面的精确子串（可在原文中定位），否则不通过；不通过时回退到确定性汇总，并标记 `citation_check=failed`。
- UI 适配：从 JSON 渲染 answer；如果本地模型不支持 JSON mode，用“围栏 JSON + 解析失败回退”，不要让 UI 直接显示原始 JSON。
- A/B：16 题，比较 contract pass、引用精确率、律师评分、调用次数。**如果律师评分或通过率下降，就不合并**；这是一次实验，不是默认升级。

**验收：** 模型能稳定返回可解析契约（≥95%）；模型编造 quote 时 100% 失败关闭；UI 无回归。

### A3. 运行预算与截止时间（P0，2–3 人日）

**问题：** 当前没有硬性的 step/token/wall 预算；长上下文、模型慢、重试可能拖垮一次 run。

- 新增 `RunBudget`：
  - `max_steps`（默认 8）
  - `max_llm_calls`（默认 2）
  - `max_input_chars`（默认 48k，启发式，不冒充 tokenizer）
  - `deadline_s`（默认 90）
- 在 Coordinator 和 LangGraph wrapper 的节点/LLM 调用前检查；超限时：
  - 停止可选节点；
  - 返回明确的 `degraded` 或 `failed`；
  - 不写 memory，不伪装成功；
  - 在 `agent_runs` 记录 `budget_json` 和 `usage_json`。
- 上下文预算：按检索排名确定性截断，法条/关键页单独保留；截断必须记录 `dropped_contexts`，不能静默丢引用页。
- 验收：构造超时、超步数、超上下文三个用例，均 fail-closed 且有可读错误；正常 4 题延迟不增加 >5%。

### A4. 轨迹哈希链（P1，2 人日）

**问题：** `agent_steps` 可被直接改写，审计证据强度不足。

- `agent_steps` 增加 `prev_hash`、`step_hash`；`agent_runs` 增加 `trace_root`（新 run 才写，老 run 标记 `unverified`）。
- 哈希输入：`run_id, node_name, status, input_json, output_json, started_at, finished_at, prev_hash`，规范 JSON（排序、紧凑、UTF-8）。
- 新增 `verify_run_chain(run_id)`（脚本或只读 API）；断链时返回第一处不一致，不返回案件正文。
- 边界：这是 **tamper-evident**，不是 tamper-proof；能改库的人可以重算链。文案不要过度承诺。

**验收：** 正常 run 验证通过；手工改一条 step 后验证失败并定位；老 run 不被误判为通过。

### A5. LangGraph 处置（P1，1–2 人日）

先看数据，再决定，不要为了“框架新”而迁移：

| 观察 | 动作 |
|---|---|
| `runtime='langgraph'` 的 run 很少，且没有真实恢复需求 | Stage 2 退役；删除依赖和 675 行包装层，减少维护面 |
| 有恢复需求，且现有 checkpoint 工作正常 | 保留但冻结；不新增功能；不与 sidecar 同时默认启用 |
| 恢复需求存在，但 LangGraph 的恢复语义不够 | 做 1 周 spike：用 ForgeHarness `WorkflowRunner` 复现 3 个故障场景；不要直接全量迁移 |
| 只是觉得 LangGraph 旧 | 不迁移；先量化维护成本 |

**原则：** 对照结束后最多两个运行时。若 sidecar 通过，优先“native + sidecar”，退役 LangGraph；若 sidecar 不通过，优先“native + LangGraph（冻结）”，不要三套并存。

---

## 5. Track B：ForgeHarness 只读 sidecar

> 目标不是“把 LexVault 接进 ForgeHarness”，而是“让 ForgeHarness 在一个受控、可回放、只读的输入上跑法律问答”，从而做公平对照。

### B0. 隔离与打包（P0，2 人日）

- 从冻结源码构建 ForgeHarness wheel，记录 sha256；sidecar 在**独立 Python 3.12 venv** 中安装该 wheel。
- sidecar 不 import LexVault，不读 `law_review.db`；只读“证据包”文件。
- 代码位置建议：`integrations/lexvault/`（不进入 ForgeHarness wheel 的 `packages`），独立 `pyproject.toml`：
  ```
  integrations/lexvault/
    pyproject.toml          # forgeharness[models]==0.5.0 (wheel path), fastapi, uvicorn
    README.md
    src/lexvault_sidecar/
      app.py                # /health, /v1/answer
      pack.py               # EvidencePack 模型 + 校验
      adapter.py            # ReplayCaseAdapter
      service.py            # 组装 case_qa_application + 运行
    tests/
  ```
- 运行方式：`uv run --project integrations/lexvault ...`，只监听 `127.0.0.1`，每次对照一个随机 token。
- 失败边界：sidecar 起不来、模型不可达、pack 哈希不符，都返回明确错误，不重试到“看起来成功”。

**验收：** 在 LexVault 的 Python 3.11 环境外独立启动；`/health` 返回 wheel 哈希和模型可达性；不加载 LexVault 包。

### B1. 证据包：把“同题同上下文”变成可回放输入（P0，3 人日）

**为什么需要证据包：** 如果 sidecar 自己检索，对照就混入了检索差异；如果 sidecar 直接读库，又破坏只读边界。证据包把 native 的检索结果冻结成输入，双方看到同一批页和同一段原文。

**导出方：** LexVault `scripts/export_evidence_pack.py`（只读）。

- 输入：`case_id`、`question`、`--snapshot`（可选，复制 DB 到临时只读快照）。
- 处理：调用现有 `HybridRetriever.retrieve()`；对每个 context 计算原始 `quote_start/end`、`page_text_sha256`；召回 memory 快照（只读）；记录模型/预算/权限 scope。
- 输出：`output/compare/packs/<pack_id>.json`，目录 0700；`pack_id = sha256(canonical JSON without pack_id)`。
- 禁止：写 `agent_runs/agent_steps/memories`；把 API key 写进 pack；把 pack 上传到网络。

**包内容：** 见附录 A。关键点：

- `contexts[].text` 是原始页文本（有上限），`quote` 是精确子串，`quote_start/end` 是原始偏移；
- `snapshot_id` 是本次导出的逻辑版本（可用 DB 文件哈希 + 导出时间）；
- `scope.resources` 只含当前 case 和已授权页；
- `model` 只含 endpoint/model/temperature，不含密钥；
- `budget` 与 native 的 A3 配置一致。

**验收：** 同一 DB 快照、同一问题导出两次，`pack_id` 相同；不同页内容得到不同 `pack_id`；pack 不含跨案页；JSON Schema 校验通过。

### B2. 回放适配器与发布（P0，4 人日）

- 新增 `ReplayCaseAdapter`（放在 `integrations/lexvault/`，不污染 ForgeHarness 核心）：
  - `cases()`：仅返回 pack 的 case（scope 匹配时）；
  - `case()`：标题、页 locator `page:{no}`；
  - `page()`：返回 pack 中的原始文本；
  - `search()`：**返回 pack 预计算的 hits**，而不是重新做字面检索。模型若换 query，仍返回同一批证据；这是“检索回放”，不是生产检索。README 必须写清楚。
  - `page_drafts()`：从 pack 页生成 draft，供宿主发布。
- 发布：在 sidecar 的临时 `RevisionStore` 中，用宿主身份（editor/reviewer/publisher）发布 pack 页；版本号固定为 `1`，digest 为 `sha256(text)`。发布是 setup，模型不可见，也不提供 publish 工具。
- 撤回/陈旧：对照运行时，sidecar 可以加载 pack v1，再用 v2 覆盖同一资源；`CitationVerifier` 应拒绝 v1 引用。这是故障注入的一部分。
- **不要**把 `ReplayCaseAdapter` 说成 LexVault 的生产适配器；生产版本应改为调用宿主只读检索 API，另立项目。

**验收：** 同一 pack 两次加载得到相同 citation 校验结果；换 query 不会“检索到 pack 外的页”；page 越界/未授权被拒绝；sidecar 临时 state 删除后不残留案件文本。

### B3. 只读服务契约（P0，3 人日）

`/v1/answer` 只接受“pack 路径 + 期望哈希”，不接受任意 case_id/问题，避免越权。pack 路径必须落在启动时配置的 allowlist 目录内，拒绝 `..`、符号链接和目录外路径：

```json
{
  "pack_path": "/…/packs/<pack_id>.json",
  "pack_sha256": "…"
}
```

响应：

```json
{
  "schema_version": 1,
  "run_id": "…",
  "status": "succeeded | refused | failed",
  "answer": "…",
  "citations": [
    {"resource_id": "case:1/page:3", "version": 1, "digest": "…", "start": 120, "end": 260, "quote": "…"}
  ],
  "verification": {
    "passed": true,
    "reason": "current scoped source and exact quote verified; semantic support not assessed",
    "checks": ["scope", "published", "version", "digest", "span"]
  },
  "usage": {"steps": 3, "llm_calls": 2, "input_chars": 12345, "latency_ms": 4567},
  "trace": {"root_hash": "…", "summary_path": "…"},
  "error": null
}
```

规则：

- `status=succeeded` 必须在 `LegalAnswerVerifier` 通过之后；未通过时 `status=failed`，不得把未验证 answer 当成功返回。
- `insufficient_evidence=true` 时不得带 citations；这是 ForgeHarness 已有的契约。
- 任何异常只返回分类码（`model_timeout`、`pack_mismatch`、`verification_failed`），不返回案件正文/堆栈。
- 并发限制 1–2；请求超时；sidecar 崩溃不自动重试到成功。

**验收：** 正常题返回引用；无证据题返回 refused；模型超时返回 failed；pack 哈希不符拒绝；响应中无密钥、无绝对路径、无堆栈。

### B4. 模型与 trace（P0，3 人日）

- 模型：复用 `OpenAICompatibleModel` 指向 LexVault 同一个本地 endpoint/model；sidecar venv 必须安装 `forgeharness[models]`（含 `httpx`），否则会在 import 阶段失败。temperature 0；最大 token 与 native 一致。若接口不匹配，写一个薄适配器，不改 ForgeHarness 核心。
- 预算：把 pack 的 `budget` 映射到 ForgeHarness `Budget`；超限时明确失败，不静默截断到“成功”。
- trace：sidecar 自己的 SQLite state 记录 hash-chained trace；返回 `root_hash` 和摘要，不返回原始消息。对照 harness 归档 state 目录，用于审计/复现。
- 隐私：pack 和 state 都含案件文本，目录 0700；对照结束删除；日志只记 ID/哈希/长度。

**验收：** 同一 pack 重跑，trace root 可复现（模型确定性允许的范围内）；日志无案件原文；state 可被独立校验。

### B5. 仅当 G4 选 B/C 才做的产品化（不排期）

- 用宿主只读检索 API 替换 `ReplayCaseAdapter`，不再依赖离线 pack；检索质量回到 LexVault 的 FTS5+向量+RRF。
- 认证映射：LexVault 的 token/OIDC/案件权限 → `RunContext` 的 tenant/actor/resources；每次请求重新授权，不信任 pack 里的 scope。
- trace 保留/脱敏/清理策略；state 升级与回滚；sidecar 崩溃后的恢复语义。
- 中文法律评测集、律师复核流程、模型升级回归；这些不在本计划内，另立项目。
- **核心原则：** 如果必须修改 ForgeHarness 核心才能跑通，说明适配层设计失败；G1 停止，不要为了 sidecar 把框架改造成 LexVault 专用。

---

## 6. Track C：三路对照

### C0. 数据集与评分口径（P0，3–4 人日）

**题目：** 完整集 16 题，四类各 4 题。Stage 1 先用 8 题子集（四类各 2 题），Stage 2 再扩到 16 题；扩题前不改变已冻结的评分口径。**G1 通过后、Stage 2 开跑前冻结 16 题全集；不得根据 Stage 1 结果删题、换题或改 gold 页。**

| 类别 | 例子 | 目的 |
|---|---|---|
| 事实/金额 | 募集资金流向；总额与人数 | 基础契约、引用支持 |
| 矛盾/疏漏 | 张某陈述是否矛盾；证据疏漏 | 专家节点价值 |
| 法条/跨文档 | 法条适用；跨文档冲突 | 检索+推理边界 |
| 对抗/失败 | 编造页、陈旧 quote、提示注入、跨案 | fail-closed 与安全 |

每题标注：`question`、`case_id`、`gold_pages`、`answerable`、`expected_refusal`、`rubric_notes`。gold 页由人工确认，不用模型生成。

**评分：**

- 确定性：answer contract、source span、refusal、citation 数、延迟、LLM 调用数、输入字符数。
- 人工：逐条 citation 判断“该页该区间是否支持这句话”；answer 级判断完整性、法律谨慎性、可用性。两位评审（1 法律 + 1 工程），盲评、随机顺序、记录分歧。
- 不把模型评委作为唯一证据；人工标注是主证据。

### C1. 三路运行器（P0，3 人日）

- 在 LexVault 侧新增 `scripts/compare_runtimes_v2.py`（不要改现有 `compare_agent_runtimes.py` 的默认行为）：
  - 每轮顺序随机：native → langgraph → sidecar，或反向；减少预热偏差。
  - native 正常跑；同时导出 pack；sidecar 用同一 pack。
  - langgraph 用同一输入、同一 memory 快照。
  - 记录 `pack_id`、源码哈希、模型配置、run_id、延迟、调用数。
- 如果同一输入下 native 两次检索得到的 contexts 不一致，增加一个 `native(pack)` 对照臂（native 也吃同一 pack），单独报告；不要把它和端到端 native 混为一个数字。
- **归因规则：** sidecar 在模型引用校验上的优势可能来自 A2 契约，而不是编排。报告必须把“来源区间”“模型引用”“人工支持”分栏，不能合并成“框架更好”。
- 输出：`output/compare/<date>/runs.jsonl`、`report.md`、`manifest.json`。
- 失败处理：某一路失败不隐藏；计入分母，单独列失败原因。

**验收：** 同一输入可重放；三路都拿到同一批 contexts；报告能追溯到 pack 和源码哈希。

### C2. 人工复核（P1，3–5 法律人日）

> Stage 1 不做 48 份完整盲评，只做 8 题 × 2 路的工程师 spot-check，用于发现明显退化；正式人工复核从 Stage 2 开始。

- 48 份答案（16 题 × 3 路），盲评；每份最多 6 条 citation。
- 先做 8 题校准，统一“支持/部分支持/不支持”的口径；再评剩余。
- 记录：citation 支持、answer 完整性、是否遗漏关键反证、是否需要律师额外回看、评审用时。
- 分歧处理：讨论达成一致；无法一致的标为 `disputed`，不计入“支持”，单独报告。

**验收：** 两位评审完成 ≥90% 样本；分歧率记录；所有 `disputed` 有备注。

### C3. 故障注入（P1，3 人日）

按附录 B 执行。每个场景记录：是否 fail-closed、是否泄露、是否写入 LexVault、恢复后是否重复副作用。

**验收：** 所有安全类场景 100% fail-closed；任何跨案/写入/伪成功都是 P0，直接停止。

---

## 7. 指标与阈值

> 阈值在 G0 冻结；以下是建议值，不是既成事实。样本小，结论以描述性为主，不宣称统计显著。

| 指标 | 定义 | 建议门槛 | 性质 |
|---|---|---|---|
| `source_span_pass` | 成功答案所引 context 的 `quote == page[start:end]` 且 digest 匹配的比例 | **100%** | 硬门槛 |
| `model_quote_pass` | 仅 A2：模型自报 quote 能在所引页面精确定位的比例 | **100%**（启用 A2 时） | 硬门槛 |
| `contract_pass` | 现有 answer contract 全通过的比例 | ≥0.98，且不低于 baseline −1pp | 硬门槛 |
| `citation_precision` | 人工判定“支持该句”的 citation / 全部 citation | ≥ native −2pp；无编造引用 | 非劣 |
| `refusal_correct` | 无证据题正确拒绝且不带 citation 的比例 | 无证据题 **0 个伪事实答案** | 硬门槛 |
| `cross_case_leak` | 跨案/未授权页出现在答案或 trace 的次数 | **0** | 硬门槛 |
| `sidecar_writes` | sidecar 对 LexVault DB 的写入次数 | **0** | 硬门槛 |
| `fail_closed` | 故障场景中以 failed/refused 结束、无伪成功的比例 | **100%** | 硬门槛 |
| `p95_latency` | 端到端延迟（同机、同模型、同并发） | ≤1.5× native | 非劣 |
| `llm_calls` | 每题模型调用次数 | ≤3；且 ≤2× native | 成本 |
| `human_review_min` | 律师复核一份答案的中位用时 | 不高于 native；若目标为“值得上线”，≥20% 降幅 | 价值 |
| `ops_startup` | sidecar 冷启动到 `/health` 可用 | ≤5s；一条命令 | 可运维 |

**决策规则：**

- 任一硬门槛失败 → 不迁移；若失败在 sidecar，保留 A1/A3/A4 的成果。
- 硬门槛全过 + 非劣 → 进入“价值”判断。
- 非劣但无价值（律师时间不降、运维成本明显增加）→ 只吸收，不设为默认。
- 质量更好但成本/延迟明显更差 → 只做受控试点，不设为默认。

---

## 8. 决策矩阵与 LangGraph 处置

### 8.1 四种结局

| 结局 | 条件 | 动作 |
|---|---|---|
| **A. 只吸收** | sidecar 未通过非劣/价值；A1/A3/A4 有独立价值 | 合并 A1/A3/A4；sidecar 归档为研究床；不增加运行时 |
| **B. 受支持的第二运行时（只读）** | 硬门槛全过；质量非劣；有审计/复核价值 | native 默认；sidecar 仅只读问答可选；退役 LangGraph；写路径仍 native |
| **C. 只读路径迁移** | B 的条件 + 运维/维护收益明确；团队接受 sidecar 常驻 | 只读问答默认 sidecar；native 保留写/审批；分阶段，不一次性删 native |
| **D. 停止** | 任一硬门槛失败，或 Stage 2 超预算 50% 仍无证据 | 停止；保留报告；只做 A1 等确定改进 |

### 8.2 LangGraph 单独决策

| 问题 | 答案 | 动作 |
|---|---|---|
| 是否有真实的“崩溃后恢复并继续”需求？ | 否 | 退役；删除依赖和包装层 |
| 是，且现有 checkpoint 满足 | 是 | 保留但冻结；不新增功能 |
| 是，但恢复语义不足 | 是 | 1 周 spike 验证 ForgeHarness `WorkflowRunner`；不要全量迁移 |
| sidecar 通过 | — | 优先“native + sidecar”，不要三套并存 |

**原则：** 运行时数量 ≤2；每增加一个，必须有一项可测能力作为交换。

### 8.3 如果目标只是“换掉 LangGraph”

- 痛点是恢复：做时间盒 spike，不要换整个 Agent 框架。
- 痛点是维护：先统计使用率，再退役；不要用第三个框架替代旧框架。
- 痛点是“多 Agent 协作”：两者都不是现成答案；见 §13 的可选研究。

---

## 9. 排期与投入

### Stage 1：最小可信对照（2–3 周，12–16 人日，部分任务可并行）

| 周 | 工作 | 产出 |
|---|---|---|
| W1 | A0 冻结；C0 数据集 v0（8 题子集）；A1 spike；B1 pack schema | baseline、cases.v0、pack schema |
| W2 | A1 合并；B0/B1/B2 最小 sidecar；C1 运行器（三路） | 8 题跑通；对 LexVault 0 写入/0 跨案 |
| W3 | 结果分析；G1 评审 | Stage 1 报告；继续/停止决定 |

**G1 停止条件（任一）：**

- A1 无法在不回归现有契约的情况下通过；
- sidecar 8 题中成功答案的来源区间不是 100%；
- 出现跨案、写入 LexVault、伪成功；
- sidecar 需要修改 ForgeHarness 核心才能跑通（说明集成边界不成立）；
- 投入超过 20 人日仍无可比结果。

### Stage 2：完整对照与决策（4–5 周，22–30 人日）

| 周 | 工作 | 产出 |
|---|---|---|
| W4 | A2/A3/A4；B3/B4 硬化 | 预算、哈希链、模型契约 |
| W5 | 16 题 + 人工复核 | 48 份盲评 |
| W6 | C3 故障注入 + 修复 | fail-closed 报告 |
| W7 | 分析、G3、ADR 草案 | 冻结报告 |
| W8 | G4 决策；文档/清理 | ADR、路线图 |

**总投入：** 34–46 工程人日 + 3–5 法律人日；全职约 7–9 周，兼职（每周 3–4 天）约 10–14 周。若只能投入一半，只做 A1/A3 + Stage 1 的只读对照，不做 sidecar 硬化/产品化。

---

## 10. 风险与对策

| 风险 | 影响 | 对策 | 触发信号 |
|---|---|---|---|
| Python 3.11/3.14 vs 3.12 | 无法集成/CI 破坏 | 独立进程 + 独立 venv；不加依赖 | 任何“顺便装进 LexVault”的提议 |
| 无 Git 元数据 | 不可审、不可回滚 | 本地 git init 或哈希快照；禁止提交数据/密钥 | 改动无法用 diff 说明 |
| quote 不是精确原文 | 校验误报/漏报 | A1 先改 offset 表示；不复用 `best_quote` | 测试出现 `…`/空白差异 |
| 无页面版本 | 无法证明“当前发布版本” | snapshot_id + text_sha256；后续再考虑 revision 表 | 页面内容变更后引用仍通过 |
| SQLite 并发/锁 | 导出 pack 失败或读到半写状态 | 只读快照/WAL/busy_timeout；导出失败即停 | `database is locked` |
| 模型方差 | 对照不可复现 | temperature 0、固定 seed（若支持）、3 次重复、记录模型版本 | 同题同配置答案差异大 |
| sidecar 漂移 | 对照结果不可比 | wheel sha256 锁定；升级只在 G 点 | 源码哈希与 pack 不匹配 |
| 范围蔓延 | 成本失控 | 非目标清单；sidecar 只读；不做笔记审批 | 出现“顺便加写入/多 Agent” |
| 法律结论被过度承诺 | 业务/合规风险 | 文案保留“未验证语义/法律结论”；律师复核 | UI 出现“已验证正确” |
| 案件数据泄露 | 严重 | 合成/匿名优先；pack 0700；日志脱敏；不外网 | trace/日志出现原文 |
| 三运行时并存 | 维护成本 | 最多两个；G4 强制选择 | 同时默认启用三套 |

---

## 11. 交付物

| 交付物 | 位置（建议） | 完成标准 |
|---|---|---|
| 本计划 | `docs/archive/LEXVAULT_INTEGRATION_PLAN.md` | 评审通过，G0 冻结 |
| 对照数据集 | `benchmarks/lexvault_compare/cases.v0.json` | 16 题、gold 页、可答性标注 |
| 评分口径 | `benchmarks/lexvault_compare/RUBRIC.md` | 双人校准、分歧规则 |
| 源码/环境清单 | `benchmarks/lexvault_compare/manifest.json` | 两边源码哈希、lock、模型 |
| A1 引用来源区间 | LexVault `app/citation_verification.py` + 迁移 + 测试 | 7 类测试通过；UI 标识 |
| A3 预算 | LexVault `app/agents.py` + `app/db.py` | 超限 fail-closed；usage 持久化 |
| A4 哈希链 | LexVault `app/agents.py` + 验证脚本 | 断链可定位；老 run 标记 |
| sidecar | `integrations/lexvault/` | 独立 venv；`/health`、`/v1/answer`；测试 |
| 三路运行器 | LexVault `scripts/compare_runtimes_v2.py` | 可重放；报告含 manifest |
| 对照报告 | `output/compare/<date>/report.md` | 三路、人工、故障注入 |
| ADR | `docs/adr/022-lexvault-readonly-sidecar.md`（G2 后） | 边界、状态所有权、失败语义 |
| 决策记录 | `docs/adr/023-agent-runtime-decision.md`（G4 后） | A/B/C/D + LangGraph 处置 |

---

## 12. 未来 5 天（可直接执行）

| 天 | 任务 | 完成标志 |
|---|---|---|
| D1 | 两个仓库做本地快照/哈希清单；记录 Python/uv/模型/硬件；统计 LangGraph 使用率 | `manifest.json` + 使用率查询结果 |
| D2 | 写 8 题 v0（四类各 2：事实/金额、矛盾/疏漏、法条/跨文档、对抗/无证据），标注 gold 页/可答性；冻结评分口径草案 | `cases.v0.json` + `RUBRIC.md` v0 |
| D3 | A1 spike：`locate_quote` 返回原始 offset；用 1 个页面验证 `quote == text[start:end]`；补 4 个单测 | spike 分支/快照；测试通过 |
| D4 | 定义 `EvidencePack` schema v0；写导出脚本最小版（1 题、1 case、只读） | 导出 1 个 pack；schema 校验通过 |
| D5 | sidecar 骨架：`/health` + 加载 pack + 用 stub 模型跑通 1 题；确认对 LexVault 0 写入 | 1 题端到端；日志无原文 |

**D5 评审：** 如果 A1 的 offset 方案不成立，或 sidecar 无法在不读 DB 的情况下跑通，停止并重新设计；不要带着这两个不确定性进入 Stage 1 的后两周。

---

## 13. 待确认问题

1. **主目标是质量、可维护性，还是审计/可解释性？** 这决定 G4 的权重；如果只是“减少维护”，优先退役 LangGraph，而不是引入 sidecar。
2. **是否能用 1–2 个真实（脱敏）案件做对照？** 只用合成数据，citation precision 的外部有效性有限。
3. **是否有第二位法律评审？** 单人标注要披露局限；没有第二人时，至少做一次盲评复核。
4. **生产上是否真的需要崩溃恢复？** 这直接决定 LangGraph 是“保留”还是“退役”。
5. **可接受的延迟/成本上限是多少？** 如果 p95 1.5× 不可接受，sidecar 的 ReAct 方案基本不可行。
6. **是否接受一个常驻 sidecar 进程？** 如果不接受，ForgeHarness 只能作为离线评测床，不能成为运行时。

> 如果这些问题的答案暂不确定，默认按“Stage 1 + A1/A3，sidecar 只到最小闭环”推进；不要先做产品化。

### 可选研究（不排期）：多 Agent 协作

只有在“单 Agent + 校验器”在 16 题上出现明确天花板时才立项，且必须预注册：

- 候选：独立 Reviewer Agent、矛盾辩论、动态专家路由。
- 约束：同一证据包、同一总预算、同一模型；与 native/单 Agent 对比。
- 指标：citation precision、矛盾召回、成本、延迟、律师时间；负结果保留。
- **不因为“多 Agent”本身增加默认功能**；ForgeHarness 当前也只有“单主循环 + 受限子 Agent + 顺序 Workflow”，不是并行专家图。

---

## 附录 A：EvidencePack v0（示例）

```json
{
  "schema_version": 1,
  "pack_id": "sha256:…",
  "created_at": "2026-10-18T12:00:00+08:00",
  "case": {
    "id": 1,
    "title": "示例案件",
    "snapshot_id": "sha256:…"
  },
  "question": "募集资金的主要流向是什么？",
  "contexts": [
    {
      "document_id": 12,
      "page_no": 3,
      "document_name": "银行流水.pdf",
      "text": "……原始页文本……",
      "text_sha256": "…",
      "quote": "……精确子串……",
      "quote_start": 120,
      "quote_end": 260,
      "rank": 1,
      "channels": ["fts5", "vector", "rrf"]
    }
  ],
  "memory": [],
  "model": {
    "provider": "openai-compatible",
    "base_url": "http://127.0.0.1:8000/v1",
    "model": "local-model",
    "temperature": 0
  },
  "scope": {
    "tenant_id": "local",
    "resources": ["case:1", "case:1/page:3"]
  },
  "budget": {
    "max_steps": 6,
    "max_input_tokens": 30000,
    "deadline_s": 60
  }
}
```

**约束：**

- `text` 上限 20k 字符/页；pack 总量上限 2MB；超出则导出失败，不截断。
- `pack_id` 对 canonical JSON（不含 `pack_id`）做 sha256。
- 目录 0700；对照结束删除；日志只记 `pack_id`。
- 不允许 `api_key`、cookie、绝对用户路径进入 pack。

---

## 附录 B：故障注入清单

| # | 注入 | 观察点 | 期望 |
|---|---|---|---|
| 1 | 模型超时 | sidecar LLM 调用 | `failed:model_timeout`；无成功答案 |
| 2 | 模型 500 | 同上 | 重试有上限；最终 failed |
| 3 | 模型返回非 JSON/坏 JSON | 契约解析 | `failed:contract_invalid`；不把原文当答案 |
| 4 | 页面 digest 被改 | CitationVerifier | 引用校验失败；status=failed |
| 5 | 页面撤回/删除 | RevisionStore | 引用不可用；fail-closed |
| 6 | pack 含跨案页 | pack 加载 | 拒绝整个 pack；0 泄露 |
| 7 | sidecar 中途重启 | 恢复 | 只读 run 可重跑；无重复副作用；否则 failed |
| 8 | LexVault DB 锁 | 导出 | 导出失败，不生成半包 |
| 9 | 上下文超限 | 预算 | 明确 `budget_exhausted`；不静默丢引用页 |
| 10 | 页内提示注入 | 模型/工具策略 | 不改变只读工具集；人工复核标记；不产生写入 |
| 11 | 模型引用未发布页 | 校验 | 失败关闭 |
| 12 | sidecar state 损坏 | trace 校验 | 明确 failed；不返回旧成功 |

**判定：** 任何“伪成功”（status=succeeded 但引用未验证/跨案/写入 LexVault）都是 P0；Stage 2 直接停止。

---

## 附录 C：不建议做的事

1. 把 Facts/Evidence/Contradiction 等确定性专家改写成 LLM 子 Agent，只为“用上框架”。
2. 用 ForgeHarness `WorkflowRunner` 替换并行专家图；它是顺序执行。
3. 把 ForgeHarness 加进 LexVault 的 `pyproject.toml` 或 CI 矩阵。
4. 用 ForgeHarness 法律适配器的字面检索替代 LexVault 的 FTS5+向量+RRF。
5. 让 sidecar 直接读 `law_review.db` 或写 `agent_runs/agent_steps/memories`。
6. 把“引用区间正确”宣传成“语义支持”或“法律结论正确”。
7. 在对照未通过前改默认 UI/默认运行时。
8. 同时保留 native、LangGraph、sidecar 三个默认运行时。
9. 为了“多 Agent”增加并行/辩论原语，却没有预注册的评测收益。
10. 在没有 Git/快照的情况下做大面积重构。
