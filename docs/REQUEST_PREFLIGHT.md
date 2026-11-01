# 完整模型请求预算预检

2026-10-17。本轮覆盖公共 SDK 的 Harness、直接 AgentRuntime 和 PlanExecuteRuntime 的规划/执行阶段。
旧 knowledge 应用的 OMLXChatModel.complete、独立 RAG/chat 调用不经过这些运行时，不在本次覆盖范围。

最终审查同时修复了旧的重复调用编号异常：模型在一次运行中重新使用既有 tool call ID
会得到可追踪的 failed 终态，不会重新执行，也不会因 journal 冲突留下 running。
工具执行内部的安全重试仍使用同一个逻辑编号，不受此模型新动作校验影响。

## 当前行为

每次调用先准备不可变 JSON 快照，再执行预算预检，最后把同一份快照交给模型适配器。
OpenAI-compatible chat-completions 适配器发送快照的 UTF-8 字节，不在通过预检后重建请求体。
认证头和 endpoint 不属于模型输入，不进入快照或预算 trace。

预检计入：

- 完整序列化消息，包括系统消息、工具调用参数和工具结果。
- 当前实际揭示的全部工具定义；下一步重新取得并计算，不能沿用旧 schema 成本。
- 模型、temperature、max_tokens 等适配器生成的请求选项与 JSON 包装。
- 宿主配置的额外开销，加适配器声明的额外开销。
- 宿主输出预留与适配器 max_tokens 的较大值。

准入限额是 `RunBudget.max_context_tokens` 与适配器明确声明的模型窗口中的较小值。
没有窗口声明时使用宿主限额，并记录 `provider_window_known=false`；不会查询注册表后猜测模型配置。

预留会压缩可供历史消息使用的空间。保留系统消息与最近完整轮次，最多重新准备三次；
仍不能容纳时明确失败，不发送该模型调用，不增加该次模型用量，也不自行删除工具、
截断必需材料或扩大限额。准备过程应为纯本地、无副作用操作。

## 配置

```python
from forgeharness.sdk import RunBudget

budget = RunBudget(
    max_context_tokens=8_000,
    reserve_output_tokens=512,
    request_overhead_tokens=128,
)
```

chat-completions 配置可显式设置 `max_tokens`、`context_window_tokens` 和
`provider_overhead_tokens`。只有 `max_tokens` 实际作为生成上限发送；宿主的
`reserve_output_tokens` 是窗口预留，不会自行改变模型参数。
累计 `max_input_tokens/max_output_tokens` 仍按模型返回的实际 usage 检查，
本次不把请求窗口估算变成收费、HTTP 重试次数或累计用量的测量。

## 兼容现有 Model

原有 `Model.decide(ModelRequest)` 无需改签名。运行时对其完整公共输入做 JSON 快照和预检。
由于不知道第三方模型内部还会增加什么包装，相关窗口、输出和额外开销标为未知。
这与确认远程实际请求体不同，不能将 generic profile 说成完整的远端 wire 证明。

需要声明适配器实际请求的模型可实现公共 `PreparingModel` 协议：

1. `prepare_request(request) -> PreparedModelCall`：返回已校验的原请求、规范 JSON 请求体、格式标识和可选窗口/输出/开销声明。
2. `decide_prepared(prepared)`：发送已经通过预检的快照，不重新拼装输入。

内置 `model-request-v1` 要求载荷与 ModelRequest 一致；`chat-completions-v1` 同时核对
消息、工具、输出上限。通用准备入口拒绝改写原始运行请求；自定义格式的额外映射仍由
可信适配器负责。SDK 先解析 JSON 再应用宿主隐私转换检查，防止凭据键藏在序列化字符串中。

`PreparedModelCall` 保存不可变 JSON 字符串，读取 request/payload 返回新对象。
原工具 schema 或返回的 payload dict 随后改变，不会改变已准入的快照。

## 证据与限制

成功准入的 `model.request` 包含公共输入、`prepared_payload` 和 `preflight`：
完整输入估算、消息/工具/选项分项、输出预留、附加开销、采用的限额、未知标记与 payload SHA-256。
拒绝候选记录为 `context.preflight_rejected`，不会伪造一次实际模型调用。

默认估算器为每四字符一个 token 的启发式。JSON 中包含输入的全部声明字段，
**不代表其估算值是真实 tokenizer 的上界**，尤其不能据此保证中文或不同 chat template 的精确容量。
分项为独立估算，取整后不一定相加等于完整载荷的估算；完整载荷的数值决定准入。
窗口和额外开销是宿主/适配器声明，未通过真实服务探测验证。

准备后的 payload/trace 一致性已通过 HTTP MockTransport 验证；第三方 transport、中间代理或
提供商在接收后追加的内部内容仍不可见。三次准备上限可能保守拒绝本可用其他策略容纳的对话，
没有最佳装箱或自动寻找所有压缩方案的承诺。已有模型真实用量与副作用恢复规则保持独立。

设计见 [ADR 020](adr/020-prepared-model-request-budget.md)，工程验收见
`reports/request-preflight-2026-10-17/verification/summary.json`。
