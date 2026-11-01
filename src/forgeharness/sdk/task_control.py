"""Optional, host-authenticated task status and cancellation HTTP adapter."""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from importlib.resources import files
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict

from forgeharness.domain.models import RunContext
from forgeharness.sdk.application import ScopeError
from forgeharness.sdk.tasks import TaskConflict, TaskState, TaskStore


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except ScopeError as exc:
        raise HTTPException(403, "task unavailable or unauthorized") from exc
    except TaskConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


TASK_PAGE = files("forgeharness.sdk").joinpath("tasks.html").read_text()


def task_router(
    store: TaskStore, *, authenticate: Callable[[Request], Awaitable[RunContext]]
) -> APIRouter:
    """The host returns a freshly authenticated context on every request.

    Cookie-authenticated hosts must enforce their CSRF policy in this callback.
    Request JSON and path selections never supply an actor, tenant or role.
    """
    router = APIRouter(prefix="/harness")

    async def trusted(request: Request) -> RunContext:
        with _errors():
            return await authenticate(request)

    @router.get("/task-console", response_class=HTMLResponse)
    async def console(context: Annotated[RunContext, Depends(trusted)]) -> HTMLResponse:
        with _errors():
            store.list(context, limit=1)
        return HTMLResponse(TASK_PAGE, headers={"Cache-Control": "no-store"})

    @router.get("/tasks")
    async def tasks(
        context: Annotated[RunContext, Depends(trusted)],
        state: TaskState | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> object:
        with _errors():
            return store.list(context, state=state, limit=limit)

    @router.get("/tasks/{task_id}")
    async def detail(task_id: str, context: Annotated[RunContext, Depends(trusted)]) -> object:
        with _errors():
            return store.get(context, task_id)

    @router.post("/tasks/{task_id}/cancel")
    async def cancel(
        task_id: str, body: EmptyRequest, context: Annotated[RunContext, Depends(trusted)]
    ) -> object:
        del body
        with _errors():
            return store.request_cancel(context, task_id)

    return router
