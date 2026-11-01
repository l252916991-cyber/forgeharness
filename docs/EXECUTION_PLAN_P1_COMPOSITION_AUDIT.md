# P1 执行计划：组合审计 + 剩余应用迁移 + 覆盖率补口

> 状态：已完成（2026-10-20）。
> 依据：2026-10-20 改进审查 P1-9/P1-10/P1-11，及 P2 的两处最小修正。
> 目标：让“一次 run 用了哪些能力”可审计；让所有参考应用走同一组合 API；把 lexvault 适配器的错误分支测满。

## 1. 范围

| 项 | 内容 | 完成标准 |
|---|---|---|
| P1-9 组合审计 | `ApplicationComposition`（capability 名单 + 结构 digest）写入 `ApplicationSpec`；`Harness` 在 run trace 里发 `application.composition` 事件 | digest 对相同组合稳定、对顺序/内容变化敏感；直接构造的 spec `composition=None` 且不发事件 |
| P1-10 迁移 | `applications/records.py`（2 处）、`commerce/consultation.py`（1 处）、`coding/application.py`（1 处）改用 `compose_application` | 行为不变；现有 records/consultation/coding 测试全绿 |
| P1-11 覆盖率 | lexvault adapter：`cases()` 未授权、search 输入边界、search/page_drafts 跳过未授权页、`resources()`、`iter_pages()`；dag：`ReadOnlyDagPolicy` deny/allow、verifier 空答案、Critic 非 Final 动作 | 新增测试通过；adapter/dag 覆盖率明显提升 |
| P2-lite | `pyproject.toml` description 去 Coding 中心化；README「公开 SDK」列表补 Capability/Orchestrator/RunKernel/CONFORMANCE | 与现状一致，不重写 README |
| ADR | `docs/adr/028-application-composition-audit.md` | 记录 digest 的语义边界（结构摘要，不是实现内容哈希） |

## 2. 明确不做

- 不拆 `sdk/application.py`、不抽 `RunKernel`/`AgentRuntime` 共享执行器（与 F1c 同轮做，避免双改）。
- 不做 RunKernel rejected-token 计费（审查 P0-4，归入 F1c）。
- 不重写 README 叙事（P2 另轮）。
- 不改 `compose_application` 的合并语义（app 级 instructions 在前、capability 依序）。

## 3. 设计

### 3.1 ApplicationComposition

```python
@dataclass(frozen=True)
class ApplicationComposition:
    capability_names: tuple[str, ...]
    digest: str
```

- `digest = sha256(canonical JSON(capability 名单, 工具名序列, instruction 文本序列, context provider 类型名序列))`。
- **结构摘要**：证明“组合形状”一致，不哈希 provider/tool 的实现代码；实现变更仍需宿主升级 application version（既有规则不变）。
- `ApplicationSpec` 新增可选字段 `composition: ApplicationComposition | None = None`（默认 None，向后兼容；`dataclasses.replace` 不受影响）。
- `Harness._run` 在 trace 开头（两条执行路径共享处）发 `application.composition` 事件；`composition is None` 时不发。

### 3.2 迁移映射

| 调用点 | Capability | 备注 |
|---|---|---|
| `records.record_application` | `record-read`（PublishedRecordTool） | instructions 留 app 级，元组顺序不变 |
| `records.cited_draft_workflow` 的 write spec | `cited-draft`（CitedDraftTool） | budget 复用 source.budget |
| `consultation.consultation_application` | `consultation`（ConsultationTool + HandoffTicketTool） | 工具顺序不变 |
| `coding.coding_application` | `coding-tools`（coding_tools + RepositoryContext） | instructions 留 app 级 |

### 3.3 覆盖率补口

只测公开行为：adapter 的授权/边界分支、policy 的 deny/allow、verifier 空答案、orchestrator 对非 Final 动作 fail-closed。不测私有方法。

## 4. 测试计划

