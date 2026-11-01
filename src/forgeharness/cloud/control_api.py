"""Invitation-only browser adapter over the authoritative PostgreSQL services.

This optional module imports FastAPI; importing ``forgeharness.cloud`` does not.
The public request schemas select objects and supply task text. Identity, scope,
deployment bindings, budgets and execution grants always come from the server.
"""

from collections.abc import Mapping
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from forgeharness.cloud.control import PlatformRuntime
from forgeharness.cloud.identity import PostgresIdentityStore
from forgeharness.cloud.identity_models import Principal, SessionCredentials
from forgeharness.cloud.models import CloudConflict, CloudDenied
from forgeharness.cloud.workbench import WORKBENCH_HTML

COOKIE_NAME = "fh_control_session"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MAX_REQUEST_BYTES = 128 * 1024
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LoginBody(RequestBody):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)


class AcceptBody(RequestBody):
    token: str = Field(min_length=16, max_length=256)
    password: str | None = Field(default=None, min_length=1, max_length=1024)


class InvitationBody(RequestBody):
    email: str = Field(min_length=3, max_length=254)
    role: Literal["owner", "admin", "member"] = "member"


class MemberBody(RequestBody):
    role: Literal["owner", "admin", "member"]
    active: bool


class ActiveBody(RequestBody):
    active: bool


class WorkspaceBody(RequestBody):
    name: str = Field(min_length=1, max_length=120)
    active: bool = True


class GrantBody(RequestBody):
    can_use: bool = True
    can_approve: bool = False
    active: bool = True


class RunBody(RequestBody):
    deployment_id: Identifier
    task: str = Field(min_length=1, max_length=32_000)


class DecisionBody(RequestBody):
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


def _configuration(origins: tuple[str, ...], hosts: tuple[str, ...]) -> None:
    if not origins or not hosts:
        raise ValueError("explicit trusted origins and hosts are required")
    if any(not host or "*" in host or "/" in host or ":" in host for host in hosts):
        raise ValueError("trusted hosts must be exact DNS names or IPv4 addresses")
    for origin in origins:
        parsed = urlsplit(origin)
        _ = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or "*" in origin
        ):
            raise ValueError("trusted origins must be exact HTTP origins without a path")


def _principal(principal: Principal, csrf: str) -> dict[str, Any]:
    return {
        "account_id": principal.account_id,
        "email": principal.email,
        "org_id": principal.org_id,
        "csrf_token": csrf,
    }


