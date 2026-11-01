"""Keyless account/approval smoke using disposable state and random credentials.

No real provider, business data, cloud account or payment system is used.
"""

import json
import secrets
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from forgeharness.applications.commerce.identity import APPROVAL_HEADER, CSRF_HEADER
from forgeharness.applications.commerce.workbench import bootstrap_admin, create_workbench


def main() -> None:
    password = secrets.token_urlsafe(32)
    with TemporaryDirectory(prefix="forge-workbench-") as directory:
        state = Path(directory)
        bootstrap_admin(
            state, workspace_id="smoke-shop", email="admin@example.test", password=password
        )
        app = create_workbench(state)
        identities = app.state.workbench.identities
        for role in ("customer", "supervisor", "support"):
            identities.add_user(
                workspace_id="smoke-shop",
                email=f"{role}@example.test",
                password=password,
                display_name=role,
                role=role,
            )
        with TestClient(app, base_url="https://testserver") as client:

            def login(role: str) -> None:
                result = client.post(
                    "/identity/login",
                    json={
                        "email": f"{role}@example.test",
                        "password": password,
                        "workspace_id": "smoke-shop",
                    },
                )
                assert result.status_code == 200, result.status_code
                client.headers[CSRF_HEADER] = result.json()["csrf"]

            login("customer")
            client.headers["X-Session-Id"] = "smoke-session"
            body = {"request_id": "smoke-handoff", "query": "Please ask a person", "handoff": True}
            pending = client.post("/commerce/consult", json=body)
            assert pending.status_code == 200, pending.status_code
            assert pending.json()["status"] == "awaiting_approval"
            turn = pending.json()["id"]
            approval = client.post(
                "/identity/approval",
                json={
                    "email": "supervisor@example.test",
                    "password": password,
                    "workspace_id": "smoke-shop",
                },
            )
            assert approval.status_code == 200, approval.status_code
            client.headers[APPROVAL_HEADER] = approval.json()["approval_token"]
            base = f"/harness/sessions/smoke-session/turns/{turn}"
            grant = client.post(base + "/approve", json={})
            assert grant.status_code == 200, grant.status_code
            resumed = client.post(base + "/resume", json={"grant": grant.json()})
            assert resumed.status_code == 200, resumed.status_code
            assert resumed.json()["status"] == "succeeded"
            replay = client.post("/commerce/consult", json=body)
            assert replay.status_code == 200 and replay.json()["id"] == turn
            login("support")
            tickets = client.get("/commerce/tickets")
            assert tickets.status_code == 200 and len(tickets.json()) == 1
            assert client.get("/health/ready").status_code == 200
            print(
                json.dumps(
                    {
                        "application": "commerce-workbench",
                        "version": "1",
                        "authentication": "distinct local accounts",
                        "model": "fixture",
                        "status": "succeeded",
                        "ticket_count": len(tickets.json()),
                    }
                )
            )


if __name__ == "__main__":
    main()
