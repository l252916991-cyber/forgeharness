# F3b 执行计划：运行时 Conformance Matrix

> 状态：已完成（F3b，2026-10-19）。
> 前置：F3a 应用契约 conformance 已完成。
> 目标：用同一套运行时矩阵检查 legal DAG 与 commerce 两个应用：成功、trace/inspect、verifier 拒绝、错误 binding、未授权资源。

## 1. 矩阵

| 运行时行为 | Legal DAG | Commerce |
|---|---|---|
| 成功 run | `LexVaultDagOrchestrator` + `[page:N]` verifier | ReAct + `product_lookup` + `CommerceVerifier` |
| trace 链 | `verify_trace` | `verify_trace` |
| inspect | 持久 checkpoint | 持久 checkpoint |
| verifier 拒绝 | 无引用答案 | 非产品 JSON |
| 错误 application binding | 运行前 `ScopeError` | 运行前 `ScopeError` |
| 未授权资源 | 无 page grant → `CaseUnavailable` → failed | 无 `product:a` → policy deny → failed |

## 2. 明确不做

- 不做 F1c 写工具/审批/journal 实现。
- 不要求两个应用拥有相同 capability（legal DAG 无工具，commerce 有工具）；矩阵只检查共同运行时行为。
- 不做真实模型/律师质量。
- 不做完整所有策略/审批/恢复组合；审批/resume 仍由 commerce 现有测试覆盖，legal DAG 的 custom resume 明确不支持。

## 3. 设计

`tests/unit/test_runtime_conformance.py`：

```python
@dataclass(frozen=True)
class RuntimeCase:
    harness: Harness
    context: RunContext
    task: str
    wrong_context: RunContext
    unauthorized_context: RunContext
```

- `build_legal(tmp_path, run_id, invalid)`：bundle + adapter + DAG; model 返回 `[page:1]` 或无效文本。
- `build_commerce(tmp_path, run_id, invalid)`：临时 `CommerceStore` + seed product；`ProductModel` 先 `product_lookup`，再回显 tool JSON；invalid 直接 final 文本。
- 参数化两个 builder，跑 4 个测试。

## 4. 验收

- 新增测试通过；两个应用在相同矩阵下行为一致（均 fail closed）。
- `pytest -q`、ruff、mypy 通过。
- `CONFORMANCE.md`/`FRAMEWORK_STRATEGY.md`/`STATUS.md`/`CHANGELOG.md` 更新。
- 明确记录“审批/resume 不在共同矩阵”。

## 5. 风险

| 风险 | 对策 |
|---|---|
| commerce 夹具复杂 | 只 seed 一个 product；不接 catalogue/search |
| legal DAG 无工具 | 矩阵只比较共同行为，不强行同构 |
| 未授权失败原因不同 | 断言“未成功 + fail closed”，不比较具体错误文本 |
| 测试重复现有覆盖 | 这是跨应用矩阵；保留现有细粒度测试作为回归 |

## 6. 时间盒

0.5–1 个工作日；如果 commerce 成功路径难稳定，先跑 legal DAG + commerce invalid，再补成功路径。

---

## 执行记录

状态：**已完成（F3b）**。

### 改动文件

| 文件 | 内容 |
|---|---|
| `tests/unit/test_runtime_conformance.py` | legal DAG 与 commerce 的 4 项共同运行时矩阵 × 2 应用 = 8 个测试 |
| `docs/CONFORMANCE.md` | 增加 F3b 共享运行时矩阵与未覆盖项 |
| `FRAMEWORK_STRATEGY.md`、`STATUS.md`、`CHANGELOG.md` | F3b 进展 |

### 覆盖

- 成功 run + `verify_trace` + `inspect`：两个应用都通过。
- verifier 拒绝：legal DAG 无引用、commerce 非产品 JSON，均 FAILED，trace 有 `verification.rejected`。
- 错误 application binding：两个应用都在运行前 `ScopeError`。
- 未授权资源：legal DAG 无 page grant → failed；commerce 无 `product:a` → policy deny → failed。
- 不强制两个应用同构（legal DAG 无工具，commerce 有工具），只比较共同行为。

### 验证

```bash
uv run --locked pytest tests/unit/test_runtime_conformance.py -q
# 8 passed

uv run --locked pytest -q
# exit 0；1099 collected

uv run --locked ruff check tests/unit/test_runtime_conformance.py
# All checks passed

uv run --locked mypy
# Success: no issues found in 119 source files
```

### 未完成 / 下一步

- 审批/resume、budget、policy 排列仍未进入同一共享表；审批由 commerce 现有测试覆盖，legal DAG 明确不支持 custom resume。
- F1c 写路径实现（ADR 026 gate）。
- 真实模型/律师质量、optional extras 安装矩阵。
