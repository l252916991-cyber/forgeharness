"""Process-death drill against the existing ForgeCommerce HTTP ASGI entrypoint.

Run with the host's own bound-wheel interpreter. All state is an explicit
synthetic directory. Never starts a public server or touches a customer tenant.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path


async def exercise(root: Path, action: str) -> None:
    import httpx
    from forgecommerce.config import Settings
    from forgecommerce.main import create_app

    app = create_app(Settings(state=root))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://127.0.0.1:8787",
        headers={"origin": "http://127.0.0.1:8787"},
    ) as client:
        login = await client.post("/api/login", json={"profile": "visitor"})
        assert login.status_code == 200, login.status_code
        client.headers["x-csrf-token"] = login.json()["csrf"]
        metadata = root / "drill.json"
        if action == "crash":
            response = await client.post("/api/conversations")
            assert response.status_code == 200
            conversation = response.json()["id"]
            response = await client.post(
                f"/api/conversations/{conversation}/handoff",
                json={"summary": "合成测试租户: 请客服核实商品资料。"},
            )
            assert response.status_code == 200
            intent = response.json()
            metadata.write_text(json.dumps({"conversation": conversation, "intent": intent}))

            def after_sdk_before_host_projection(*args, **kwargs):
                os._exit(91)

            app.state.agent.sessions.inspect = after_sdk_before_host_projection
            await client.post(
                f"/api/intents/{intent['id']}/confirm", json={"digest": intent["digest"]}
            )
            raise AssertionError("crash barrier was not reached")
        data = json.loads(metadata.read_text())
        row = app.state.store.one(
            "SELECT * FROM requests WHERE conversation=?", (data["conversation"],)
        )
        response = await client.post(f"/api/requests/{row['id']}/resume")
        assert response.status_code == 200, response.status_code
        assert response.json()["state"] == "succeeded"
        first = response.json()["output"]["ticket_id"]
        intent = data["intent"]
        repeated = await client.post(
            f"/api/intents/{intent['id']}/confirm", json={"digest": intent["digest"]}
        )
        assert repeated.status_code == 200
        assert repeated.json()["output"]["ticket_id"] == first
        count = app.state.store.one("SELECT COUNT(*) AS n FROM tickets")["n"]
        assert count == 1
        print(
            json.dumps(
                {
                    "status": "succeeded",
                    "tickets": count,
                    "ticket_id": first,
                    "http_transport": "ASGI in independent host processes",
                    "effect": "synthetic local ticket; no merchant system",
                }
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("action", choices=("crash", "recover"))
    args = parser.parse_args()
    if not args.root.resolve().is_relative_to(Path(__file__).resolve().parents[1] / "reports"):
        parser.error("synthetic drill state must stay in workspace reports")
    asyncio.run(exercise(args.root, args.action))


if __name__ == "__main__":
    main()
