# P0 执行计划：引用完整性与导出快照

> 状态：已完成（2026-10-20）。
> 依据：2026-10-20 改进审查（P0-1 ~ P0-5）。
> 目标：让 LexVault bundle 的引用校验从“页号存在”升级到“页号 + 精确原文 + digest”，导出成为一致性快照，并堵住边界测试的动态 import 漏洞。

## 1. 范围

| 项 | 内容 | 完成标准 |
|---|---|---|
| P0-1 引用完整性 | `BundlePage.text_sha256`（自动派生 + 显式传入必须匹配）；`CaseBundle.snapshot_digest`；verifier 支持 `[page:N]「精确引文」`，可选 `strict_quotes` | 篡改页文本/错误引文/严格模式缺引文均拒绝 |
| P0-2 导出快照 | 导出器单次读事务（`BEGIN/COMMIT`）；version 绑定页 digest | 页文本变化（updated_at 不变）时 version 必变 |
| P0-3 原文保真 | 导出不再 `strip()` 存储文本，只用 strip 判空 | 首尾空白原样保留 |
| P0-4 边界加固 | `test_import_boundaries` 扫描 `import_module("...")` 字符串 | 动态 import 应用/coding 会被测试抓到 |
| P0-5 文档修复 | STATUS 的 ADR 009“未实现”历史矛盾；FRAMEWORK_STRATEGY 的“kernel 强制 approval”措辞 | 与 ADR 023/026 一致 |

## 2. 明确不做

- 不做 F1c 写路径、不拆 `sdk/application.py`、不加 capability composition digest（P1 另轮）。
- 不改 `RunKernel`/`AgentRuntime` 行为。
- 不做真实模型/律师评测。
- 不改变 `[page:N]` 无引文形式在默认模式下的兼容性（现有测试与真实 DB 4 题运行仍通过）。

## 3. 设计

### 3.1 BundlePage digest

```python
text_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

@model_validator(mode="before")   # 未提供时自动 sha256(text)
@model_validator(mode="after")    # 显式提供时必须等于 sha256(text)
```

- 自动派生保证现有构造点零改动。
- 显式传错 digest → 构造失败（防止伪造快照）。

### 3.2 CaseBundle.snapshot_digest

`sha256(canonical JSON(tenant, case, version, [(locator, page_no, text_sha256)]))`；页文本任何变化都会改变它。

### 3.3 Verifier 升级

- 引文语法：`[page:N]「原文精确子串」`（引文块紧跟页号）。
- 有引文 → 必须是该页 `text` 的精确子串；空引文拒绝。
- `strict_quotes=True` → 每个 `[page:N]` 都必须带引文。
- 无论哪种模式，校验时重算页 digest 与 `text_sha256` 比对（防 bundle 对象被篡改）。
- “材料不足”显式拒绝路径保持不变。
- 语义支持仍然不评估（文案不变）。

### 3.4 导出器

- `isolation_level=None` + 显式 `BEGIN`…`COMMIT`：case 与 pages 两次 SELECT 处于同一读快照。
- 存原文；`strip()` 仅用于判空。
- 默认 `version = "{case_id}:{updated_at}:{pages_digest16}"`，页内容变化即变。

### 3.5 边界测试

- `_imported_modules` 额外扫描 `import_module(...)` / `__import__(...)` 的字符串字面量参数。
- 两个既有 Coding shim 正好落在 allowlist；新增一个“动态 import 可被检测”的自检用例。

## 4. 测试计划

1. bundle：digest 自动派生正确；显式错误 digest 拒绝；`snapshot_digest` 随页文本变化。
2. verifier：精确引文通过；引文不是原文子串拒绝；strict 模式缺引文拒绝；digest 篡改（`model_copy`）拒绝；材料不足仍通过。
3. 导出：首尾空白保留；页文本修改后 version 变化（updated_at 不变）。
4. 边界：动态 import 字符串被 `_imported_modules` 检出。
5. 全量回归 + ruff + mypy。

## 5. 交付物

- `src/forgeharness/applications/legal/lexvault/bundle.py`、`dag.py`、`exporter.py`
- `tests/unit/test_lexvault_dag.py`、`test_lexvault_export.py`、`test_import_boundaries.py`
- `docs/adr/027-bundle-citation-integrity.md`
- `docs/archive/STATUS.md`（新段落 + ADR 009 历史标注）、`docs/archive/FRAMEWORK_STRATEGY.md`（措辞修复 + 进展）、`CHANGELOG.md`

