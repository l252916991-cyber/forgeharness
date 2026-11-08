# 参与开发

## 环境

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。

```bash
make sync            # 安装开发、CLI、平台、基准与 PostgreSQL 测试依赖
uvx pre-commit install   # 可选：提交前自动格式化和检查
```

## 日常命令

| 命令 | 作用 |
| --- | --- |
| `make format` | 格式化并自动修复 lint |
| `make lint` | 格式与 lint 检查（不改文件） |
| `make typecheck` | mypy 严格模式 |
| `make test` | 全量测试、覆盖率，以及真实 PostgreSQL 控制面测试 |
| `make gate` | 发布前门禁：以上全部 + 无密钥评测、CLI 冒烟、锁文件和依赖漏洞审计 |
| `make framework-check` | 可选的 LangChain / LangGraph 对照实现 |

CI 在每次推送时运行 `make gate`，并冻结当前源码、在干净副本上完整复验一遍。

## 约定

- 仓库的产品不变量见 [AGENTS.md](AGENTS.md)：运行时状态、预算和审批不能只存在于模型对话里；
  模型输出和工具参数一律视为不可信；每个模型可见输入和工具副作用都要能从 trace 还原。
- 每个行为变更和缺陷修复都要带测试；修缺陷时先写能复现问题的测试。
- 不平凡的架构决策写进 `docs/adr/`，编号递增，记录背景、决定和后果。
- 公开 API 只从 `forgeharness.sdk` 导出；参考应用只能依赖公开 SDK，`tests/unit/test_import_boundaries.py` 会检查。
- 实验和评测必须先在 dev 集调通、封存方案，再在正式集上只跑一次；holdout 不用于调参。
- 不提交模型密钥、令牌或任何真实凭据；trace 和报告在写入前会脱敏，评测报告不能包含 API key。

## 提交信息

使用 `类型(范围): 说明`，例如 `fix(runtime): settle definitive provider refusals`。
常用类型：`feat`、`fix`、`docs`、`test`、`refactor`、`chore`、`evidence`（实验证据）。

## Pull Request

提交前确认：

- `make lint typecheck` 通过，相关测试通过；
- 行为变化已写进 `CHANGELOG.md` 的未发布部分；
- 新的设计取舍有 ADR；新的能力或边界已同步到 `docs/CURRENT_CAPABILITIES.md`。
