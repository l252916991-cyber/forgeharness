# M9-B3 #3：工具异常 → 结构化可行动反馈

> M9-B 闭环之一（工具异常 → 结构化可行动反馈）。总览见 [`M9B_OVERVIEW.md`](M9B_OVERVIEW.md)。

第三个闭环，补上最后一类故障：**工具真正执行失败后，Runtime 怎么给模型一个可行动、可诊断、可回归的反馈。**

**边界（严格限定）**：只做 **Tool Exception → Structured Observation**。**不实现自动 retry / backoff**——那属于 M10 Reliability。

---

## 问题

修复前，工具抛出的宿主异常被 dispatcher 的兜底 `except Exception` 拼成字符串，**连同宿主路径一起**进入模型上下文：

```
tool execution failed: FileNotFoundError: [Errno 2] No such file or directory: './runtime'
```

三个缺陷：

1. 模型拿到的是**不可解析的 Python 异常文本**，不是可行动的反馈；
2. 绝对路径等**宿主环境细节泄漏**进模型上下文；
3. 失败**无法机器分类**，评测与诊断只能靠字符串匹配。

## 设计：两个接口分离

按"给模型看的恢复接口"与"给 Harness/人看的诊断接口"分离：

**模型看到的**（`ToolFailure.as_observation()`，确定性 JSON）：

```json
{"error":{"code":"path_not_found","details":{"path":"./runtime/config.py"},
 "message":"Requested path does not exist.","recoverable":true,
 "suggestion":"Inspect the available paths before retrying."},"ok":false}
```

**Trace 里记录的**（Harness 诊断）：

```
tool.completed
ok=false
error_code=path_not_found
exception_class=FileNotFoundError
recoverable=true
elapsed_ms=...
```

模型侧**不含**：异常类名、traceback、行号、宿主绝对路径。为此 `details` **只回填模型自己传入的参数**（`path` / `query`），**绝不解析异常消息**——否则宿主路径会从消息里漏出去，且消息一改措辞就失效。

## 冻结的小 taxonomy（4 类）

| code | 触发 | recoverable |
| --- | --- | --- |
| `path_not_found` | `FileNotFoundError` / `NotADirectoryError` | true |
| `invalid_arguments` | schema 校验失败 / `WorkspacePathError` / 未知工具 | true |
| `timeout` | `asyncio.timeout` | true |
| `tool_execution_error` | 兜底（未分类） | **false** |

**分类按异常类型，不按消息匹配**：消息可能带宿主路径，且改措辞就会失效。

`recoverable` **不等于**"异常类型可重试"：未知异常一律 `false`——Harness 无法刻画的失败，就不能承诺可重试。

**扩展原则**：以后真实 Bad Case 出现再扩（`permission_denied` / `connection_reset` / …），不预先铺几十种。

## 结构而非字符串

`ToolOutput` 增加 `error: ToolFailure | None`，`ToolFailure` 带 `code / message / recoverable / details / suggestion`。所以：

- **内部**是结构化对象（评测可直接读字段）；
- **trace** 序列化分类字段；
- **模型**只收到渲染后的 observation。

避免了"内部仍是字符串、以后再回头解析"的老路。

## 指标口径

工具**已经真正执行**，因此：

| 指标 | 失败时 |
| --- | --- |
| `logical_tool_calls` | **+1** |
| `tool_attempts` | **+1** |
| `tool_ok` | false |
| `usage.steps` | 正常计 |
| `model_requests` | 不额外变化 |

**不因失败把这次调用从 `tool_attempts` 抹掉**——这也为 M10 的 retry 铺好语义：

```
1 logical_tool_call
→ attempt #1 timeout
→ attempt #2 success
```

## Red / Green

**Red（修复前）**：observation 是 `tool execution failed: FileNotFoundError: ...`

**Green（修复后）**，`RecordedModel` 冻结同一调用验证：

```
tool_ok            = False
observation        = {"error":{"code":"path_not_found","details":{"path":"./runtime/config.py"},
                     "message":"Requested path does not exist.","recoverable":true,
                     "suggestion":"Inspect the available paths before retrying."},"ok":false}
projected error_type = path_not_found
无原始异常文本      = True
无宿主路径泄漏      = True
attempt 仍计入      = True (tool_attempts=1)
```