1. composition：名单顺序、digest 确定性（等价组合相同）、顺序变化 digest 变化、直接构造为 None。
2. trace：组合应用 run 后 trace 含 `application.composition` 与 digest。
3. 迁移回归：records/consultation/coding/workflow 现有测试全绿（不改断言）。
4. lexvault：新增约 8 个分支测试。
5. 全量 + ruff + mypy。

## 5. 风险

| 风险 | 对策 |
|---|---|
| 新字段破坏 `replace()`/相等性用法 | 带默认值追加在末尾；全量回归 |
| digest 被误解为实现哈希 | ADR 028 + docstring 明确“结构摘要” |
| 迁移改变 instructions/tools 顺序 | 逐点保持原元组顺序；现有断言不动 |
| trace 事件泄露敏感内容 | 事件只含 capability 名与 digest，不含 instructions 文本 |

## 6. 时间盒

0.5–1 个工作日；若 coding 迁移触发工作区相关测试波动，先迁移 records/consultation，coding 单独补测。

---

## 执行记录

状态：**已完成（P1 批次）**。

### 改动文件

| 文件 | 内容 |
|---|---|
| `src/forgeharness/sdk/application.py` | `ApplicationComposition`；`ApplicationSpec.composition`（可选，末位默认 None）；`Harness._run` 发 `application.composition` trace 事件（两条执行路径共享） |
| `src/forgeharness/sdk/capability.py` | `compose_application` 计算结构 digest 并写入 composition |
| `src/forgeharness/sdk/__init__.py` | 导出 `ApplicationComposition` |
| `src/forgeharness/applications/records.py` | 两处 spec 迁移到 `compose_application`（`record-read` / `cited-draft`） |
| `src/forgeharness/applications/commerce/consultation.py` | 迁移（`consultation`） |
| `src/forgeharness/coding/application.py` | 迁移（`coding-tools`，含 RepositoryContext provider） |
| `pyproject.toml` / `README.md` | description 去 Coding 中心化；SDK 列表补 Capability/Orchestrator/RunKernel/CONFORMANCE |
| `tests/unit/test_capability_composition.py` | +2：composition 确定性/敏感性；直接构造为 None；harness 测试补 trace 断言 |
| `tests/unit/test_lexvault_dag.py` | +6：adapter 授权面、search 边界、未授权页跳过、policy allow/deny、verifier 空答案、非 Final 动作 fail-closed |
| `docs/adr/028-application-composition-audit.md` | digest 语义边界（结构摘要 ≠ 实现哈希） |
| `docs/STATUS.md`、`docs/FRAMEWORK_STRATEGY.md`、`CHANGELOG.md` | 进展同步 |

### 验证

```bash
uv run --locked pytest <9 个相关测试文件> -q
# 122 passed（迁移零回归：records/consultation/coding/legal/commerce 断言未改）

uv run --locked pytest -q
# exit 0；1117 collected（+8）

uv run --locked pytest tests/unit/test_lexvault_*.py --cov=...lexvault
# adapter 78%→98%，bundle 90%→92%，dag 88%→99%，exporter 86%→90%，合计 96%

uv run --locked ruff check <本轮目录/文件>   # All checks passed
uv run --locked mypy                          # Success: 119 source files
```

未覆盖残留（有意保留）：`adapter._page` 的不可达 miss 分支、exporter 的“数据库不存在/超 100 页”入口（已有直接单测覆盖其一）、bundle 重复 locator 分支。

### 未完成 / 下一步

- F1c：抽取 `AgentRuntime` 与 `RunKernel` 共享的 model/tool 执行器；一并处理 RunKernel 失败计费（审查 P0-4）与写路径。
- `sdk/application.py` 拆分（与 F1c 同轮，避免双改）。
- README 全面重写（叙事仍是 Coding 优先，本轮只改了 SDK 列表与主工作流一句）。
- 真实模型 + 律师复核评测。
