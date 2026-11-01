# F3a 执行计划：应用契约 Conformance + Take-apart

> 状态：已完成（F3a，2026-10-19）。
> 前置：F2b-1、commerce Capability adoption、F1c gate 已完成。
> 目标：用同一套测试检查 legal 与 commerce 的 `ApplicationSpec` 契约，证明“两个应用共用同一核心”不是口号。

## 1. 范围

| 检查 | 内容 |
|---|---|
| Spec 身份 | `id/version/policy_version` 非空；返回类型是 `ApplicationSpec` |
| 工具契约 | 工具名唯一；每个工具都有 `spec/input_model/execute` |
| 治理契约 | 每个 app 都有 `policy.evaluate/can_approve` 与 `verifier.verify` |
| 组合契约 | legal/commerce 的工厂输出都来自 `compose_application` 形状；工具顺序稳定 |
| Take-apart | legal pilot：`search_case` → `read_case_page`；commerce：5 个工具固定顺序；readonly：`product_lookup`；legal DAG：无工具 |
| 作用域失败关闭 | 使用错误 application binding 时 Harness 在运行前拒绝 |

## 2. 明确不做

- 不做 F1c 写工具/审批/journal 实现；不开放自定义编排写路径。
- 不做真实模型/律师质量评测。
- 不接 LexVault UI/ARQ。
- 不做完整运行时 conformance（scope/policy/approval/budget/trace 的全矩阵仍由现有 SDK/workflow 测试覆盖）；本轮是应用契约层。
- 不删除 API v1 shim。

## 3. 测试设计

`tests/unit/test_application_conformance.py`：

1. 参数化四个应用工厂：
   - legal pilot（`case_qa_application`，真实 synthetic case + RevisionStore）；
   - legal DAG（`lexvault_dag_application`，bundle）；
   - commerce（`commerce_application`，临时 CommerceStore）；
   - commerce readonly（`commerce_readonly_application`）。
2. `test_application_spec_contract`：身份、工具唯一/可执行、policy/verifier、budget、instructions、context providers。
3. `test_tool_order_is_stable`：四个应用的工具名顺序。
4. `test_wrong_application_binding_is_rejected`：Harness 在运行前拒绝错误 binding。
5. `test_public_sdk_contract_only`：所有 spec 的 tools/policy/verifier 来自 `forgeharness.sdk` 契约。
6. `test_no_empty_tool_specs`：工具名/描述/输入 schema 非空。

## 4. 验收

- 新增测试通过。
- `pytest -q` 全量通过。
- `ruff`、`mypy` 通过。
- `docs/guides/CONFORMANCE.md` 记录当前覆盖与缺口。
- `FRAMEWORK_STRATEGY.md`/`STATUS.md`/`CHANGELOG.md` 更新。

## 5. 风险

| 风险 | 对策 |
|---|---|
| 测试变成“只检查对象存在” | 检查工具执行契约、顺序、唯一名、错误 binding |
| legal/commercial 夹具太重 | 复用现有 synthetic case/RevisionStore；commerce 只需临时 store |
| 误称完整 conformance | 文档明确本轮是应用契约层，不是全矩阵运行时 |
| 触碰写路径 | 不修改 `RunKernel`/`AgentRuntime` |

## 6. 时间盒

0.5–1 个工作日；如果夹具成本过高，先覆盖 legal DAG + commerce readonly，再补另外两个。

---

## 执行记录

状态：**已完成（F3a）**。

### 改动文件

| 文件 | 内容 |
|---|---|
| `tests/unit/test_application_conformance.py` | 9 个测试：4 个 app 的 spec 契约、工具顺序、错误 binding |
| `docs/guides/CONFORMANCE.md` | 当前覆盖/缺口/新应用接入清单 |
| `FRAMEWORK_STRATEGY.md`、`STATUS.md`、`CHANGELOG.md` | F3a 进展 |

### 覆盖

- legal pilot、legal DAG、commerce、commerce readonly 四个 spec 的身份、policy/verifier、工具唯一性/可执行性。
- 工具顺序：legal `search_case → read_case_page`；commerce 5 个固定工具；readonly `product_lookup`；legal DAG 无工具。
- 错误 application binding 在运行前被 Harness 拒绝。
- 明确记录未覆盖：完整运行时矩阵、写路径、真实模型/律师、optional extras。

### 验证

```bash
uv run --locked pytest tests/unit/test_application_conformance.py -q
# 9 passed

uv run --locked pytest -q
# exit 0；1091 collected

uv run --locked ruff check tests/unit/test_application_conformance.py
# All checks passed

uv run --locked mypy
# Success: no issues found in 119 source files
```

### 未完成 / 下一步

- 完整运行时 conformance：scope/policy/approval/budget/trace/verification 的统一矩阵。
- F1c 写路径实现（当前 ADR 026 gate）。
- 真实模型/律师质量评测、optional extras 安装矩阵。
