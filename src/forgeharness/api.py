"""Small local control-plane API for runs and trace inspection."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field

from forgeharness import __version__
from forgeharness.domain.models import FinalAction, ModelResult, RunResult, ToolAction, ToolCall
from forgeharness.models.scripted import ScriptedModel
from forgeharness.observability.hash_chain import (
    HashChainedJSONLTrace,
    TraceVerification,
    verify_trace,
)
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.state.checkpoint import SQLiteCheckpointStore
from forgeharness.tools.builtin import EchoTool
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.policy import RiskBasedPolicy
from forgeharness.tools.registry import ToolRegistry

TaskId = Annotated[str, ApiPath(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]


class KeylessDemoRequest(BaseModel):
    """Validated input for the deterministic demo endpoint."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="ForgeHarness is running", min_length=1, max_length=1_000)


class HealthResponse(BaseModel):
    """Stable service identity returned to health probes."""

    service: str
    version: str
    status: str


def create_app(data_dir: Path | None = None) -> FastAPI:
    """Create an app whose durable files remain under one explicit directory."""
    root = (data_dir or Path(".forgeharness")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    checkpoints = SQLiteCheckpointStore(root / "runs.sqlite3")
    traces = root / "traces"
    traces.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="ForgeHarness Control API", version=__version__)

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(service="forgeharness", version=__version__, status="ok")

    @app.post("/runs/keyless-demo", response_model=RunResult)
    async def keyless_demo(request: KeylessDemoRequest) -> RunResult:
        task_id = f"demo-{uuid4().hex}"
        registry = ToolRegistry()
        registry.register(EchoTool())
        trace = HashChainedJSONLTrace(traces / f"{task_id}.jsonl", task_id)
        model = ScriptedModel(
            [
                ModelResult(
                    action=ToolAction(
                        call=ToolCall(id="echo-1", name="echo", arguments={"text": request.text})
                    ),
                    model_name="scripted",
                ),
                ModelResult(
                    action=FinalAction(content=f"Echo verified: {request.text}"),
                    model_name="scripted",
                ),
            ]
        )
        runtime = AgentRuntime(
            model=model,
            registry=registry,
            dispatcher=ToolDispatcher(registry),
            policy=RiskBasedPolicy(),
            trace=trace,
            checkpoint_store=checkpoints,
        )
        return await runtime.run(
            task_id=task_id,
            task="Verify the echo tool",
            workspace=root,
        )

    @app.get("/runs/{task_id}", response_model=RunResult)
    async def get_run(task_id: TaskId) -> RunResult:
        result = checkpoints.load(task_id)
        if result is None:
            raise HTTPException(status_code=404, detail="run not found")
        return result

    @app.get("/traces/{task_id}/verify", response_model=TraceVerification)
    async def get_trace_verification(task_id: TaskId) -> TraceVerification:
        path = traces / f"{task_id}.jsonl"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="trace not found")
        return verify_trace(path)

    return app