## 6. 风险

| 风险 | 对策 |
|---|---|
| digest 字段破坏现有构造点 | before-validator 自动派生；全量回归 |
| 引文语法过严导致兼容断裂 | 默认非 strict；`[page:N]` 仍可用 |
| 显式 BEGIN 与 sqlite3 自动事务冲突 | `isolation_level=None` 全手动管理 |
| RUF001 全角标点告警 | 测试文件已有 noqa；源码 prompt 用 ASCII 标点 + 引文括号 |

## 7. 时间盒

0.5–1 个工作日；digest/引文契约若在全量回归中暴露兼容问题，先回退 strict 默认值，不放宽校验。

---

## 执行记录

状态：**已完成（P0 批次）**。

### 改动文件

| 文件 | 内容 |
|---|---|
| `.../lexvault/bundle.py` | `BundlePage.text_sha256`（自动派生 + 显式必须匹配）；`text_digest()`；`CaseBundle.snapshot_digest` |
| `.../lexvault/dag.py` | `BundleCitationVerifier` 升级：`[page:N]「精确引文」`、`strict_quotes`、空引文拒绝、校验时重算 digest；Critic prompt 要求逐字引文 |
| `.../lexvault/exporter.py` | 单读事务（`isolation_level=None` + `BEGIN/COMMIT`）；原文保真（strip 仅判空）；默认 version 绑定页 digest |
| `tests/unit/test_lexvault_dag.py` | +7：精确引文、改写引文、strict、空引文、digest 篡改、digest 派生/强制、snapshot_digest 漂移 |
| `tests/unit/test_lexvault_export.py` | +2：首尾空白保留（含纯空白页跳过）、页文本变化 → version/digest 必变（updated_at 不变） |
| `tests/unit/test_import_boundaries.py` | +1 动态 import 检出自检；扫描覆盖 `import_module("...")`；allowlist 修正为 3 个 shim |
| `docs/adr/027-bundle-citation-integrity.md` | 本轮契约与 ADR 024 shim 清单勘误 |
| `docs/archive/STATUS.md` | 新段落 + ADR 009“未实现”历史矛盾标注 |
| `docs/ARCHITECTURE.md` | shim 数量 2→3，注明动态扫描 |
| `docs/archive/FRAMEWORK_STRATEGY.md` | 修复“kernel 强制 approval”措辞（approval 属 ReAct，只读 kernel 显式拒绝）；进展注记 |
| `CHANGELOG.md` | Unreleased 新增本批次条目 |

### 计划外发现

动态 import 扫描立即抓到**第三个未登记 shim**：`context/repository.py`（懒加载
`forgeharness.coding.repository`，仅 `test_paths_repository.py` 使用）。按 ADR 024
同一原则处理：冻结进 allowlist，API v2 一并移除；ADR 027 记录勘误。这验证了
P0-4 的必要性——静态扫描确实看不见它。

### 验证

```bash
uv run --locked pytest tests/unit/test_lexvault_dag.py tests/unit/test_lexvault_export.py \
  tests/unit/test_import_boundaries.py tests/unit/test_application_conformance.py \
  tests/unit/test_runtime_conformance.py -q
# 41 passed

uv run --locked pytest -q
# exit 0；1109 collected（+10）

uv run --locked ruff check <本轮文件>
# All checks passed

uv run --locked mypy
# Success: no issues found in 119 source files
```

### 未完成 / 下一步

- P1：拆 `sdk/application.py`（941 行）；`RunKernel`/`AgentRuntime` 共享 model/tool 执行器（F1c 入口）。
- P1：`compose_application` 记录 capability 名单/composition digest 进 spec/trace；迁移剩余直接构造 `ApplicationSpec` 的应用（`commerce/consultation.py`、`records.py`、`coding/application.py`）。
- P1：`RunKernel.call_model` 失败时的 rejected-token 计费与稳定 error code（改进审查的 P0-4 项；本轮计划明确“不改 `RunKernel`/`AgentRuntime` 行为”，故留待 F1c 共享执行器一并处理，避免双改）。
- P2：README/pyproject 定位改写；真实模型 + 律师复核。