**这证明的不是"模型突然更聪明"，而是 Harness 把不可控的宿主异常转换成了稳定的 Agent 恢复协议。**

## 负例（必需）

工具抛未分类异常 `RuntimeError("something unexpected happened")`：

```
error.code     = tool_execution_error
recoverable    = false
错误文本是否泄漏 = 否
exception_class = RuntimeError   （仍记录在 trace，供 Harness 诊断）
```

没有这个负例，只是"美化了 FileNotFoundError"，没形成统一机制。

## Live 证据（额外，非必要条件）

真实模型被要求读取一个不存在的文件：

```
action=read_file("runtime/config.py")  → path_not_found（结构化）
action=list_files("runtime")           → path_not_found（结构化）
action=final "I cannot find the file runtime/config.py"
```

模型**收到结构化反馈后自主改换了工具**（`read_file` → `list_files`），证明 observation 是可行动的。这属于额外证据；#3 的成功不依赖它。

## 顺带修正：投影漏掉 `error_code`

`tool.completed` 现在带 `error_code`，但投影的 `_error_type_of` 只看 `error_type`/`error`，导致 `StepRecord.error_type` 为 `None`——trace 有分类而诊断视图没有。已把回退链改为 `error_code` → `error_type` → `error`，使两者一致。

## 退出标准核对

1. [x] `FileNotFoundError` 不再以原始异常字符串进入模型上下文
2. [x] 内部有稳定 `error_code`
3. [x] trace 与 model observation 分层
4. [x] RecordedModel 证明修复前后的 observation 差异
5. [x] 模型可根据结构化反馈执行下一动作（live 实测换用 `list_files`）
6. [x] 未知异常有安全 fallback（`tool_execution_error` + `recoverable=false`）
7. [x] **不实现自动 retry**（留给 M10）
8. [ ] 全量 dev regression 0 结构性退化（见下）

## 全量回归：一个必须如实说明的行为变化

修复后重跑 dev `equal` 全量：

| arm | #3 之前 | #3 之后 |
| --- | --- | --- |
| ReAct | 20/34 | 20/34（不变） |
| Plan-Execute | 19/34 | 18/34 |

差异落在 `long_horizon` / `plan_execute`（2/5 → 1/5），具体是 `long-serial-reads`（`steps:11>max:6`）。

**这不是采样方差——是稳定复现的行为变化。受控实验：**

同一模型、同一预算、同一条用例，6 次采样：

| observation 形式 | 结果 |
| --- | --- |
| #3 结构化 | **11 步 ×6，全部超限** |
| 修复前原始字符串（monkeypatch 还原） | **4 步，通过** |

**因果**：该任务的 fixture 有 `app.py / README.md / pkg/util.py / pkg/main.py`。模型在结构化反馈下**继续尝试**不存在的路径（`/workspace/1.txt` → `1.txt` → `/` → `workspace` → `.` → `workspace/1.txt`），跑了 9 次调用；而修复前它拿到晦涩的原始异常文本就**更早放弃**。

**如何理解**：

- **#3 本身有效**：反馈确实变得可行动（live 实测模型据此改换工具）。
- **代价真实**：更可行动的反馈让模型探索更久，在自然成本上是真实开销。这应当记录，而不是通过放宽用例断言掩盖。
- **根因是模型质量**：任务要求"按顺序读这 4 个文件"，模型却有 9 次调用都在猜路径。它属于 `model_quality`，不是 harness 缺陷——Harness 的职责是给出可用反馈，不是替模型选对路径。
- **它不在 Bad Case 目录**（无 harness gap、无 case design 问题），符合"目录只放可行动项"的规则。

**这条观察对 M8-C 的意义**：observation 形式是可影响模型行为的**实验变量**。因此 `long_serial_reads` 这类用例在跨版本比较时，必须固定 observation 形式或标注版本，否则差异无法归因。已在 `STATUS.md` 记录。

## 未做

- **自动 retry / backoff**：M10 的 `ToolErrorCode` + `recoverable` 已是其前置条件。
- 更细的错误分类：等真实 Bad Case 出现再扩。
