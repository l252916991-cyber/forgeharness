# F0b 执行计划：core/application 边界 + Capability  Adoption

> 状态：已完成（F0b，2026-10-18）。
> 前置：F1a `Capability`、F1b `Orchestrator`/`RunKernel` 已完成。
> 目标：让“核心不依赖应用”变成可测试的规则，并让一个现有参考应用真正用上 `Capability`。

## 1. 为什么是这一轮

- F1a/F1b 已经给了组合原语，但还没有一个参考应用迁移过去；如果不迁移，`Capability` 只是“新 API”，不是框架实践。
- 核心仍有 Coding 兼容 shim；如果不把边界写进测试，后续应用会继续把领域代码塞进核心。
- 这轮不做 F1c（写工具/审批/恢复），也不做 LexVault 迁移；先把“核心/应用/兼容层”的边界钉住。

## 2. 交付物

| 交付物 | 位置 | 完成标准 |
|---|---|---|
| 边界测试 | `tests/unit/test_import_boundaries.py` | 干净导入核心不加载 `forgeharness.applications`；AST 扫描只允许声明的 Coding shim |
| ADR | `docs/adr/024-core-application-boundary.md` | 记录依赖方向、兼容 shim、移除条件 |
| 架构文档 | `docs/ARCHITECTURE.md` | 系统图不再以 Coding 为默认；列出 sdk/applications/integrations |
| Legal 迁移 | `src/forgeharness/applications/legal/pilot.py` | `case_qa_application` 用 `Capability` + `compose_application`；行为不变 |
| 文档/状态 | `FRAMEWORK_STRATEGY.md`、`STATUS.md`、`CHANGELOG.md` | 记录 F0b 进展与剩余 |

## 3. 边界规则

允许的核心包：`domain`、`models`、`runtime`、`tools`、`context`、`state`、`observability`、`sdk`。
允许依赖方向：

```text
applications ──> sdk ──> runtime/domain/tools/context/state
integrations ──> sdk
core          ──X──> applications
core          ──X──> coding（除两个已声明兼容 shim）
```

已知兼容 shim（API v1，暂不删除）：

- `runtime/verification.py`：`CodingVerifier` 懒加载。
- `context/__init__.py`：`PythonRepositoryMap` 懒加载。
- `runtime/__init__.py` 的 `CodingVerifier` 导出转发。

边界测试只允许这三个位置；新增核心→coding/applications 的 import 直接失败。

## 4. Legal 迁移

`case_qa_application` 从直接构造 `ApplicationSpec` 改为：

```python
return compose_application(
    id=application_id,
    version=version,
    policy_version=policy_version,
    model_factory=model_factory,
    capabilities=(
        Capability(
            "case-tools",
            tools=(CaseSearchTool(adapter, store), CasePageTool(adapter, store)),
        ),
    ),
    policy=ReadOnlyCasePolicy(),
    verifier=LegalAnswerVerifier(store),
)
```

要求：

- 工具顺序不变：`search_case`、`read_case_page`。
- 不新增 instructions/context providers，避免行为变化。
- 现有 `tests/unit/test_legal_pilot.py` 全部通过。
- 增加一个断言：应用由 capability 组合而来（工具名/顺序不变）。

## 5. 测试计划

1. 子进程 `python -c "import forgeharness.sdk"`，断言 `sys.modules` 里没有 `forgeharness.applications.*`。
2. 对核心包做 AST 扫描，`forgeharness.coding` import 只允许三个 shim 位置。
3. 现有 legal pilot 测试通过。
4. 新增 legal 组合断言。
5. 全量测试、ruff、mypy。

## 6. 明确不做

- 不删除兼容 shim（API v1；v2 再移除）。
- 不迁移 commerce/coding 到 `compose_application`。
- 不做 F1c 写工具/审批/恢复。
- 不接 LexVault 真实 host（F2）。
- 不改 `RunKernel` 行为。

## 7. 风险

| 风险 | 对策 |
|---|---|
| 边界测试误报 | 允许三个 shim 位置，其他新增 import 才失败 |
| Legal 行为变化 | 只把 tools 包进 capability；不新增 instructions/context |
| 工具顺序变化 | 断言 `search_case` 在前、`read_case_page` 在后 |
| 文档过度承诺 | 明确 F0b 只做边界 + 一个参考应用，不是全量迁移 |

## 8. 时间盒

0.5–1 个工作日；如果 legal 测试暴露行为差异，先回滚迁移，只保留边界测试与 ADR。

---

## 执行记录

状态：**已完成（F0b）**。

### 改动文件

| 文件 | 内容 |
|---|---|
| `tests/unit/test_import_boundaries.py` | AST + 干净子进程 import 边界测试；只允许两个 Coding shim |
| `src/forgeharness/applications/legal/pilot.py` | `case_qa_application` 改用 `Capability` + `compose_application` |
| `tests/unit/test_legal_pilot.py` | 断言工具顺序 `search_case` → `read_case_page` |
| `docs/ARCHITECTURE.md` | 系统图/模块边界/依赖方向改为 app-neutral |
| `docs/adr/024-core-application-boundary.md` | 边界规则、shim 冻结、v2 移除条件 |
| `docs/FRAMEWORK_STRATEGY.md`、`docs/STATUS.md`、`CHANGELOG.md` | 进展与边界说明 |

### 验证

```bash
uv run --locked pytest tests/unit/test_import_boundaries.py tests/unit/test_legal_pilot.py -q
# 29 passed

uv run --locked pytest -q
# exit 0；1070 collected

uv run --locked ruff check src/forgeharness/applications/legal/pilot.py \
  tests/unit/test_import_boundaries.py tests/unit/test_legal_pilot.py
# All checks passed

uv run --locked mypy
# Success: no issues found in 114 source files
```

### 未完成 / 下一步

- API v2 时移除两个 Coding 兼容 shim；现在只冻结，不删除。
- Commerce 已在后续轮次迁移到 `Capability` + `compose_application`；两个参考应用现在使用同一组合 API。
- F1c：写路径 ADR 026 与 fail-closed 测试已完成；审批/journal 的自定义实现仍待抽取共享执行器。
- F2a/F2b-1：只读 bundle/DAG 与 SQLite 导出器已完成；真实 LexVault 案例运行仍待做。
