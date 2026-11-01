"""HTTP identity routes and trusted-context dependencies for the commerce console.

The browser never names an actor. A request is authenticated from the ``fh_identity``
cookie, the account and membership rows are re-read on every call, unsafe methods must
carry ``X-CSRF-Token``, and the harness binding is built from the verified identity plus
a server-side resource list.

Approval runs on a channel of its own: ``POST /identity/approval`` exchanges a
supervisor's own credentials for a short-lived ``X-Approval-Token``. That token cannot
open a browser session, and the owner's cookie cannot approve anything.
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from forgeharness.applications.commerce.identity import (
    APPROVAL_HEADER,
    COOKIE_NAME,
    CSRF_HEADER,
    SESSION_HEADER,
    ApprovalGrant,
    AuthenticationError,
    IdentityStore,
    LoginResult,
    LoginThrottled,
    Principal,
    RegistrationError,
)
from forgeharness.sdk import RunContext, ScopeError

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
ScopeResolver = Callable[[Principal], Sequence[str]]


def _http_error(exc: Exception) -> HTTPException:
    """Map identity failures without disclosing account or object existence."""
    if isinstance(exc, LoginThrottled):
        return HTTPException(429, "too many failed attempts")
    if isinstance(exc, AuthenticationError):
        return HTTPException(401, "authentication required")
    if isinstance(exc, ScopeError):
        return HTTPException(403, "operation is not authorized")
    if isinstance(exc, (RegistrationError, ValueError)):
        return HTTPException(422, str(exc))
    raise exc


def _public(principal: Principal, *, csrf: str) -> dict[str, object]:
    return {
        "user_id": principal.user_id,
        "display_name": principal.display_name,
        "workspace_id": principal.workspace_id,
        "roles": list(principal.roles),
        "permissions": sorted(principal.permissions),
        "csrf": csrf,
    }


def _set_cookie(
    response: Response, result: LoginResult, *, secure: bool, store: IdentityStore
) -> None:
    response.set_cookie(
        COOKIE_NAME,
        result.token,
        max_age=max(0, int(store.session_ttl.total_seconds())),
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )


def _clear_cookie(response: Response, *, secure: bool) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", secure=secure, httponly=True, samesite="lax")


def _csrf_failure() -> HTTPException:
    return HTTPException(403, "request verification failed")


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)
    workspace_id: str = Field(min_length=1, max_length=128)


class AcceptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=120)


class InvitationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    role: str = Field(min_length=1, max_length=32)


def identity_router(store: IdentityStore, *, secure_cookies: bool = True) -> APIRouter:
    """Browser session, invitation and approval-channel routes."""
    router = APIRouter(prefix="/identity")

    def current(request: Request) -> tuple[str, Principal]:
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            raise HTTPException(401, "authentication required")
        try:
            return token, store.authenticate(token)
        except AuthenticationError as exc:
            raise _http_error(exc) from exc

    def csrf_checked(request: Request) -> tuple[str, Principal]:
        token, principal = current(request)
        try:
            if request.method not in SAFE_METHODS and not store.verify_csrf(
                token=token, csrf=request.headers.get(CSRF_HEADER, "")
            ):
                raise _csrf_failure()
        except AuthenticationError as exc:
            raise _http_error(exc) from exc
        return token, principal

    @router.post("/login")
    def login(body: LoginBody, response: Response) -> dict[str, object]:
        try:
            result = store.login(
                email=body.email, password=body.password, workspace_id=body.workspace_id
            )
        except (AuthenticationError, RegistrationError, ValueError) as exc:
            raise _http_error(exc) from exc
        _set_cookie(response, result, secure=secure_cookies, store=store)
        return {**_public(result.principal, csrf=result.csrf), "expires_at": result.expires_at}

    @router.post("/logout", status_code=204)
    def logout(
        request: Request,
        response: Response,
        session: Annotated[tuple[str, Principal], Depends(csrf_checked)],
    ) -> Response:
        del request
        token, _ = session
        store.logout(token=token)
        _clear_cookie(response, secure=secure_cookies)
        response.status_code = 204
        return response

    @router.get("/me")
    def me(session: Annotated[tuple[str, Principal], Depends(current)]) -> dict[str, object]:
        token, principal = session
        try:
            return _public(principal, csrf=store.session_csrf(token))
        except AuthenticationError as exc:
            raise _http_error(exc) from exc

    @router.post("/invitations", status_code=201)
    def invite(
        body: InvitationBody, session: Annotated[tuple[str, Principal], Depends(csrf_checked)]
    ) -> dict[str, str]:
        _, principal = session
        try:
            token = store.invite(actor=principal, email=body.email, role=body.role)
        except (ScopeError, RegistrationError, ValueError) as exc:
            raise _http_error(exc) from exc
        return {"invitation_token": token}

    @router.post("/invitations/{invitation_token}/accept")
    def accept(invitation_token: str, body: AcceptBody, response: Response) -> dict[str, object]:
        try:
            result = store.accept_invitation(
                token=invitation_token, password=body.password, display_name=body.display_name
            )
        except (AuthenticationError, RegistrationError, ValueError) as exc:
            raise _http_error(exc) from exc
        _set_cookie(response, result, secure=secure_cookies, store=store)
        return _public(result.principal, csrf=result.csrf)

    @router.post("/approval")
    def approval(body: LoginBody) -> dict[str, object]:
        try:
            grant: ApprovalGrant = store.approve_session(
                email=body.email, password=body.password, workspace_id=body.workspace_id
            )
        except (ScopeError, RegistrationError, ValueError) as exc:
            raise _http_error(exc) from exc
        return {
            "approval_token": grant.token,
            "expires_at": grant.expires_at,
            "approver": grant.principal.user_id,
        }

    return router


def context_dependency(
    store: IdentityStore,
    *,
    application_id: str,
    application_version: str,
    policy_version: str,
    resources: ScopeResolver | None = None,
) -> Callable[[Request], Awaitable[RunContext]]:
    """Owner context: cookie identity, CSRF on writes, bound session identifier."""
    resolver = resources or (lambda principal: ())

    async def authenticate(request: Request) -> RunContext:
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            raise HTTPException(401, "authentication required")
        try:
            principal = store.authenticate(token)
            if request.method not in SAFE_METHODS and not store.verify_csrf(
                token=token, csrf=request.headers.get(CSRF_HEADER, "")
            ):
                raise _csrf_failure()
        except AuthenticationError as exc:
            raise _http_error(exc) from exc
        session_id = request.path_params.get("session_id")
        if session_id is None and principal.has("consult"):
            # Only a consulting identity addresses a consultation session; other roles
            # (support, approval, knowledge) never pick one up from a header.
            session_id = request.headers.get(SESSION_HEADER)
        if session_id:
            # A session identifier is bound to one owner per workspace; an identifier
            # from the path or a header is never an authorization credential.
            try:
                store.bind_session(principal=principal, session_id=session_id)
            except ScopeError as exc:
                raise _http_error(exc) from exc
        else:
            # Routes that address no session (tickets, knowledge) still need a stable
            # per-user binding for the run record.
            session_id = f"web-{principal.user_id}"
        return store.context_for(
            principal,
            application_id=application_id,
            application_version=application_version,
            policy_version=policy_version,
            session_id=session_id,
            resources=resolver(principal),
            correlation_id=session_id,
        )

    return authenticate


def approver_dependency(
    store: IdentityStore,
    *,
    application_id: str,
    application_version: str,
    policy_version: str,
    resources: ScopeResolver | None = None,
) -> Callable[[Request], Awaitable[RunContext]]:
    """Approver context: a separate credential, never the owner's cookie."""
    resolver = resources or (lambda principal: ())

    async def authenticate(request: Request) -> RunContext:
        token = request.headers.get(APPROVAL_HEADER, "")
        if not token:
            raise HTTPException(403, "a separate approver credential is required")
        try:
            principal = store.authenticate_approver(token)
        except ScopeError as exc:
            raise _http_error(exc) from exc
        session_id = (
            request.path_params.get("session_id")
            or request.headers.get(SESSION_HEADER)
            or f"web-{principal.user_id}"
        )
        return store.context_for(
            principal,
            application_id=application_id,
            application_version=application_version,
            policy_version=policy_version,
            session_id=session_id,
            resources=resolver(principal),
            correlation_id=session_id,
            run_id=f"approver-{principal.user_id}",
        )

    return authenticate
