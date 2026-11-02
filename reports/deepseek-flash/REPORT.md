# DeepSeek 实测：基线、失败分析与两项改进

日期：2026-11-02（Asia/Shanghai）。模型：`deepseek-flash`（DeepSeek-V4.1-Flash）。

结论：在合成的 Agent Benchmark 上，两项针对失败分析的改进让 **ReAct** 在 dev 集从 24/36 提升到 29/36，在从未用于调整的 holdout 集从 10/14 提升到 12/14，平均步数也下降了。**Plan-Execute 没有可测到的变化。** 样本量小，结论只适用于这个模型和这套合成任务。

## 实验设置

| 项目 | 取值 |
| --- | --- |
| 模型与参数 | `deepseek-flash`，关闭思考模式，temperature 0，单次输出上限 1024 token |
| 评测集 | `evals/agent_cases.json`；dev 36 个可在真实模型上跑的用例，holdout 14 个 |
| 配置 | `natural` 预算（宽松，不让截断左右结果）；每个用例采样 3 次 |
| 判定 | 一个用例 3 次全部通过才算通过（`benchmark_pass`）；另报告样本级通过数 |
| 两种策略 | ReAct（默认运行时）与 Plan-Execute |
| holdout 规则 | 改进前、改进后各跑一次；改进完成前没有查看 holdout 的逐用例结果 |

每个结果文件都在 `model_parameters` 里记录了参数（不含密钥），并绑定了生成它的源码版本。

## 过程

| 步骤 | 源码版本 | 结果文件 | 说明 |
| --- | --- | --- | --- |
| 冒烟测试 | `c5e523b` | 未提交 | 1 个用例，确认连通、计费与密钥不落盘 |
| 第 0 轮（作废） | `c5e523b` | [run0-broken-workspace](run0-broken-workspace/agent-eval-dev-natural.json) | 文件工具全部失败，见下文「顺带发现的缺陷」 |
| 基线 | `f2028ee` | [dev](baseline/agent-eval-dev-natural.json)、[holdout](baseline/agent-eval-holdout-natural.json) | holdout 基线在 `67d8856` 上运行，评测行为与 `f2028ee` 相同 |
| 失败诊断 | `67d8856` | [dev，仅 ReAct](diagnose/agent-eval-dev-natural.json) | 加 `--keep-all-failures`，保存全部失败 trace；同时作为第二次 ReAct 基线 |
| 改进后 | `df4d035` | [dev](improved/agent-eval-dev-natural.json)、[holdout](improved/agent-eval-holdout-natural.json) | 参数与基线完全相同 |

## 结果

| 指标 | 基线 | 改进后 |
| --- | ---: | ---: |
| ReAct dev 用例通过 | 24/36（两次均为 24） | **29/36** |
| ReAct dev 样本通过 | 74/108、76/108 | **87/108** |
| ReAct holdout 用例通过 | 10/14 | **12/14** |
| ReAct holdout 样本通过 | 32/42 | **36/42** |
| ReAct dev 平均步数 | 3.64、3.28 | 2.89 |
| ReAct dev input token（各用例中位数样本之和） | 146,560、83,493 | 74,946 |
| Plan-Execute dev 样本通过 | 66/108 | 66/108 |
| Plan-Execute holdout 样本通过 | 25/42 | 24/42 |

- 基线的 146,560 个 input token 里，有 79,306 个来自一个用例：模型把同一个错误的 `write_todos` 调用连发了 20 次。和不含这次失控的诊断轮相比，改进后的 token 下降约 10%。
- 统计上：dev 样本级 150/216 对 87/108，Fisher 精确检验 p ≈ 0.034；只拿第一次基线比较，p ≈ 0.060。holdout 32/42 对 36/42，p ≈ 0.41，样本太少，不能单独支持结论。同一用例的 3 次采样彼此相关，实际有效样本比表面上少。用例级翻转：dev 5 个变好、1 个变差，holdout 2 个变好、0 个变差，符号检验 p ≈ 0.07。

## 失败分析（诊断轮，ReAct 的 12 个失败用例）

| 类别 | 用例数 | 证据 | 处理 |
| --- | ---: | --- | --- |
| ① 一次返回多个工具调用 | 5 | DeepSeek 不理会 `parallel_tool_calls: false`，一次返回 2–4 个调用。运行时只允许一步一个，整条回复被拒，耗掉一步。`long-five-step` 唯一超出的那一步就是被拒的回复 | **改进 A** |
| ② 参数报错不说哪里错 | 2 | `write_todos` 的 `rationale` 超过 300 字符，报错只有「不符合 schema」。模型把同一个错误调用连发 4 次（基线轮 20 次） | **改进 B** |
| ③ 用例设计问题 | 约 5 | 见下文「没有调整的部分」 | 不调，如实记录 |
| ④ 模型绕过 todo 门禁 | 1 | 本应失败的任务，模型把待办标成「已完成」，内容写「已放弃」，通过了门禁 | 不调，作为治理发现记录 |

## 两项改进

**A. 每次请求都写明「一步只能调一个工具」**（[ADR 048](../../docs/adr/048-pinned-protocol-notice.md)）。这条规则一直由运行时强制执行，但只通过一个请求参数传达，而 DeepSeek 不理会这个参数。现在运行时会在每个带工具的请求里固定放一段说明，内容只陈述它强制执行的规则，不提任何工具或任务。这段说明会记入每次请求的 trace，但不进入对话历史，所以 checkpoint、恢复和上下文压缩都不受影响。

