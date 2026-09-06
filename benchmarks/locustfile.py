"""HTTP workload for a separately started ForgeHarness API."""

from __future__ import annotations

from uuid import uuid4

from locust import HttpUser, between, task


class ForgeHarnessUser(HttpUser):
    wait_time = between(0.05, 0.2)

    @task(5)
    def search(self) -> None:
        self.client.post("/v1/search", json={"query": "agent approval", "limit": 5})

    @task(2)
    def readiness(self) -> None:
        self.client.get("/health/ready")

    @task(1)
    def upload(self) -> None:
        token = uuid4().hex
        self.client.post(
            "/v1/documents",
            headers={"Idempotency-Key": token},
            files={"file": (f"{token}.txt", f"agent evidence {token}", "text/plain")},
        )