def _public_run(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep internal checkpoints and traces out even if an adapter adds fields."""
    return {
        key: value.get(key)
        for key in (
            "run_id",
            "workspace_id",
            "deployment_id",
            "status",
            "cancel_requested",
            "result",
            "used_units",
            "reserved_units",
            "error",
        )
    }


def _public_runs(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "items": [_public_run(item) for item in value["items"]],
        "next_cursor": value.get("next_cursor"),
    }


def create_control_app(
    identity: PostgresIdentityStore,
    runtime: PlatformRuntime,
    *,
    allowed_origins: tuple[str, ...],
    allowed_hosts: tuple[str, ...],
    secure_cookies: bool = True,
) -> FastAPI:
    """Create the public API; it never starts or invokes an execution worker.

    Use a separate identity DML connection pool and a Web runtime database role.
    ``secure_cookies=False`` is intended solely for explicit loopback tests.
    """
    _configuration(allowed_origins, allowed_hosts)
    app = FastAPI(title="ForgeHarness Internal Alpha", docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Content-Type", CSRF_HEADER, "Idempotency-Key"],
    )

    @app.middleware("http")
    async def browser_boundary(request: Request, call_next: Any) -> Response:
        if (
            request.method not in SAFE_METHODS
            and request.headers.get("origin") not in allowed_origins
        ):
            response: Response = JSONResponse({"error": "request_verification_failed"}, 403)
        else:
            try:
                body = bytearray()
                if request.method not in SAFE_METHODS:
                    too_large = False
                    async for chunk in request.stream():
                        if len(body) + len(chunk) > MAX_REQUEST_BYTES:
                            too_large = True
                            break
                        body.extend(chunk)
                    if too_large:
                        response = JSONResponse({"error": "request_too_large"}, 413)
                    else:
                        # Starlette's cached Request body is replayed to the
                        # downstream parser without reading the socket twice.
                        request._body = bytes(body)
                        response = await call_next(request)
                else:
                    response = await call_next(request)
            except Exception:
                # Exception strings can contain SQL, DSNs, tokens and user input.
                response = JSONResponse({"error": "internal_error"}, 500)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'"
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": "invalid_request"}, 422)

    @app.exception_handler(CloudDenied)
    async def unavailable(_request: Request, _exc: CloudDenied) -> JSONResponse:
        return JSONResponse({"error": "unavailable"}, 404)

    @app.exception_handler(CloudConflict)
    async def conflict(_request: Request, _exc: CloudConflict) -> JSONResponse:
        return JSONResponse({"error": "conflict"}, 409)

    @app.exception_handler(ValueError)
    async def invalid_value(_request: Request, _exc: ValueError) -> JSONResponse:
        return JSONResponse({"error": "invalid_request"}, 422)

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": str(exc.detail)}, exc.status_code)

    def cookie(response: Response, credentials: SessionCredentials) -> None:
        from datetime import UTC, datetime

        response.set_cookie(
            COOKIE_NAME,
            credentials.token,
            max_age=max(0, int((credentials.expires_at - datetime.now(UTC)).total_seconds())),
            expires=credentials.expires_at,
            path="/",
            secure=secure_cookies,
            httponly=True,
            samesite="strict",
        )

    async def current(request: Request) -> Principal:
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            raise HTTPException(401, "authentication_required")
        try:
            principal = await identity.authenticate(token)
        except CloudDenied as exc:
            raise HTTPException(401, "authentication_required") from exc
        if request.method not in SAFE_METHODS:
            try:
                return await identity.check_csrf(token, request.headers.get(CSRF_HEADER, ""))
            except CloudDenied as exc:
                raise HTTPException(403, "request_verification_failed") from exc
        return principal

    def selected(principal: Principal, org_id: str) -> None:
        if principal.org_id != org_id:
            raise CloudDenied("organization unavailable")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def workbench() -> str:
        return WORKBENCH_HTML

    @app.get("/assets/workbench.js", include_in_schema=False)
    async def script() -> Response:
        from forgeharness.cloud.workbench import WORKBENCH_SCRIPT

        return Response(WORKBENCH_SCRIPT, media_type="text/javascript")

    @app.get("/assets/workbench.css", include_in_schema=False)
    async def stylesheet() -> Response:
        from forgeharness.cloud.workbench import WORKBENCH_CSS

        return Response(WORKBENCH_CSS, media_type="text/css")

    @app.post("/v1/login")
    async def login(body: LoginBody, response: Response) -> dict[str, Any]:
        try:
            result = await identity.login(body.email, body.password)
        except CloudDenied as exc:
            raise HTTPException(401, "authentication_required") from exc
        cookie(response, result)
        return _principal(result.principal, result.csrf_token)

    @app.post("/v1/invitations/accept")
    async def accept_invitation(
        body: AcceptBody, request: Request, response: Response
    ) -> dict[str, Any]:
        existing_session = request.cookies.get(COOKIE_NAME)
        if existing_session:
            try:
                await identity.authenticate(existing_session)
            except CloudDenied:
                # An idle-expired Cookie must not prevent a new account from
                # accepting its invitation. Existing accounts still require a
                # matching authenticated session in the identity transaction.
                existing_session = None
            else:
                await current(request)
        try:
            result = await identity.accept_invitation(
                body.token,
                password=body.password,
                existing_session=existing_session,
            )
        except CloudDenied as exc:
            raise HTTPException(401, "authentication_required") from exc
        cookie(response, result)
        return _principal(result.principal, result.csrf_token)

    @app.get("/v1/me")
    async def me(
        request: Request, principal: Annotated[Principal, Depends(current)]
    ) -> dict[str, Any]:
        csrf = await identity.session_csrf(request.cookies[COOKIE_NAME])
        return _principal(principal, csrf)

    @app.post("/v1/logout", status_code=204)
    async def logout(
        request: Request, response: Response, principal: Annotated[Principal, Depends(current)]
    ) -> Response:
        del principal
        await identity.logout(request.cookies[COOKIE_NAME])
        response.delete_cookie(
            COOKIE_NAME, path="/", secure=secure_cookies, httponly=True, samesite="strict"
        )
        response.status_code = 204
        return response

    @app.get("/v1/organizations")
    async def organizations(principal: Annotated[Principal, Depends(current)]) -> dict[str, Any]:
        return {"items": await identity.list_organizations(principal)}

    @app.post("/v1/organizations/{org_id}/select")
    async def select_organization(
        org_id: Identifier,
        request: Request,
        response: Response,
        principal: Annotated[Principal, Depends(current)],
    ) -> dict[str, Any]:
        del principal
        result = await identity.switch_organization(request.cookies[COOKIE_NAME], org_id)
        cookie(response, result)
        return _principal(result.principal, result.csrf_token)

    @app.get("/v1/organizations/{org_id}/members")
    async def members(
        org_id: Identifier, principal: Annotated[Principal, Depends(current)]
    ) -> dict[str, Any]:
        selected(principal, org_id)
        return {"items": await identity.list_members(principal, org_id)}

    @app.post("/v1/organizations/{org_id}/invitations", status_code=201)
    async def invite(
        org_id: Identifier, body: InvitationBody, principal: Annotated[Principal, Depends(current)]
    ) -> dict[str, str]:
        selected(principal, org_id)
        token = await identity.invite(principal, org_id, body.email, body.role)
        return {"invitation_token": token}

    @app.get("/v1/organizations/{org_id}/invitations")
    async def invitations(
        org_id: Identifier,
        principal: Annotated[Principal, Depends(current)],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=128)] = None,
    ) -> dict[str, Any]:
        selected(principal, org_id)
        return await identity.list_invitations(principal, org_id, limit=limit, cursor=cursor)

    @app.post("/v1/organizations/{org_id}/invitations/{invitation_id}/revoke", status_code=204)
    async def revoke_invitation(
        org_id: Identifier,
        invitation_id: Identifier,
        principal: Annotated[Principal, Depends(current)],
    ) -> Response:
        selected(principal, org_id)
        await identity.revoke_invitation(principal, org_id, invitation_id)
        return Response(status_code=204)

    @app.put("/v1/organizations/{org_id}/members/{account_id}", status_code=204)
    async def set_member(
        org_id: Identifier,
        account_id: Identifier,
        body: MemberBody,
        principal: Annotated[Principal, Depends(current)],
    ) -> Response:
        selected(principal, org_id)
        await identity.set_member(principal, org_id, account_id, body.role, body.active)
        return Response(status_code=204)

    @app.put("/v1/organizations/{org_id}/active", status_code=204)
    async def set_organization(
        org_id: Identifier,
        body: ActiveBody,
        principal: Annotated[Principal, Depends(current)],
    ) -> Response:
        selected(principal, org_id)
        await identity.set_organization(principal, org_id, body.active)
        return Response(status_code=204)

    @app.get("/v1/organizations/{org_id}/workspaces")
    async def workspaces(
        org_id: Identifier, principal: Annotated[Principal, Depends(current)]
    ) -> dict[str, Any]:
        selected(principal, org_id)
        return {"items": await identity.list_workspaces(principal, org_id)}

    @app.put("/v1/organizations/{org_id}/workspaces/{workspace_id}", status_code=204)
    async def put_workspace(
        org_id: Identifier,
        workspace_id: Identifier,
        body: WorkspaceBody,
        principal: Annotated[Principal, Depends(current)],
    ) -> Response:
        selected(principal, org_id)
        await identity.put_workspace(principal, org_id, workspace_id, body.name, body.active)
        return Response(status_code=204)

    @app.put(
        "/v1/organizations/{org_id}/workspaces/{workspace_id}/members/{account_id}",
        status_code=204,
    )
    async def workspace_member(
        org_id: Identifier,
        workspace_id: Identifier,
        account_id: Identifier,
        body: ActiveBody,
        principal: Annotated[Principal, Depends(current)],
    ) -> Response:
        selected(principal, org_id)
        await identity.set_workspace_member(
            principal, org_id, workspace_id, account_id, body.active
        )
        return Response(status_code=204)

    @app.get("/v1/organizations/{org_id}/workspaces/{workspace_id}/deployments")
    async def deployments(
        org_id: Identifier,
        workspace_id: Identifier,
        principal: Annotated[Principal, Depends(current)],
    ) -> dict[str, Any]:
        selected(principal, org_id)
        return {"items": await identity.list_deployments(principal, org_id, workspace_id)}

    @app.put(
        "/v1/organizations/{org_id}/workspaces/{workspace_id}/deployments/{deployment_id}",
        status_code=204,
    )
    async def put_deployment(
        org_id: Identifier,
        workspace_id: Identifier,
        deployment_id: Identifier,
        body: ActiveBody,
        principal: Annotated[Principal, Depends(current)],
    ) -> Response:
        selected(principal, org_id)
        binding = runtime.registered_deployment(deployment_id)
        await identity.put_deployment(
            principal,
            org_id,
            deployment_id,
            workspace_id,
            binding["application_id"],
            binding["binding_digest"],
            body.active,
        )
        return Response(status_code=204)

    @app.put(
        "/v1/organizations/{org_id}/deployments/{deployment_id}/grants/{account_id}",
        status_code=204,
    )
    async def deployment_grant(
        org_id: Identifier,
        deployment_id: Identifier,
        account_id: Identifier,
        body: GrantBody,
        principal: Annotated[Principal, Depends(current)],
    ) -> Response:
        selected(principal, org_id)
        await identity.set_deployment_grant(
            principal,
            org_id,
            deployment_id,
            account_id,
            body.can_use,
            body.can_approve,
            body.active,
        )
        return Response(status_code=204)

    @app.post("/v1/organizations/{org_id}/workspaces/{workspace_id}/runs", status_code=202)
    async def accept_run(
        org_id: Identifier,
        workspace_id: Identifier,
        body: RunBody,
        principal: Annotated[Principal, Depends(current)],
        request_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    ) -> dict[str, Any]:
        selected(principal, org_id)
        if not body.task.strip():
            raise HTTPException(422, "invalid_request")
        return _public_run(
            await runtime.accept(
                principal, org_id, workspace_id, body.deployment_id, body.task, request_key
            )
        )

    @app.get("/v1/organizations/{org_id}/workspaces/{workspace_id}/runs")
    async def runs(
        org_id: Identifier,
        workspace_id: Identifier,
        principal: Annotated[Principal, Depends(current)],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=128)] = None,
    ) -> dict[str, Any]:
        selected(principal, org_id)
        return _public_runs(
            await runtime.list_runs(principal, org_id, workspace_id, limit=limit, cursor=cursor)
        )

    @app.get("/v1/organizations/{org_id}/workspaces/{workspace_id}/runs/{run_id}")
    async def inspect_run(
        org_id: Identifier,
        workspace_id: Identifier,
        run_id: Identifier,
        principal: Annotated[Principal, Depends(current)],
    ) -> dict[str, Any]:
        selected(principal, org_id)
        return _public_run(await runtime.inspect(principal, org_id, workspace_id, run_id))

    @app.post("/v1/organizations/{org_id}/workspaces/{workspace_id}/runs/{run_id}/cancel")
    async def cancel_run(
        org_id: Identifier,
        workspace_id: Identifier,
        run_id: Identifier,
        principal: Annotated[Principal, Depends(current)],
    ) -> dict[str, Any]:
        selected(principal, org_id)
        return _public_run(await runtime.cancel(principal, org_id, workspace_id, run_id))

    @app.get("/v1/organizations/{org_id}/workspaces/{workspace_id}/approvals")
    async def pending_approvals(
        org_id: Identifier,
        workspace_id: Identifier,
        principal: Annotated[Principal, Depends(current)],
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        cursor: Annotated[str | None, Query(max_length=128)] = None,
    ) -> dict[str, Any]:
        selected(principal, org_id)
        return await runtime.pending_approvals(
            principal, org_id, workspace_id, limit=limit, cursor=cursor
        )

    @app.get("/v1/organizations/{org_id}/workspaces/{workspace_id}/runs/{run_id}/approval")
    async def approval(
        org_id: Identifier,
        workspace_id: Identifier,
        run_id: Identifier,
        principal: Annotated[Principal, Depends(current)],
    ) -> dict[str, Any]:
        selected(principal, org_id)
        return await runtime.approval_detail(principal, org_id, workspace_id, run_id)

    async def decision(
        principal: Principal,
        org_id: str,
        workspace_id: str,
        run_id: str,
        body: DecisionBody,
        value: Literal["approve", "reject"],
    ) -> dict[str, Any]:
        selected(principal, org_id)
        return await runtime.decide(
            principal, org_id, workspace_id, run_id, value, body.fingerprint
        )

    @app.post("/v1/organizations/{org_id}/workspaces/{workspace_id}/runs/{run_id}/approve")
    async def approve(
        org_id: Identifier,
        workspace_id: Identifier,
        run_id: Identifier,
        body: DecisionBody,
        principal: Annotated[Principal, Depends(current)],
    ) -> dict[str, Any]:
        return await decision(principal, org_id, workspace_id, run_id, body, "approve")

    @app.post("/v1/organizations/{org_id}/workspaces/{workspace_id}/runs/{run_id}/reject")
    async def reject(
        org_id: Identifier,
        workspace_id: Identifier,
        run_id: Identifier,
        body: DecisionBody,
        principal: Annotated[Principal, Depends(current)],
    ) -> dict[str, Any]:
        return await decision(principal, org_id, workspace_id, run_id, body, "reject")

    return app
