# 当前框架候选的冻结与验证

2026-10-26 固定专家协作源码为 0.8.6，使用新的独立目录：

```bash
uv run --no-sync python -m benchmarks.comparison.verify --candidate-dir reports/multi-agent-20261026/candidate-r1 --freeze-only
uv run --no-sync python -m benchmarks.comparison.verify --candidate-dir reports/multi-agent-20261026/candidate-r1
```

本轮包含协作三进程示例的源码/干净副本/最小 wheel 门禁；结果与残余边界读取
`reports/multi-agent-20261026/REPORT.md`。下文保留 0.8.5 的历史入口和证据定位。

2026-10-26 更新。适用源码候选：ForgeHarness 0.8.5；SDK API v1。
当前状态见 [能力索引](../CURRENT_CAPABILITIES.md)。2026-10-23 的工程证据属于
`reports/release-0.8.3-r1/`；本次模型调用工具目录控制及评测接线修复使用独立候选
`reports/improvement-20261026/candidate-r3/`，不继承旧通过结果。
candidate-r2 的 0.8.4 通过证据保持原样，不覆盖本次运行时修改。
首轮 candidate-r1 的新依赖公告失败保留；修复与版本变更见 [依赖审计记录](DEPENDENCY_REFRESH_2026-10-26.md)。
历史 session/business/P0-P1 冻结与 0.5.0 + Unreleased 的 `reports/framework-next/`
冻结保留原目录，只覆盖其记录的源码。

## 当前入口

在项目根目录运行，Python 3.12、`uv`、`rg` 必需：

```bash
uv sync --python 3.12 --extra dev --extra cli --extra platform --extra benchmark --extra frameworks --frozen
uv run --no-sync python -m benchmarks.comparison.verify --candidate-dir reports/improvement-20261026/candidate-r3 --freeze-only
uv run --no-sync python -m benchmarks.comparison.verify --candidate-dir reports/improvement-20261026/candidate-r3
```

`--freeze-only` 只保存源码身份，不运行门禁。省略冻结标志时必须已有相同源码的清单。
既有 `--freeze` 仍表示冻结并验证；旧默认目录 `reports/next-candidate` 保留兼容，
当前 CI 使用本轮独立 candidate-r3 目录，本轮不改写旧阶段证据。

候选目录必须在本工作区 `reports/` 之下；解析 `..` 和符号链接后仍执行同样的约束。
目录中的源码清单一旦存在，不同源码即使再次要求冻结也会被拒绝。若需要新候选，
先审查变更，再选择新的独立子目录，例如 `reports/framework-next-r1`。
不要删除旧清单或借用旧通过结果。

## 如何读取结果

- `source.json`：逐文件 SHA-256 与源码集合摘要；无 Git 元数据时使用 archive 身份，不补造提交。
- `verification/runs/<attempt>/`：本次独立命令日志、JUnit、覆盖率、依赖报告和构建产物。
- `verification/summary.json`：最近一次尝试的索引；历史尝试保留在各自目录。
- `qualified=true`：本驱动的工程范围通过；同时核对 `source_unchanged`、全部命令和构建产物。

驱动复用已有完整验证：源码与干净副本的质量检查、纯分支覆盖率 ≥85%、
keyless control/Agent dev、法律/电商开发契约、安全场景、依赖审计、
固定版本的 33 个共同宿主组合场景，以及 wheel/sdist/最小安装。
它不打开终评封存正文，不认证真实模型质量、专业法律结论或生产部署，
也不证明原生框架优劣。

本轮修复前的观察保存在 `reports/framework-next/r0/`：源码清单、测试 nodeid 和缓存对照。
2026-10-20 的实际收集为 1116 项；旧 pytest 缓存为 1123 项，包含 7 条已不属于当前采集的名称。
随后新增验证入口回归测试会改变测试总数；新的总数从对应候选的 JUnit 报告读取。

0.6.0 的首次冻结与其 83/87 尝试保留在 `reports/release-0.6.0/`：四个失败门禁
（source/clean 的 comparison-audit-deepagents 与 comparison-audit-pydantic）来自两个
独立对照环境锁文件仍锁定 certifi 2026.7.22（PYSEC-2026-4320），根锁升级覆盖不到它们。
升级两个环境锁后，修正的候选冻结为 `reports/release-0.6.0-r1/`；首轮清单与尝试
按规约保留，不删除、不覆盖。

## 边界与后续工作

SDK 的 ReAct 与显式 R2 图具备持久审批与 journal 恢复；任意自定义 one-shot kernel
仍只读，审批/恢复接口在该路径明确拒绝。三个 Coding API v1 兼容 shim 与 SDK application
re-export shim 保留；共享模型调用与 SDK 模块拆分已完成。

当前资料预算仍使用启发式计数；它不构成真实 tokenizer 上界。可选 frameworks profile
保留 FH-019 的 NLTK 审计例外，每个候选都重新审计；例外不适用于核心或其他漏洞。
历史 FH-015–018 的范围与当前 SDK 分别记录，不能自动从任一通过标志关闭。
完整剩余工作见 [完善计划](REMAINING_WORK_PLAN_2026-10-20.md)。
