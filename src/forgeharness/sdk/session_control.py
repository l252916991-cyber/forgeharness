"""Optional HTTP adapter for host-authenticated persistent sessions."""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from forgeharness.domain.models import RunContext
from forgeharness.sdk.application import ScopeError
from forgeharness.sdk.sessions import SessionConflict, SessionService
from forgeharness.state.approval import ApprovalError, ApprovalGrant
from forgeharness.state.checkpoint import CheckpointConflict


class EmptySubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MessageSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=100_000)


class ResumeSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grant: ApprovalGrant | None = None


@contextmanager
def _http_errors() -> Iterator[None]:
    try:
        yield
    except (ScopeError, ApprovalError) as exc:
        # Missing, deleted and out-of-scope objects share one response. Never
        # disclose whether an identifier belongs to another tenant or actor.
        raise HTTPException(403, "session operation unavailable or unauthorized") from exc
    except (SessionConflict, CheckpointConflict) as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def session_router(
    service: SessionService,
    *,
    authenticate: Callable[[Request], Awaitable[RunContext]],
    authenticate_approver: Callable[[Request], Awaitable[RunContext]] | None = None,
) -> APIRouter:
    """Build an optional router with fresh host authentication on every request.

    ``authenticate`` must authenticate the caller and authorize the owner context
    and selected session from current host permissions. It must not manufacture a
    trusted context from request JSON. Path identifiers select objects, not scope.

    Delegated approval requires ``authenticate_approver`` to return the separately
    authenticated approver. Both callbacks run again when consuming a grant; the
    service also checks both principals against current permissions. Without the
    second callback approval is self-approval. A submitted grant is untrusted
    evidence, never an identity source, and is checked by the durable ledger.
    """
    router = APIRouter(prefix="/harness")

    async def trusted(request: Request) -> RunContext:
        with _http_errors():
            context = await authenticate(request)
            selected = request.path_params.get("session_id")
            if selected is not None and selected != context.session_id:
                raise ScopeError("session selection is outside the authenticated binding")
            return context

    async def approver(request: Request, context: RunContext) -> RunContext:
        if authenticate_approver is None:
            return context
        return await authenticate_approver(request)

    @router.post("/sessions", status_code=201)
    async def create(
        context: Annotated[RunContext, Depends(trusted)],
        body: Annotated[EmptySubmission | None, Body()] = None,
    ) -> object:
        del body
        with _http_errors():
            return service.create(context)

    @router.get("/sessions/{session_id}")
    async def get(context: Annotated[RunContext, Depends(trusted)]) -> object:
        with _http_errors():
            return service.get(context)

    @router.get("/sessions/{session_id}/history")
    async def history(
        context: Annotated[RunContext, Depends(trusted)],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        before_sequence: Annotated[int | None, Query(ge=1)] = None,
    ) -> object:
        with _http_errors():
            return service.history(context, limit=limit, before_sequence=before_sequence)

    @router.post("/sessions/{session_id}/messages")
    async def submit(
        body: MessageSubmission, context: Annotated[RunContext, Depends(trusted)]
    ) -> object:
        with _http_errors():
            return await service.submit(context, request_id=body.request_id, text=body.text)

    @router.get("/sessions/{session_id}/turns/{turn_id}")
    async def get_turn(turn_id: str, context: Annotated[RunContext, Depends(trusted)]) -> object:
        with _http_errors():
            return service.get_turn(context, turn_id)

    @router.get("/sessions/{session_id}/turns/{turn_id}/inspect")
    async def inspect(turn_id: str, context: Annotated[RunContext, Depends(trusted)]) -> object:
        with _http_errors():
            return service.inspect(context, turn_id)

    @router.get("/sessions/{session_id}/turns/{turn_id}/events")
    async def events(
        turn_id: str,
        context: Annotated[RunContext, Depends(trusted)],
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> object:
        with _http_errors():
            return service.events(context, turn_id, after=after, limit=limit)

    @router.post("/sessions/{session_id}/turns/{turn_id}/approve")
    async def approve(
        turn_id: str,
        request: Request,
        context: Annotated[RunContext, Depends(trusted)],
        body: Annotated[EmptySubmission | None, Body()] = None,
    ) -> object:
        del body
        with _http_errors():
            return service.approve(context, turn_id, approver=await approver(request, context))

    @router.post("/sessions/{session_id}/turns/{turn_id}/resume")
    async def resume(
        turn_id: str,
        request: Request,
        context: Annotated[RunContext, Depends(trusted)],
        body: Annotated[ResumeSubmission | None, Body()] = None,
    ) -> object:
        grant = body.grant if body is not None else None
        with _http_errors():
            return await service.resume(
                context,
                turn_id,
                grant=grant,
                approver=await approver(request, context) if grant is not None else None,
            )

    @router.post("/sessions/{session_id}/close")
    async def close(
        context: Annotated[RunContext, Depends(trusted)],
        body: Annotated[EmptySubmission | None, Body()] = None,
    ) -> object:
        del body
        with _http_errors():
            return service.close(context)

    @router.delete("/sessions/{session_id}/turns/{turn_id}", status_code=204)
    async def delete_turn(
        turn_id: str,
        context: Annotated[RunContext, Depends(trusted)],
        body: Annotated[EmptySubmission | None, Body()] = None,
    ) -> Response:
        del body
        with _http_errors():
            service.delete_turn(context, turn_id)
        return Response(status_code=204)

    @router.delete("/sessions/{session_id}", status_code=204)
    async def delete(
        context: Annotated[RunContext, Depends(trusted)],
        body: Annotated[EmptySubmission | None, Body()] = None,
    ) -> Response:
        del body
        with _http_errors():
            service.delete(context)
        return Response(status_code=204)

    return router
