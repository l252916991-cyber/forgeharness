"""Optional commerce console; all data and mutations use fresh host identity."""

from collections.abc import Awaitable, Callable
from importlib.resources import files
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from forgeharness.applications.commerce.application import CommerceStore
from forgeharness.sdk import RevisionStore, RunContext, ScopeError, SessionService
from forgeharness.sdk.records import RecordConflict
from forgeharness.sdk.session_control import _http_errors
from forgeharness.sdk.sessions import SessionConflict


class ConsultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=200)
    skus: tuple[str, ...] = Field(default=(), max_length=5)
    faq_ids: tuple[str, ...] = Field(default=(), max_length=5)
    follow_up: bool = False
    handoff: bool = False


class RevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


CONSOLE = files("forgeharness.applications.commerce").joinpath("console.html").read_text()


def commerce_router(
    *,
    sessions: SessionService,
    store: CommerceStore,
    records: RevisionStore,
    authenticate: Callable[[Request], Awaitable[RunContext]],
) -> APIRouter:
    router = APIRouter(prefix="/commerce")

    async def trusted(request: Request) -> RunContext:
        with _http_errors():
            return await authenticate(request)

    @router.get("/console", response_class=HTMLResponse)
    async def console() -> HTMLResponse:
        # Public static shell has no business data. Each fetch authenticates afresh.
        return HTMLResponse(CONSOLE, headers={"Cache-Control": "no-store"})

    @router.post("/consult")
    async def consult(
        body: ConsultRequest, context: Annotated[RunContext, Depends(trusted)]
    ) -> object:
        with _http_errors():
            try:
                sessions.create(context)
            except SessionConflict:
                sessions.get(context)
            return await sessions.submit(
                context, request_id=body.request_id, text=body.model_dump_json()
            )

    @router.get("/knowledge")
    async def knowledge(context: Annotated[RunContext, Depends(trusted)]) -> object:
        with _http_errors():
            if not {"staff", "editor", "reviewer", "publisher", "admin"}.intersection(
                context.actor.roles
            ):
                raise ScopeError("knowledge management role required")
            result = []
            for resource in context.scope.resources:
                if resource.startswith("faq:"):
                    try:
                        result.append(records.read(context, resource))
                    except RecordConflict:
                        continue
            return result

    @router.post("/knowledge/{resource}/review")
    async def review(
        resource: str, body: RevisionRequest, context: Annotated[RunContext, Depends(trusted)]
    ) -> object:
        with _http_errors():
            return records.review(
                context, resource, expected_version=body.expected_version, approve=True
            )

    @router.post("/knowledge/{resource}/publish")
    async def publish(
        resource: str, body: RevisionRequest, context: Annotated[RunContext, Depends(trusted)]
    ) -> object:
        with _http_errors():
            return records.publish(context, resource, expected_version=body.expected_version)

    def ticket_access(context: RunContext) -> None:
        if not {"support", "supervisor"}.intersection(context.actor.roles):
            raise ScopeError("support role required")

    @router.get("/tickets")
    async def tickets(context: Annotated[RunContext, Depends(trusted)]) -> object:
        with _http_errors():
            ticket_access(context)
            return tuple(
                row
                for row in store.list_tickets(context.scope.tenant_id)
                if not row["sku"] or f"product:{row['sku']}" in context.scope.resources
            )

    @router.get("/tickets/{ticket_id}")
    async def detail(ticket_id: str, context: Annotated[RunContext, Depends(trusted)]) -> object:
        with _http_errors():
            ticket_access(context)
            try:
                row = store.ticket_detail(ticket_id, tenant=context.scope.tenant_id)
            except ValueError as exc:
                raise ScopeError("ticket unavailable") from exc
            if row["sku"] and f"product:{row['sku']}" not in context.scope.resources:
                raise ScopeError("ticket unavailable")
            return row

    return router
