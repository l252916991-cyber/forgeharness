from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_tasks import context, create, new_store

from forgeharness.domain.models import ResourceScope
from forgeharness.sdk.task_control import task_router


def test_task_page_and_api_use_fresh_host_permissions(tmp_path):
    store = new_store(tmp_path / "tasks")
    task = create(store, request_id="one")
    identity = context()
    calls = []

    async def authenticate(request):
        calls.append(request.url.path)
        return identity

    app = FastAPI()
    app.include_router(task_router(store, authenticate=authenticate))
    with TestClient(app) as client:
        assert client.get("/harness/task-console").status_code == 200
        assert client.get("/harness/tasks").json()[0]["id"] == task.id
        assert client.get(f"/harness/tasks/{task.id}").status_code == 200
        assert (
            client.post(f"/harness/tasks/{task.id}/cancel", json={"tenant_id": "other"}).status_code
            == 422
        )
        identity = context(scope=ResourceScope(tenant_id="other"))
        assert client.get("/harness/tasks").json() == []
        assert client.get(f"/harness/tasks/{task.id}").status_code == 403
        assert client.post(f"/harness/tasks/{task.id}/cancel", json={}).status_code == 403
        identity = context()
        assert (
            client.post(f"/harness/tasks/{task.id}/cancel", json={}).json()["state"] == "cancelled"
        )
        assert client.get("/harness/tasks?limit=0").status_code == 422
    assert len(calls) >= 8
