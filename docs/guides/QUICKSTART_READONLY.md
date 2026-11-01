# 首次接入：注册一个只读工具

2026-10-26。Python 3.12，SDK API v1。以下步骤是作者自测流程；没有独立开发者耗时记录。
目标 30–60 分钟是待验证的可用性假设。

1. 用 `uv venv --python 3.12 .venv` 创建应用自己的环境。
2. 安装明确选定的 wheel：`uv pip install --python .venv/bin/python /absolute/path/forgeharness-0.8.5-py3-none-any.whl`。
   对照其 SHA-256 与候选 `verification/summary.json`；需要可复现依赖时使用候选导出的 hash requirements。
3. 将下面代码保存为应用目录中的 `demo.py`，运行 `.venv/bin/python demo.py`。
   它不需要模型密钥，只在当前目录 `.forge-onboarding/` 保存合成状态。
4. 应看到 `succeeded hello from a registered tool` 和有效 trace 路径。
   重启后可用相同 ApplicationSpec、RunContext 和 state_dir 调用 `inspect` 查询。
   此例验证 SDK 接线，不测模型质量。只读工具也必须声明 `READ_ONLY` 效果类别。

```python
import asyncio
from pathlib import Path

from pydantic import BaseModel

from forgeharness.sdk import (
    Actor,
    ApplicationSpec,
    FinalAction,
    Harness,
    ModelResult,
    PolicyDecision,
    PolicyDecisionType,
    ResourceScope,
    RiskLevel,
    RunContext,
    RunStatus,
    ToolAction,
    ToolCall,
    ToolEffectClass,
    ToolOutput,
    ToolSpec,
    NoopVerifier,
    verify_trace,
)


class Input(BaseModel):
    pass


class GreetingTool:
    input_model = Input
    spec = ToolSpec(
        name="greeting",
        description="Read a synthetic greeting",
        input_schema=Input.model_json_schema(),
        risk=RiskLevel.READ,
        effect_class=ToolEffectClass.READ_ONLY,
    )

    async def execute(self, arguments, context):
        return ToolOutput(ok=True, content="hello from a registered tool")


class LocalModel:
    async def decide(self, request):
        if request.messages[-1].role.value == "tool":
            return ModelResult(action=FinalAction(content=request.messages[-1].content))
        return ModelResult(
            action=ToolAction(call=ToolCall(id="greet", name="greeting", arguments={}))
        )


class ReadPolicy:
    def evaluate(self, request):
        return PolicyDecision(
            type=PolicyDecisionType.ALLOW
            if request.spec.risk is RiskLevel.READ
            else PolicyDecisionType.DENY,
            reason="synthetic read only",
        )

    def can_approve(self, **kwargs):
        return False


async def main():
    application = ApplicationSpec(
        id="hello",
        version="1",
        policy_version="1",
        model_factory=LocalModel,
        tools=(GreetingTool(),),
        policy=ReadPolicy(),
        verifier=NoopVerifier(),
    )
    context = RunContext(
        application_id="hello",
        application_version="1",
        policy_version="1",
        run_id="hello-1",
        session_id="hello",
        correlation_id="hello",
        actor=Actor(id="local-user"),
        scope=ResourceScope(tenant_id="synthetic"),
    )
    harness = Harness(application, state_dir=Path.cwd() / ".forge-onboarding")
    result = harness.find(context)
    if result is None:
        result = await harness.run(context=context, task="Read the greeting")
    assert result.status is RunStatus.SUCCEEDED
    assert verify_trace(harness.trace_path(context)).valid
    assert harness.inspect(context).final_output == result.final_output
    print(result.status.value, result.final_output)
    print(harness.trace_path(context))


asyncio.run(main())
```

接入者记录表（待真人填写）：开始时间、首次成功时间、Python/OS、wheel 摘要、失败命令、
文档卡点、是否需要阅读框架源码、是否需要修改框架核心。不得代填为独立验证。

从只读示例迁移到业务应用时，将真实身份/权限校验接到 `authorize_run`、策略和资源回调，
领域工具留在宿主。`NoopVerifier` 仅适用于本例接线；实际成功判据由宿主 verifier 给出。
需要写入、审批与恢复时先选择 ReAct 或显式 `DurableOrchestrator`，阅读
[R2 契约](R2_ORCHESTRATION.md)。任意 one-shot kernel 仍只读、不可恢复。
升级 wheel 前分别运行各宿主回归；0.5.0 的电商与 0.8.3 的法律宿主绑定不因本例自动迁移。
R2 的完整运行身份包含框架版本；旧持久运行继续使用原绑定 wheel，新版本不能隐式接管。
