# LangChain/LangGraph：上游能力、本地适配与测量边界

更新日期：2026-10-16。本文件纠正旧版将本仓库轻量适配器的功能范围视为上游能力上限的表述。
旧版本仍保存在 0.5.0 冻结副本中；本次变更不能沿用旧候选审计。

## 对照的层次

ForgeHarness 核心保持独立；`src/forgeharness/langchain_impl/` 是可选的参考实现。
Deep Agents 是基于 LangGraph 的 Harness；LangGraph 是底层状态编排框架，两者不是独立的同层产品。

| 主题 | 上游官方能力 | 本仓库适配范围 | 可作出的测量结论 |
| --- | --- | --- | --- |
| 编排 | LangGraph 状态图、子图、检查点 | 轻量 coding graph 与 RAG 包装 | 只能评价实际配置的实现 |
| 审批与恢复 | 原生 `interrupt` 和 `Command(resume=...)`，配合 checkpointer 持久等待 | 历史 coding adapter 使用自写审批回调 | 回调范围不能证明上游缺少原生审批 |
| 子图 | 官方有完整子图与状态持久化说明 | 本地未实现全部子图模式 | 不将本地未实现或实验状态归于上游 |
| 记忆与上下文 | 状态、跨线程存储及上层 Harness 的上下文能力 | 本地参考链仅接入其选用的历史策略 | 不能将其概括为上游只支持最后 N 条消息 |
| 工具与授权 | 工具及 middleware，可结合宿主授权；Deep Agents 有路径权限 | 本地工具封装实现自己的工作区检查 | 区分框架、宿主授权与执行环境 |
| 追踪与评测 | LangSmith 等生态集成 | 历史检索包装与耗时记录 | 本地日志或测试数不证明领先 |
| 副作用 | 官方说明中断恢复可能重跑节点，之前的副作用需幂等 | ForgeHarness 有审批台账、稳定键与调用日志；上游适配需明确组合 | 应使用相同故障点和台账结果比较 |

来源：[中断与恢复](https://docs.langchain.com/oss/python/langgraph/interrupts)、
[子图](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)、
[Deep Agents 权限](https://docs.langchain.com/oss/python/deepagents/permissions)。
执行比较时以固定版本的实际 API 和源码为准，滚动文档不替代锁文件。

## 既有检索实验

`forge framework-compare` 比较的是本仓库 Native 检索与 LCEL 包装，复用相同检索后端。
旧报告只覆盖当时的资料、查询、模型与源码；不能据其毫秒差值推导通用性能、RAG 质量或安全排名。
比较相同后端的包装开销，也不等于完整业务 Harness 的优劣。

## 新一轮对照

协议：`evals/comparison/protocol.json`。实现：`benchmarks/comparison/`。
当前第一轮包括三套真实 agent loop 的只读/审批恢复验证，以及共同宿主层组合下的确定性故障场景。

- `minimal`：原生 agent loop、原生审批/持久化，加同一业务工具；只测正常只读和跨进程审批继续。
- `equivalent`：各 loop 外接共同宿主契约。权限、来源失效、请求键、审批业务参数和结果核对的保障归于宿主，不能计为任何框架原生优势。
- 取消异常转换属于适配职责；保留初版失败与修复后的证据，不能将配置缺陷直接判为上游缺陷。
- 对原生历史撤回、完整原生故障矩阵、独立接入体验及真实业务效果，本轮保留未测项，不作胜负结论。

运行：

```bash
uv run --no-sync python -m benchmarks.comparison.run --prepare
```

两个上游环境各有独立 `pyproject.toml` / `uv.lock`，虚拟环境和运行状态写入 `reports/framework-comparison/`。
核心依赖不因比较增加；无远程模型调用、无真实资料、无封存集读取。

## 执行隔离边界

工作目录、路径校验和经过筛选的子进程环境不是操作系统级沙箱。`tools/policy.py`
自己写明了这一点：路径前缀与命令规则是纵深防御，不是沙箱。

执行不可信代码的边界现在由 `ContainerBackend`（ADR 044）提供，但它是**可选组合**而不是默认：
`--network none`、只读根文件系统加单一读写挂载、`--cap-drop ALL`、`no-new-privileges`、
非 root 用户、`--memory` 与 `--memory-swap` 相等、`--pids-limit`、`--init`，命令一律
argv 传递。容器只包住“执行仓库代码”这一步：文件工具仍走 bind mount 上的本地后端，
因为它们的威胁是错路径（已拒绝），而不是在别人机器上执行。组合了 `LocalBackend` 的
应用保留原有影响范围。这些标志的强度取决于运行时是否兑现它们，
本阶段的合成读写台账仍不声称验证容器或虚拟机隔离。

## 与本轮补齐项的对照

以下对照的依据是两边实际代码，不是功能清单。四行里只有第一行会改变长任务的**结论**，
其余三行改变的是成本、影响范围和可移植性。

| 主题 | 上游做法 | 本仓库现状 | 差别与可信结论 |
| --- | --- | --- | --- |
| 任务状态 | Deep Agents 的 `write_todos` 写入可变状态键，每轮可改 | `TodoBoard` + `write_todos` 工具，final 前有闸门（ADR 042） | 机制同构。差别在闸门位置：未关闭的 todo 会让 final 失败并附未完成项，连续拒绝超限则 `unfinished_todos` 终止，而不是跑满步数 |
| 完成判定 | `RubricMiddleware` 起独立 grader 子 agent 读 transcript | 确定性 `CodingVerifier` 读 `tool_results` 与工作区（`runtime/verification.py`、`runtime/loop.py`） | 结构同构：都拦截 final、判定、注入反馈、续跑。判定性质不同：本仓库 0 token 且不可被模型说服，但只能判“有没有证据”，不能判“语义完成度”。这是取舍，不是优劣 |
| 前缀成本 | `append_prompt_caching_middleware()` 默认挂载 | `models/caching.py` 的 `CachePlan`，发送时渲染 marker（ADR 043） | 默认关闭（未知网关可能拒绝 `cache_control`）。命中只以 provider 回报为准（`model.cache.observed`），不为忽略该字段的网关声称省钱 |
| 文件后端 | `backends/protocol.py` + 9 个实现，中间件只调 backend | `FileBackend` + local/container/composite 三个实现（ADR 044） | 相同的“工具不直接碰文件系统”约束。实现数量少，够用即可；容器只包命令执行，文件操作仍在本机后端的路径校验下 |

压缩与上下文的两处差异值得单独记：

- 目标常驻。`ContextAssembler(pin_task=True)` 把首条 user 消息变成不可折叠单元，pinned
  状态每轮从可调用对象重读。上游的做法是把塌缩后的摘要留在历史里；本仓库把“用户最初
  要我做什么”移出折叠集合，并且被丢弃的历史写入 `.forgeharness/context/`、摘要里带
  `full: ctx://...`，可用 `read_compacted_context` 回读。
- Skill 自进化。上游有 skill 加载与使用，没有安装闭环；本仓库的 `SkillInstaller` 走
  下载→校验→白名单裁剪→后端内 smoke→原子 promote，同名不同字节默认拒绝覆盖（ADR 045）。
  这条只证明“包能装、能跑”，不证明“指令是好的”。

