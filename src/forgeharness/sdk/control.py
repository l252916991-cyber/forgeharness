"""Optional FastAPI control router; authentication is supplied by the host."""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from forgeharness.domain.models import RunContext
from forgeharness.sdk.application import Harness, ScopeError
from forgeharness.state.approval import ApprovalError
from forgeharness.state.checkpoint import CheckpointConflict


class TaskSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(min_length=1, max_length=100_000)


def control_router(
    harness: Harness, *, authenticate: Callable[[Request], Awaitable[RunContext]]
) -> APIRouter:
    """Host callback verifies credentials, run ownership and scope from request.

    It must authenticate on every request; task JSON contains no identity fields.
    A RunContext returned from client-controlled text without validation is unsafe.
    """
    router = APIRouter(prefix="/harness")

    # A closure dependency keeps FastAPI's annotation resolution straightforward.
    async def trusted(request: Request) -> RunContext:
        return await authenticate(request)

    async def handle(context: RunContext, operation: str, task: str = "") -> object:
        try:
            if operation == "run":
                return await harness.run(context=context, task=task)
            if operation == "inspect":
                return harness.inspect(context)
            if operation == "approve":
                # This minimal endpoint is self-approval only; delegated approval
                # uses embedded SDK with separate host-authenticated owner context.
                grant = harness.approve(context=context, approver=context)
                return await harness.resume(context=context, grant=grant, approver=context)
            return await harness.resume(context=context)
        except (ScopeError, ApprovalError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (CheckpointConflict, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/runs")
    async def run(body: TaskSubmission, context: Annotated[RunContext, Depends(trusted)]) -> object:
        return await handle(context, "run", body.task)

    @router.get("/runs/current")
    async def inspect(context: Annotated[RunContext, Depends(trusted)]) -> object:
        return await handle(context, "inspect")

    @router.post("/runs/current/approve")
    async def approve(context: Annotated[RunContext, Depends(trusted)]) -> object:
        return await handle(context, "approve")

    @router.post("/runs/current/resume")
    async def resume(context: Annotated[RunContext, Depends(trusted)]) -> object:
        return await handle(context, "resume")

    return router
