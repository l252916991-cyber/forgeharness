# F2b + Commerce Adoption + F1c Gate 执行计划

> 状态：已完成（2026-10-18）。
> 前置：F2a 只读 bundle/DAG、F1b `RunKernel`、F0b 边界与 legal Capability adoption 已完成。
> 本轮把上一轮列出的剩余建议一起推进：F2b 导出器 + 4 题、commerce 迁 `Capability`、F1c 写路径边界。

## 1. 范围

| 工作 | 内容 | 完成标准 |
|---|---|---|
| **F2b-1 导出器** | 从 LexVault-shaped SQLite 只读导出 `CaseBundle`；保留 document/page 身份与 bundle-global `[page:N]` | 只读 URI、0 写入、缺案/空案失败 |
| **F2b-2 4 题** | 用导出的 bundle 跑 4 个只读问题 | 4 次 run 全部 SUCCEEDED；DB 哈希不变 |
| **F2b-3 契约** | `BundlePage` 增加 `document_id/document_name/document_page_no`；locator 支持 `doc:{id}/page:{n}` | 旧 `page:{n}` bundle 仍兼容 |
| **F3a Commerce** | `commerce_application` / `commerce_readonly_application` 改用 `Capability` + `compose_application` | 工具顺序/策略/验证器不变；现有 tests 全绿 |
| **F1c Gate** | 写路径 ADR + 失败关闭测试；不实现审批/journal 自定义路径 | 写工具/审批被拒绝；`approve/resume` 拒绝；不伪造恢复 |

## 2. 明确不做

- 不实现 F1c 的写工具/审批/journal/恢复；需要先抽 `AgentRuntime` 的共享执行器（ADR 026）。
- 不接 LexVault UI/ARQ/写入；F2b 仍是只读。
- 不移除 API v1 Coding shim（API v2 再做）。
- 不把 commerce 的写路径迁到自定义 Orchestrator。
- 不声称法律/电商质量或生产就绪。

## 3. F2b 设计

```text
LexVault SQLite (read-only URI)
  -> bundle_from_lexvault_db(case_id, tenant_id)
  -> CaseBundle(pages: global page_no + document locator)
  -> BundleCaseAdapter
  -> LexVaultDagOrchestrator (same DAG)
  -> BundleCitationVerifier
```

- `page_no` 是 bundle-global 引用编号，`[page:N]` 用它。
- `document_id`/`document_page_no` 保留回 LexVault 页面的路径；`locator` 为
  `doc:{document_id}/page:{document_page_no}`。
- 导出器只读 `cases/documents/pages`；空文本页跳过；空案失败；>100 页失败。
- 4 题测试只验证控制流与 0 写入，不冒充真实检索/法律质量。

## 4. F1c Gate

- `RunKernel.call_tool` 继续只允许 `RiskLevel.READ`。
- policy 返回 `REQUIRE_APPROVAL` 时也必须拒绝，不能静默当 ALLOW。
- `Harness.approve/resume` 对自定义编排继续拒绝。
- ADR 026 记录三种方案与选择“先抽取共享执行器，再开放写路径”。

## 5. 测试计划

1. 导出器：document 身份、global page_no、locator、缺案/空案。
2. 4 题：导出 bundle + DAG；4 个 run SUCCEEDED；源 DB 哈希不变。
3. Bundle 旧格式：`page:{n}` 仍工作。
4. Commerce：现有 business/commerce tests；工具顺序不变。
5. F1c gate：写工具拒绝、审批要求拒绝、resume/approve 拒绝。
6. 全量测试、ruff、mypy。

## 6. 风险

| 风险 | 对策 |
|---|---|
| 导出器读到写一半的 DB | read-only URI + `PRAGMA query_only`；导出失败即停 |
| schema 变化破坏 F2a | document 字段可选；旧 locator 保持 `page:{n}` |
| 4 题只是脚本答案 | 明确只验证控制流；真实模型/检索不在本轮 |
| F1c 被误解为已实现 | ADR 026 + 失败关闭测试 + 文档明确“写路径仍 ReAct” |
| Commerce 行为变化 | 只包 capability；工具顺序/策略/验证器不变，现有 tests 回归 |

## 7. 时间盒

1 个工作日；如果 F1c 抽取发现无法复用，不强行实现，保留 ADR 与 gate。

---

## 执行记录

状态：**已完成（F2b-1 + commerce adoption + F1c gate）**。

### 改动文件

| 文件 | 内容 |
|---|---|
| `src/forgeharness/applications/legal/lexvault/bundle.py` | `BundlePage` 增加 document 身份；locator 支持 `doc:{id}/page:{n}` |
| `.../lexvault/exporter.py` | LexVault-schema SQLite 只读导出器 |
| `.../lexvault/dag.py` | 通过 bundle locator 映射回 global `[page:N]` |
| `.../lexvault/__init__.py` | 导出器/契约导出 |
| `src/forgeharness/applications/commerce/application.py` | commerce 两个工厂改用 `Capability` + `compose_application` |
| `tests/unit/test_lexvault_export.py` | 导出身份、缺案/空案、4 题、DB 哈希不变 |
| `tests/unit/test_orchestration_kernel.py` | 写工具/审批要求 fail-closed |
| `docs/adr/026-custom-orchestrator-write-path.md` | F1c 方案与“暂不实现写路径” |
| `FRAMEWORK_STRATEGY.md`、`STATUS.md`、`CHANGELOG.md` | 本轮进展 |

### 验证

```bash
uv run --locked pytest tests/unit/test_lexvault_export.py -q
# 3 passed（含 4 题循环）

uv run --locked pytest tests/unit/test_lexvault_export.py \
  tests/unit/test_lexvault_dag.py tests/unit/test_orchestration_kernel.py \
  tests/unit/test_business_modules.py tests/unit/test_commerce_knowledge.py \
  tests/unit/test_import_boundaries.py -q
# 60 passed

uv run --locked pytest -q
# exit 0；1082 collected

uv run --locked ruff check ...
# All checks passed

uv run --locked mypy
# Success: no issues found in 119 source files
```

### 真实 LexVault 数据验证

在 `/Users/xiaoy/Documents/本地律所/data/law_review.db` 上做了一次只读导出与 4 题运行：

- case `1`：14 页、5 份文档；`synthetic=false`。
- 4 个只读问题全部 `succeeded`，每次 `usage.steps=1`。
- 源 DB SHA-256 前后一致；未记录页文本/标题。
- 模型是 `ScriptedModel`（固定引用 `[page:1]`），因此这只验证**数据形状 + 控制流 + 0 写入**，不是真实模型/律师质量评测。

### 未完成 / 下一步

- **F2b-2 真实质量：** 真实模型回答 + 律师复核 + 4 题对照；当前只验证了真实 DB 的数据形状与只读控制流。
- **F1c 实现：** 抽取 `AgentRuntime` 的审批/journal 执行器后才能开放写工具；当前写路径仍 ReAct-only。
- **F3 conformance：** commerce 已迁 `Capability`，但两应用同一套 conformance/take-apart suite 尚未建立。
- **API v2：** 两个 Coding shim 仍未移除。
