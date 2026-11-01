"""Invitation-only local workbench over the existing governed consultation app.

This entry is intentionally separate from the public-role synthetic demo. Its
business model is still deterministic and local; identity and permissions are
real account records and are re-read at session, model and tool boundaries.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from forgeharness.applications.commerce.application import CommerceStore
from forgeharness.applications.commerce.catalog import CatalogProduct, seed_catalog
from forgeharness.applications.commerce.consultation import (
    ConsultationPolicy,
    consultation_application,
    source_validator,
)
from forgeharness.applications.commerce.control import commerce_router
from forgeharness.applications.commerce.identity import IdentityStore, Principal
from forgeharness.applications.commerce.identity_http import (
    ScopeResolver,
    approver_dependency,
    context_dependency,
    identity_router,
)
from forgeharness.applications.commerce.operations import bind_application_state, service_lock
from forgeharness.sdk import (
    AuthorizationRequest,
    Model,
    PolicyDecision,
    PolicyDecisionType,
    RevisionStore,
    RunContext,
    ScopeError,
    SessionService,
    ToolCall,
)
from forgeharness.sdk.records import RecordOperation
from forgeharness.sdk.session_control import session_router
from forgeharness.sdk.sessions import Session, SessionOperation


@dataclass(frozen=True)
class WorkbenchState:
    """Host-side services; not serialized into HTTP responses or run evidence."""

    identities: IdentityStore
    commerce: CommerceStore
    records: RevisionStore
    sessions: SessionService


class _CurrentAccess:
    def __init__(self, identities: IdentityStore, resources: ScopeResolver) -> None:
        self.identities, self.resources = identities, resources

    def principal(self, context: RunContext) -> Principal:
        if not context.actor.id.startswith("user:"):
            raise ScopeError("current account identity required")
        principal = self.identities.current_principal(
            user_id=context.actor.id.removeprefix("user:"),
            workspace_id=context.scope.tenant_id,
        )
        if context.actor != principal.actor or context.scope.resources != tuple(
            dict.fromkeys(self.resources(principal))
        ):
            raise ScopeError("current account binding changed")
        return principal

    def owner(self, context: RunContext) -> bool:
        try:
            principal = self.principal(context)
            principal.require("consult")
            self.identities.session_owner(principal=principal, session_id=context.session_id)
        except ScopeError:
            return False
        return True

    def run(self, context: RunContext) -> bool:
        # Harness validates the separately authenticated approver through the same
        # run gate. Actual approval still requires the exact owner/scope/action in
        # _CurrentPolicy.can_approve; no model/tool admission follows from this alone.
        if self.owner(context):
            return True
        try:
            return self.principal(context).has("approval:decide")
        except ScopeError:
            return False

    def session(
        self, context: RunContext, operation: SessionOperation, session: Session | None
    ) -> bool:
        if (
            operation is SessionOperation.APPROVE
            and session is not None
            and context.actor.id != session.actor_id
        ):
            try:
                self.principal(context).require("approval:decide")
            except ScopeError:
                return False
            return True
        return self.owner(context)


class _CurrentPolicy(ConsultationPolicy):
    def __init__(self, store: CommerceStore, access: _CurrentAccess) -> None:
        super().__init__(store)
        self.access = access

    def evaluate(self, request: AuthorizationRequest) -> PolicyDecision:
        if not self.access.owner(request.context):
            return PolicyDecision(type=PolicyDecisionType.DENY, reason="account grant changed")
        return super().evaluate(request)

    def can_approve(self, *, requester: RunContext, owner: RunContext, call: ToolCall) -> bool:
        try:
            self.access.principal(requester).require("approval:decide")
        except ScopeError:
            return False
        return self.access.owner(owner) and super().can_approve(
            requester=requester, owner=owner, call=call
        )


class _CurrentRecordPolicy:
    def __init__(self, access: _CurrentAccess) -> None:
        self.access = access

    def authorize(self, context: RunContext, operation: RecordOperation, resource_id: str) -> bool:
        try:
            principal = self.access.principal(context)
        except ScopeError:
            return False
        if resource_id not in context.scope.resources:
            return False
        if operation is RecordOperation.READ:
            return principal.has("consult") or principal.has("knowledge:read")
        required = {
            RecordOperation.WRITE: "knowledge:draft",
            RecordOperation.REVIEW: "knowledge:review",
            RecordOperation.PUBLISH: "knowledge:publish",
            RecordOperation.WITHDRAW: "knowledge:publish",
        }.get(operation)
        return required is not None and principal.has(required)


def create_workbench(
    root: Path,
    *,
    catalog: Sequence[CatalogProduct] = (),
    resources: ScopeResolver | None = None,
    secure_cookies: bool = True,
    model_factory: Callable[[], Model] | None = None,
) -> FastAPI:
    """Build a local account-authenticated app; no accounts or passwords are seeded.

    ``resources`` is trusted host configuration and is re-evaluated on access.
    With no resolver, only catalogue products in the account workspace are granted.
    Cookie security defaults to HTTPS; loopback HTTP needs an explicit opt-out.
    """
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lifetime_lock = service_lock(root)
    lifetime_lock.__enter__()
    lock_held = True
    catalog = tuple(catalog)
    resolver = resources or (
        lambda principal: tuple(
            f"product:{item.sku}" for item in catalog if item.tenant_id == principal.workspace_id
        )
    )
    try:
        if not (root / "identity.sqlite").is_file():
            raise ValueError("bootstrap an administrator before starting the workbench")
        bind_application_state(root, application_id="commerce-workbench", application_version="1")
        identities = IdentityStore(root / "identity.sqlite")
        commerce = CommerceStore(root / "commerce.sqlite")
        seed_catalog(commerce, catalog)
        access = _CurrentAccess(identities, resolver)
        records = RevisionStore(root / "records.sqlite", policy=_CurrentRecordPolicy(access))
        application = consultation_application(store=commerce, records=records, catalog=catalog)
        if model_factory is not None:
            application = replace(application, model_factory=model_factory)
        application = replace(
            application,
            id="commerce-workbench",
            version="1",
            policy_version="1",
            policy=_CurrentPolicy(commerce, access),
            authorize_run=access.run,
        )
        sessions = SessionService(
            application,
            state_dir=root / "sessions",
            authorize=access.session,
            validate_sources=source_validator(commerce, records),
        )
    except BaseException:
        lifetime_lock.__exit__(None, None, None)
        raise

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal lifetime_lock, lock_held
        del app
        if not lock_held:
            lifetime_lock = service_lock(root)
            lifetime_lock.__enter__()
            lock_held = True
        try:
            yield
        finally:
            lifetime_lock.__exit__(None, None, None)
            lock_held = False

    app = FastAPI(title="ForgeHarness invitation-only local workbench", lifespan=lifespan)
    app.state.workbench = WorkbenchState(identities, commerce, records, sessions)

    @app.middleware("http")
    async def private_responses(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    owner = context_dependency(
        identities,
        application_id=application.id,
        application_version=application.version,
        policy_version=application.policy_version,
        resources=resolver,
    )
    approver = approver_dependency(
        identities,
        application_id=application.id,
        application_version=application.version,
        policy_version=application.policy_version,
        resources=resolver,
    )

    async def authenticated(request: Request) -> RunContext:
        context = await owner(request)
        principal = access.principal(context)
        path = request.url.path
        permission = None
        if path == "/commerce/consult" or path.startswith("/harness/"):
            permission = "consult"
        elif path.startswith("/commerce/knowledge"):
            permission = (
                "knowledge:review"
                if path.endswith("/review")
                else "knowledge:publish"
                if path.endswith("/publish")
                else "knowledge:read"
            )
        elif path.startswith("/commerce/tickets"):
            permission = "ticket:read_scope"
        if permission is None or not principal.has(permission):
            raise HTTPException(403, "operation is not authorized")
        return context

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(
            files("forgeharness.applications.commerce").joinpath("workbench.html").read_text(),
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok", "mode": "account-authenticated-local"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        checks = (
            (identities.path, "SELECT id, status FROM users LIMIT 0"),
            (commerce.path, "SELECT tenant, sku, version FROM products LIMIT 0"),
            (records.path, "SELECT app, tenant, resource, version FROM record_heads LIMIT 0"),
            (sessions.database, "SELECT id, payload FROM sdk_sessions LIMIT 0"),
        )
        try:
            for path, query in checks:
                with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
                    connection.execute(query).fetchall()
        except sqlite3.Error as exc:
            raise HTTPException(503, "local state is unavailable") from exc
        return {"status": "ready"}

    app.include_router(identity_router(identities, secure_cookies=secure_cookies))
    # The synthetic demo console uses role headers and must never be mounted here.
    business = commerce_router(
        sessions=sessions, store=commerce, records=records, authenticate=authenticated
    )
    business.routes[:] = [
        route for route in business.routes if getattr(route, "path", "") != "/commerce/console"
    ]
    app.include_router(business)
    app.include_router(
        session_router(sessions, authenticate=authenticated, authenticate_approver=approver)
    )
    return app


def bootstrap_admin(root: Path, *, workspace_id: str, email: str, password: str) -> str:
    """Offline bootstrap only: initialize one previously absent identity database.

    The caller supplies a private state path and reads the password without command
    line arguments. Repeated bootstrap is refused; later users join by invitation.
    """
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with service_lock(root):
        if (root / "identity.sqlite").exists():
            raise ValueError("identity state already exists; use an administrator invitation")
        bind_application_state(root, application_id="commerce-workbench", application_version="1")
        with TemporaryDirectory(prefix=".identity-bootstrap-", dir=root) as directory:
            identities = IdentityStore(Path(directory) / "identity.sqlite")
            identities.create_workspace(workspace_id, workspace_id=workspace_id)
            user_id = identities.add_user(
                workspace_id=workspace_id,
                email=email,
                password=password,
                display_name="Administrator",
                role="admin",
            )
            identities.path.replace(root / "identity.sqlite")
            return user_id
