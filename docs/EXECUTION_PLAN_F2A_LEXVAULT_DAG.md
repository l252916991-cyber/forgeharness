# F2a 执行计划：LexVault 形状的只读 DAG 应用

> 状态：已完成（F2a，2026-10-18）。
> 前置：F1b `Orchestrator`/`RunKernel` 已完成；F0b 边界与 legal Capability adoption 已完成。
> 目标：用一份 **LexVault 形状的只读 case bundle** 跑通“并行专家 → Critic → 校验”的 DAG，证明 `RunKernel` 能承载法律阅卷的控制流。

## 1. 为什么先做 F2a

直接接 LexVault 的 SQLite/UI/ARQ 会把“框架能力验证”和“宿主集成”混在一起。F2a 先把**数据形状和控制流**固定：

- bundle 只包含 case 元数据、页文本、页 locator；不含数据库、不写任何东西。
- DAG 的专家节点是确定性 host 代码，Critic 走 `kernel.call_model`。
- 用 `BundleCitationVerifier` 检查答案是否引用真实页号；明确不做语义/法律正确性判断。

这为 F2b（真实 LexVault 只读 host）提供可替换的适配器边界。

## 2. 交付物

| 交付物 | 位置 | 完成标准 |
|---|---|---|
| Bundle 契约 | `src/forgeharness/applications/legal/lexvault/bundle.py` | 页唯一、文本有界、租户/case 必填 |
| 只读适配器 | `.../lexvault/adapter.py` | 实现 `CaseAdapter`；按 `RunContext` 授权；无 DB |
| DAG Orchestrator | `.../lexvault/dag.py` | 三个专家并行、Critic 走 kernel、emit/checkpoint |
| 应用工厂 + Verifier | `.../lexvault/dag.py` | `compose_application` + `[page:N]` 校验 |
| 测试 | `tests/unit/test_lexvault_dag.py` | 成功、无效页、无引用、trace/usage |
| 文档 | 本文件 + `FRAMEWORK_STRATEGY.md`/`STATUS.md`/`CHANGELOG.md` | 记录 F2a 边界与后续 |

## 3. 契约草案

```python
class BundlePage(FrozenModel):
    page_no: int
    text: str
    summary: str = ""


class CaseBundle(FrozenModel):
    schema_version: Literal[1] = 1
    tenant_id: str
    case_id: str
    title: str
    version: str = "1"
    pages: tuple[BundlePage, ...]
    synthetic: bool = True
```

- locator 由 `page_no` 生成：`page:{n}`；resource id：`case:{case_id}/page:{n}`。
- 文本上限 20k/页，页数上限 100；重复页号拒绝。
- `BundleCaseAdapter` 的 `search` 做字面匹配并返回精确 span；只用于只读工具/测试，不冒充 LexVault 的混合检索。
- `LexVaultDagOrchestrator`：
  1. `facts` / `evidence` / `contradiction` 三个确定性专家 `asyncio.gather` 并行；
  2. 汇总 findings，`kernel.call_model(phase="critic")`；
  3. 只接受 `FinalAction`，`kernel.finish(final=...)`；
  4. `BundleCitationVerifier` 要求 `[page:N]` 的 N 属于 bundle；无引用但明确“材料不足”可通过。
- 不提供写工具；`ReadOnlyPolicy` 拒绝非 READ 工具。

## 4. 测试计划

1. 3 页 bundle + ScriptedModel 返回带 `[page:1]` 的答案：run SUCCEEDED；`usage.steps==1`；trace 有 3 个 `dag.node.completed`；trace 链有效。
2. 答案引用 `[page:99]`：verifier 拒绝，run FAILED。
3. 答案无引用且不是“材料不足”：verifier 拒绝。
4. 答案“材料不足”且无引用：通过（显式拒绝场景）。
5. bundle 重复页号：构造失败。
6. 适配器跨租户/未授权页：`CaseUnavailable`。
7. 全量测试、ruff、mypy。

## 5. 明确不做

- 不接 LexVault SQLite/UI/ARQ；F2b 再做。
- 不做写路径、审批、笔记、真实检索质量。
- 不判断语义支持/法律结论；verifier 只查页号存在与显式拒绝。
- 不把 bundle 适配器说成生产 LexVault 适配器。
- 不迁移 LexVault 代码。

## 6. 风险

| 风险 | 对策 |
|---|---|
| Bundle 形状与 LexVault 不一致 | 只保留最小页级字段；F2b 用适配器映射真实 DB |
| DAG 专家太“玩具” | 明确这是控制流验证；专家函数可替换，不声称法律质量 |
| Verifier 被误解为语义校验 | 文案固定“page citation valid; semantic support not assessed” |
| 过度设计 | 一个 bundle + 一个 adapter + 一个 orchestrator；不做插件系统 |

## 7. 时间盒

0.5–1 个工作日；如果 bundle/adapter 边界不稳，先改契约，不写复杂专家。

---

## 执行记录

状态：**已完成（F2a）**。

### 改动文件

| 文件 | 内容 |
|---|---|
| `src/forgeharness/applications/legal/lexvault/bundle.py` | `CaseBundle`/`BundlePage`，页唯一/有界 |
| `.../lexvault/adapter.py` | `BundleCaseAdapter`：租户/资源授权、字面搜索、精确 span、page drafts |
| `.../lexvault/dag.py` | 三个并行专家 + Critic + `BundleCitationVerifier` + 应用工厂 |
| `.../lexvault/__init__.py` | 应用包导出 |
| `tests/unit/test_lexvault_dag.py` | 8 个测试：成功、无效页、无引用、材料不足、重复页、租户/页权限、精确 span、page drafts |
| `docs/adr/025-lexvault-readonly-bundle.md` | 只读 bundle 边界与“不是 live LexVault” |
| `FRAMEWORK_STRATEGY.md`、`STATUS.md`、`CHANGELOG.md` | F2a 进展 |

### 验证

```bash
uv run --locked pytest tests/unit/test_lexvault_dag.py -q
# 8 passed

uv run --locked pytest tests/unit/test_lexvault_dag.py \
  tests/unit/test_orchestration_kernel.py \
  tests/unit/test_capability_composition.py \
  tests/unit/test_import_boundaries.py \
  tests/unit/test_legal_pilot.py -q
# passed

uv run --locked pytest -q
# exit 0；1078 collected

uv run --locked ruff check src/forgeharness/applications/legal/lexvault \
  tests/unit/test_lexvault_dag.py
# All checks passed

uv run --locked mypy
# Success: no issues found in 118 source files
```

### 未完成 / 下一步

- **F2b：** 写 LexVault 导出器或只读 DB snapshot → `CaseBundle`，用同一 DAG 跑 4 题；仍然 0 写入。
- **F1c：** 写工具/审批/journal/恢复；当前自定义编排仍只读。
- `BundleCitationVerifier` 只校验页号存在，不校验语义支持；不能当作 `LegalAnswerVerifier` 的等价物。
- 专家节点是词典式扫描，不是 LexVault 的生产专家；这是控制流验证，不是法律质量评测。