**B. 参数格式错误时指出字段和规则**（[ADR 047](../../docs/adr/047-field-level-schema-feedback.md)）。报错现在会带上 `{参数位置: 违反的约束}`，比如 `rationale: String should have at most 300 characters`，但不回显模型提交的值。外部 JSON Schema 的错误根据关键字自行生成说明，不使用会引用原值的校验器原文。观察契约版本 `OBSERVATION_CONTRACT_VERSION` 因此升为 2。

改进后的 trace 证据：

- 一次返回多个调用：诊断轮 12 个失败 trace 里有 5 个出现；改进后 9 个 ReAct 失败 trace 和 27 个 Plan-Execute 失败 trace 里都是 0 次。
- 参数报错：在 [`long-todo-gate-cleared`](improved/bad-cases/natural-react-long-todo-gate-cleared-s0.json) 里，模型收到 `rationale` 超长的提示后，下一次调用就改对了。这个用例仍然失败，原因是用例设计（见下文）。

用例翻转（ReAct）：

| 用例 | 基线 → 改进后 | 解读 |
| --- | --- | --- |
| `long-five-step`、`long-alternate-list-read`、`long-serial-reads` | 0–1/3 → 3/3 | 与改进 A 一致：诊断轮里这三个用例的失败样本都含被拒的多调用回复，并因此超出步数 |
| holdout `long-holdout-four-step` | 0/3 → 3/3 | 同属长任务，但基线没有保存它的 trace（当时未加 `--keep-all-failures`），机制未核实 |
| `multi-list-read-write`、`retr-find-then-write` | 0/3 → 3/3 | **不是针对性修改的目标**。原先模型在回复里给出报告，而没有调用 `write_file`。两次基线都是 0/3，不像随机波动；机制可能是协议说明让模型更倾向于用工具完成动作，但没有做消融，未验证 |
| `long-six-decisions` | 1/3 → 0/3 | 任务本身含糊（「在工作区里交替做标记和读取」），模型做了更多调用而超出上限；诊断轮也是 0/3，在这个用例的波动范围内 |
| holdout `notool-holdout-a` | 2/3 → 3/3 | 与改进无关，可能是随机波动 |

**Plan-Execute 为什么没变**：它的失败主要是重复调用，比如同一个目录列两次、同一个词搜三次，以及计划步骤带来的额外开销，两项改进都没有针对这些。用例级从 20/36 变成 16/36，但样本级保持 66/108，属于用例级判定在 3 次采样下的波动。

## 没有调整的部分

这些用例的失败来自用例或框架设计，针对它们修改会变成对着评测集调参，所以只记录：

- `recover-error-retry`：注入的「临时故障」是框架无法分类的异常，被标为 `recoverable: false`。模型照这个提示没有重试，反而按用例期望判失败。这说明框架的错误分类缺少「临时故障」这一类，真实场景里的网络抖动也会被报告成不可恢复。值得后续单独设计，但不应为这个用例改。
- `recover-second-attempt-fails`：任务只需读一次文件，用例却期望读三次。
- `ambig-nested-path`、`long-todo-gate-cleared`：任务提到的文件名或位置与工作区不符，又没有列目录的工具，模型只能猜。
- `long-todo-unfinished-fails`：模型改写待办状态绕过了门禁。待办门禁只能检查状态，无法判断内容是否属实，这是它的固有边界。

## 顺带发现并修复的缺陷

1. **评测报告会写入 API key**（`6001a53`）。真实模型评测的报告会把整份模型配置序列化进去，包括 `api_key`，而报告是要提交到仓库的。以前用本地 OMLX，写入的只是占位符，所以没暴露。
2. **macOS 上所有文件工具失败**（`f2028ee`）。评测用 `tempfile.mkdtemp` 创建工作区，macOS 返回的路径在 `/var` 下，而 `/var` 是指向 `/private/var` 的符号链接，被文件后端的 no-follow 检查拒绝。第 0 轮因此得到 ReAct 12/36、Plan-Execute 5/36 的无效结果。Linux CI 的 `/tmp` 是真实目录，从来测不出这个问题。
3. **没设密钥时请求远程端点返回 401**（`c5e523b`）。变量为空时程序会退回本地占位 key，被记成模型失败；现在改为请求前直接报错。

## 局限

- 只测了一个模型，任务是合成的小任务，不代表真实业务效果。
- 没有做消融，无法把提升拆分到 A 和 B 各自身上。
- 每个用例只采样 3 次；holdout 只有 14 个用例。
- 改进 B 只覆盖本地调度路径；PostgreSQL 托管路径仍返回固定报错文本，原因见 ADR 047。

## 复现

```bash
read -s "FORGE_API_KEY?DeepSeek key: " && export FORGE_API_KEY
uv run --no-sync forge eval-agent --split dev --profile natural \
  --model deepseek-flash --base-url https://api.deepseek.com --samples 3 \
  --keep-all-failures --output reports/deepseek-flash/<run>/agent-eval-dev-natural.json
```

把 `--split dev` 换成 `--split holdout` 即可跑 holdout。每个结果目录下的 `bad-cases/` 保存失败样本及其哈希链 trace。
