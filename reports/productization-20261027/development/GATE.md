# 开发门禁记录

执行日期：2026-10-27；工作区：`~/Documents/多agent协作`。

本记录对应尚未冻结的产品化工作树，不替代 checkout-equivalent 候选资格报告。

## 结果

`make gate` 在最后一次代码修改后退出码为 0：

- Ruff format/check：403 个文件格式合格，lint 通过。
- mypy：131 个源码文件无问题。
- pytest：1339 项测试被收集，全套覆盖测试退出码为 0。
- 覆盖率：statement 93.49%，纯 branch 85.97%（门槛 85%），综合 91.93%。
- keyless control：7/7 通过。
- keyless Agent 开发集：ReAct 38/38、Plan-Execute 38/38，契约检查通过。
- CLI、framework comparison CLI、`uv lock --check`：通过。
- core 与 all-extras 依赖审计：无已知未处理漏洞；all-extras 继续忽略已有
  `PYSEC-2026-3740`，依据 `docs/review/FINDINGS.json` 的 FH-019，没有新增例外。

产品化新增的业务安全选择单独执行，69/69 通过。它包含无商品通用人工转接、support
访问控制、备份互斥、备份摘要、同路径恢复和待审批恢复后单次副作用。

合成业务开发契约 120/120 通过（法律 40、电商 80），但报告明确
`measurement_valid=false`、`business_qualified=false`，不能作为真实业务质量结论。

## 机器可读证据

- `reports/coverage.json`
- `reports/control-eval.json`
- `reports/agent-eval-dev-keyless.json`
- `reports/productization-20261027/development/business-dev.json`
- `evals/business/safety.json`

## 后续候选结果

随后已将 SDK 0.9.2 冻结为产品化 candidate-r1。源码和 checkout-equivalent 干净副本资格驱动均通过；权威结果见 `../candidate-r1/verification/summary.json`。旧候选没有被复用或覆盖。
真实宿主、真实接口、目标模型、独立使用者和业务终评仍未具备。
