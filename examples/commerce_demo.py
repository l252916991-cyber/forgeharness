"""Synthetic consultation demo; --serve exposes a loopback-only local console.

Demo credentials are public fixture role names, not production authentication.
A real host must replace this callback with its own identity and revocation checks.
"""

import argparse
import asyncio
import json
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from forgeharness.applications.commerce import (
    CommerceStore,
    load_catalog,
    load_faq_corpus,
    publish_faq,
    seed_catalog,
)
from forgeharness.applications.commerce.consultation import (
    consultation_application,
    source_validator,
)
from forgeharness.applications.commerce.operations import bind_application_state, service_lock
from forgeharness.sdk import (
    Actor,
    ResourceScope,
    RevisionStore,
    RoleRecordPolicy,
    RunContext,
    RunStatus,
)
from forgeharness.sdk.sessions import SessionService

DATA = Path(__file__).resolve().parents[1] / "data/synthetic/commerce"


def identity(role: str, session: str = "demo-session", tenant: str = "shop-a") -> RunContext:
    catalog = load_catalog(DATA / "catalog.json")
    faqs = load_faq_corpus(DATA / "faq", tenant_id=tenant)
    roles = (role, "reader") if role == "customer" else (role,)
    return RunContext(
        application_id="commerce-consultation",
        application_version="2",
        policy_version="2",
        run_id="authenticated",
        session_id=session,
        correlation_id="synthetic-demo",
        actor=Actor(id=role, roles=roles),
        scope=ResourceScope(
            tenant_id=tenant,
            resources=tuple(f"product:{p.sku}" for p in catalog if p.tenant_id == tenant)
            + tuple(f"faq:{f.id}" for f in faqs),
        ),
    )


def _build_locked(root: Path) -> tuple[CommerceStore, RevisionStore, SessionService]:
    catalog = load_catalog(DATA / "catalog.json")
    store = CommerceStore(root / "commerce.sqlite")
    seed_catalog(store, catalog)
    records = RevisionStore(root / "records.sqlite", policy=RoleRecordPolicy())
    for tenant in ("shop-a", "shop-b"):
        publish_faq(
            store=records,
            entries=load_faq_corpus(DATA / "faq", tenant_id=tenant),
            editor=identity("editor", tenant=tenant),
            reviewer=identity("reviewer", tenant=tenant),
            publisher=identity("publisher", tenant=tenant),
        )
    sessions = SessionService(
        consultation_application(store=store, records=records, catalog=catalog),
        state_dir=root / "sessions",
        authorize=lambda *_: True,
        validate_sources=source_validator(store, records),
    )
    return store, records, sessions


def build(root: Path) -> tuple[CommerceStore, RevisionStore, SessionService]:
    """Initialize legacy demo state without adopting account-workbench data."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with service_lock(root):
        bind_application_state(
            root, application_id="commerce-consultation", application_version="2"
        )
        return _build_locked(root)


async def demo(root: Path) -> dict[str, object]:
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True, mode=0o700)
    with service_lock(root):
        bind_application_state(
            root, application_id="commerce-consultation", application_version="2"
        )
        return await _demo_locked(root)


async def _demo_locked(root: Path) -> dict[str, object]:
    store, _, sessions = _build_locked(root)
    owner = identity("customer")
    sessions.create(owner)
    first = await sessions.submit(
        owner,
        request_id="compare",
        text=json.dumps(
            {"request_id": "compare", "query": "price", "skus": ["SYN-NB-001", "SYN-NB-002"]}
        ),
    )
    assert first.status is RunStatus.SUCCEEDED
    second = await sessions.submit(
        owner,
        request_id="follow",
        text=json.dumps({"request_id": "follow", "query": "stock", "follow_up": True}),
    )
    assert second.status is RunStatus.SUCCEEDED
    pending = await sessions.submit(
        owner,
        request_id="handoff",
        text=json.dumps(
            {"request_id": "handoff", "query": "support", "skus": ["SYN-NB-001"], "handoff": True}
        ),
    )
    assert pending.status is RunStatus.AWAITING_APPROVAL
    grant = sessions.approve(owner, pending.id, approver=identity("supervisor"))
    finished = await sessions.resume(
        owner, pending.id, grant=grant, approver=identity("supervisor")
    )
    assert finished.status is RunStatus.SUCCEEDED
    return {
        "data": "synthetic only",
        "model": "deterministic fixture",
        "comparison": json.loads(first.output or "{}"),
        "follow_up": json.loads(second.output or "{}"),
        "ticket_id": finished.output,
        "ticket_count": len(store.list_tickets("shop-a")),
    }


def web_app(root: Path):
    from fastapi import FastAPI, HTTPException, Request

    from forgeharness.applications.commerce.control import commerce_router
    from forgeharness.sdk.session_control import session_router

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lifetime_lock = service_lock(root)
    lifetime_lock.__enter__()
    lock_held = True
    try:
        bind_application_state(
            root, application_id="commerce-consultation", application_version="2"
        )
        store, records, sessions = _build_locked(root)
    except BaseException:
        lifetime_lock.__exit__(None, None, None)
        raise

    async def authenticate(request: Request) -> RunContext:
        role = request.headers.get("authorization", "").removeprefix("Bearer ")
        if role not in {"customer", "editor", "reviewer", "publisher", "supervisor", "support"}:
            raise HTTPException(
                401, "Use a public demo role: customer, reviewer, publisher, supervisor, support"
            )
        session = request.headers.get("x-session-id", "demo-session")
        # Delegated approval authenticates the supervisor separately; the bound owner
        # remains customer. Roles in a user message never enter this context.
        owner_role = (
            "customer"
            if role == "supervisor" and "/harness/sessions/" in request.url.path
            else role
        )
        return identity(owner_role, session)

    async def approve_identity(request: Request) -> RunContext:
        if request.headers.get("authorization") != "Bearer supervisor":
            raise HTTPException(403, "Separate supervisor identity required")
        return identity("supervisor", request.headers.get("x-session-id", "demo-session"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
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

    app = FastAPI(title="ForgeHarness synthetic commerce", lifespan=lifespan)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok", "application": "commerce-consultation", "version": "2"}

    @app.get("/health/ready")
    async def ready() -> dict[str, object]:
        checks = {
            "commerce": store.path,
            "records": records.path,
            "sessions": sessions.database,
        }
        try:
            for path in checks.values():
                with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
                    connection.execute("SELECT 1").fetchone()
        except sqlite3.Error as exc:
            raise HTTPException(503, "local state is unavailable") from exc
        return {"status": "ready", "checks": {name: "ok" for name in checks}}

    app.include_router(
        commerce_router(sessions=sessions, store=store, records=records, authenticate=authenticate)
    )
    app.include_router(
        session_router(sessions, authenticate=authenticate, authenticate_approver=approve_identity)
    )
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--state", type=Path, default=Path("reports/commerce-console-state"))
    args = parser.parse_args()
    if args.serve:
        import uvicorn

        uvicorn.run(web_app(args.state), host="127.0.0.1", port=8765)
    else:
        with tempfile.TemporaryDirectory(prefix="forge-commerce-") as directory:
            print(json.dumps(asyncio.run(demo(Path(directory))), ensure_ascii=False, indent=2))
