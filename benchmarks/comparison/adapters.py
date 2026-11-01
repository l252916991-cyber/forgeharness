"""Real optional native agent loops with equivalent deterministic model decisions.

Public upstream APIs only. No external model, tools, telemetry or provider keys.
State/approval support is native; the business host around it is explicitly shared.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from benchmarks.comparison.host import Ledger


class Forge:
    async def run(
        self, db: Ledger, *, request_id: str, operation: str, payload: str, resume: bool
    ) -> dict[str, Any]:
        from pydantic import BaseModel

        from forgeharness.sdk import (
            Actor,
            ApplicationSpec,
            FinalAction,
            Harness,
            ModelResult,
            NoopVerifier,
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
        )

        name = "read_doc" if operation == "read" else "write_note"

        class Inputs(BaseModel):
            payload: str

        class Tool:
            input_model = Inputs
            spec = ToolSpec(
                name=name,
                description="Synthetic comparison tool",
                input_schema=Inputs.model_json_schema(),
                risk=RiskLevel.READ if operation == "read" else RiskLevel.WRITE,
                effect_class=ToolEffectClass.READ_ONLY
                if operation == "read"
                else ToolEffectClass.IDEMPOTENT,
            )

            async def execute(self, arguments: Any, context: Any) -> Any:
                value = (
                    db.read(arguments.payload)
                    if operation == "read"
                    else await db.write(request_id, arguments.payload)
                )
                return ToolOutput(ok=True, content=value)

        class Model:
            async def decide(self, request: Any) -> Any:
                db.model_input(request.model_dump(mode="json"))
                if request.messages[-1].role.value == "tool":
                    return ModelResult(action=FinalAction(content=request.messages[-1].content))
                return ModelResult(
                    action=ToolAction(
                        call=ToolCall(id="call-1", name=name, arguments={"payload": payload})
                    )
                )

        class Policy:
            def evaluate(self, request: Any) -> Any:
                return PolicyDecision(
                    type=PolicyDecisionType.REQUIRE_APPROVAL
                    if operation == "write"
                    else PolicyDecisionType.ALLOW,
                    reason="native approval fixture",
                )

            def can_approve(self, *, requester: Any, owner: Any, call: Any) -> bool:
                return requester.actor.id != owner.actor.id and "reviewer" in requester.actor.roles

        context = RunContext(
            application_id="comparison",
            application_version="1",
            policy_version="1",
            run_id=request_id,
            session_id="synthetic",
            correlation_id="comparison",
            actor=Actor(id="requester", roles=("customer",)),
            scope=ResourceScope(tenant_id="synthetic-shop", resources=("document:catalog",)),
        )
        engine = Harness(
            ApplicationSpec(
                id="comparison",
                version="1",
                policy_version="1",
                model_factory=Model,
                tools=(Tool(),),
                policy=Policy(),
                verifier=NoopVerifier(),
            ),
            state_dir=db.root / "native",
        )
        if resume:
            reviewer = context.model_copy(
                update={"actor": Actor(id="reviewer", roles=("reviewer",))}
            )
            grant = engine.approve(context=context, approver=reviewer)
            result = await engine.resume(context=context, grant=grant, approver=reviewer)
        else:
            result = await engine.run(
                context=context, task=json.dumps({"operation": operation, "payload": payload})
            )
        if result.status is RunStatus.AWAITING_APPROVAL:
            return {"state": "pending"}
        if result.status is not RunStatus.SUCCEEDED:
            raise RuntimeError("native run did not succeed: " + str(result.status))
        return {"state": "succeeded", "result": result.final_output}


class DeepAgents:
    async def run(
        self, db: Ledger, *, request_id: str, operation: str, payload: str, resume: bool
    ) -> dict[str, Any]:
        from deepagents import create_deep_agent
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage, ToolMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from langchain_core.tools import tool
        from langchain_core.utils.function_calling import convert_to_openai_tool
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        from langgraph.errors import NodeCancelledError
        from langgraph.types import Command

        name = "read_doc" if operation == "read" else "write_note"
        bound_tools: list[dict[str, Any]] = []
        bound_options: dict[str, Any] = {}

        class Model(BaseChatModel):
            @property
            def _llm_type(self) -> str:
                return "deterministic-comparison-fixture"

            def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
                bound_tools[:] = [convert_to_openai_tool(item) for item in tools]
                bound_options.clear()
                bound_options.update(kwargs)
                return self

            def _generate(
                self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any
            ) -> Any:
                db.model_input(
                    {
                        "messages": [m.model_dump(mode="json") for m in messages],
                        "tools": bound_tools,
                        "bind_options": bound_options,
                        "stop": stop,
                        "settings": kwargs,
                    }
                )
                if isinstance(messages[-1], ToolMessage):
                    message = AIMessage(content=messages[-1].content)
                else:
                    message = AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": name,
                                "args": {"payload": payload},
                                "id": "call-1",
                                "type": "tool_call",
                            }
                        ],
                    )
                return ChatResult(generations=[ChatGeneration(message=message)])

        @tool
        def read_doc(payload: str) -> str:
            """Read the current authorized synthetic document."""
            return db.read(payload)

        @tool
        async def write_note(payload: str) -> str:
            """Write an independently approved synthetic support note."""
            return await db.write(request_id, payload)

        async with AsyncSqliteSaver.from_conn_string(str(db.root / "native.sqlite")) as saver:
            graph = create_deep_agent(
                model=Model(),
                tools=[read_doc, write_note],
                checkpointer=saver,
                interrupt_on={"write_note": True},
            )
            config = {"configurable": {"thread_id": request_id}, "recursion_limit": 12}
            value = (
                Command(resume={"decisions": [{"type": "approve"}]})
                if resume
                else {
                    "messages": [
                        {
                            "role": "user",
                            "content": json.dumps({"operation": operation, "payload": payload}),
                        }
                    ]
                }
            )
            try:
                result = await graph.ainvoke(value, config=config)
            except NodeCancelledError as exc:
                # LangGraph wraps a cancelled tool task. Normalize the documented
                # wrapper; do not classify arbitrary agent errors as cancellation.
                raise asyncio.CancelledError from exc
            if result.get("__interrupt__"):
                return {"state": "pending"}
            return {"state": "succeeded", "result": result["messages"][-1].content}


class Pydantic:
    async def run(
        self, db: Ledger, *, request_id: str, operation: str, payload: str, resume: bool
    ) -> dict[str, Any]:
        from dataclasses import asdict

        from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults, Tool
        from pydantic_ai.messages import (
            ModelMessagesTypeAdapter,
            ModelResponse,
            TextPart,
            ToolCallPart,
            ToolReturnPart,
        )
        from pydantic_ai.models.function import FunctionModel
        from pydantic_ai.usage import UsageLimits
        from pydantic_ai_harness.step_persistence import SqliteStepStore, StepPersistence

        name = "read_doc" if operation == "read" else "write_note"

        def model(messages: Any, info: Any) -> Any:
            metadata = asdict(info)
            db.model_input(
                {
                    "messages": json.loads(ModelMessagesTypeAdapter.dump_json(messages)),
                    "tools": metadata["function_tools"],
                    "agent_info": metadata,
                }
            )
            returns = [p for p in messages[-1].parts if isinstance(p, ToolReturnPart)]
            if returns:
                return ModelResponse(parts=[TextPart(str(returns[-1].content))])
            return ModelResponse(
                parts=[ToolCallPart(name, {"payload": payload}, tool_call_id="call-1")]
            )

        def read_doc(payload: str) -> str:
            """Read the current authorized synthetic document."""
            return db.read(payload)

        async def write_note(payload: str) -> str:
            """Write an independently approved synthetic support note."""
            return await db.write(request_id, payload)

        step_store = SqliteStepStore(database=db.root / "native.sqlite")
        agent = Agent(
            FunctionModel(model),
            output_type=[str, DeferredToolRequests],
            tools=[Tool(read_doc), Tool(write_note, requires_approval=True)],
            capabilities=[StepPersistence(store=step_store)],
            name="comparison",
        )
        saved = db.root / "pending-messages.json"
        kwargs: dict[str, Any] = {
            "usage_limits": UsageLimits(request_limit=4, tool_calls_limit=2),
            "run_id": request_id + ("-approve" if resume else "-submit"),
            "conversation_id": request_id,
        }
        if resume:
            kwargs.update(
                message_history=ModelMessagesTypeAdapter.validate_json(saved.read_bytes()),
                deferred_tool_results=DeferredToolResults(approvals={"call-1": True}),
            )
        result = await agent.run(
            None if resume else json.dumps({"operation": operation, "payload": payload}), **kwargs
        )
        if isinstance(result.output, DeferredToolRequests):
            saved.write_bytes(result.all_messages_json())
            return {"state": "pending"}
        return {"state": "succeeded", "result": result.output}


ENGINES = {"forge": Forge, "deepagents": DeepAgents, "pydantic": Pydantic}
