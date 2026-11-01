# 首轮接入样例与测量边界

本轮是维护者自动化自测，没有独立接入开发者；主动开发工时、首次成功时间没有测量结果。

两类样例使用同一合成业务工具：授权只读、独立审批写入并在新进程继续。三套真实 agent loop 分别位于 `benchmarks/comparison/adapters.py`，共同宿主位于 `host.py`，判分与运行脚本位于 `run.py`。每个 action 都是新进程，正常审批控制会核对不同 PID、模型真实输入和实际业务效果。

运行所有样例与故障：

```bash
uv run --no-sync python -m benchmarks.comparison.run --prepare
```

只用本机 SDK 运行：

```bash
uv run --no-sync python -m benchmarks.comparison.run --engines forge
```

`--prepare` 使用各环境的固定锁文件创建隔离环境，不修改根项目依赖。输出与状态位于 `reports/framework-comparison/`，已有 attempt 不覆盖。无真实模型或服务凭据要求。

共同宿主需要当前授权/版本检查、稳定请求、独立审批及业务结果核对；它们都显式写在宿主代码中。上游 loop 使用其公开工具、审批和持久化 API。Pydantic 额外保存 pending message history，不能将这部分宿主存储当成无需集成的原生服务。

`onboarding.json` 记录物理行数与基础设施清单，但不同 API 的代码行数不等于功能完整性、工时或质量。本轮不据这些数字评价开发者效率。

下一次独立试验：安排未参与这些接口实现的开发者，分别从文档完成两类接入；记录已有经验、顺序、额外配置、错误、主动时间与最终契约结果。无人参与前维持“独立接入未测”。
