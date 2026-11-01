"""Comparison oracles must reject denial-only and false-success implementations."""

import json
import sys
from pathlib import Path

import pytest

from benchmarks.comparison.host import CONTENT, Conflict, Denied, Host, Ledger, model_input_json
from benchmarks.comparison.run import (
    ActionRunner,
    clean_environment,
    complete_model_inputs,
    grade,
    load_protocol,
    scenario,
)


@pytest.mark.parametrize(
    "case",
    [
        "read_control",
        "approval_restart_control",
        "permission_revocation",
        "source_withdrawal",
        "approval_mutation",
        "lost_receipt",
        "duplicate_request",
        "cancel_before_write",
        "cancel_after_write",
    ],
)
def test_actual_forge_worker_restart_and_fault_contracts(tmp_path, case):
    result = scenario(ActionRunner(Path(sys.executable), "forge", tmp_path), case)
    assert result["outcome"] == "passed", result


@pytest.mark.parametrize("case", ["read_control", "approval_restart_control"])
def test_oracle_rejects_always_deny(case):
    actions = [{"result": {"state": "denied"}, "exit_code": 0}]
    assert not grade(case, actions, {"effects": 0, "model_inputs": 0, "events": []})


def test_oracle_rejects_false_completion_duplicate_effects_and_failed_worker():
    actions = [
        {"result": {"state": "pending", "worker_pid": 1}, "exit_code": 0},
        {"result": {"state": "succeeded", "worker_pid": 2}, "exit_code": 0},
    ]
    event = {
        "kind": "model_input",
        "payload": json.dumps(
            {
                "evidence_format": "structured-model-input-v2",
                "messages": [],
                "tools": [{"name": "write_note"}],
            }
        ),
    }
    evidence = {"effects": 1, "model_inputs": 2, "events": [event, event]}
    assert grade("approval_restart_control", actions, evidence)
    evidence["effects"] = 0
    assert not grade("approval_restart_control", actions, evidence)
    evidence["effects"] = 2
    assert not grade("approval_restart_control", actions, evidence)
    evidence["effects"] = 1
    actions[1]["exit_code"] = 1
    assert not grade("approval_restart_control", actions, evidence)
    with pytest.raises(ValueError):
        grade("unknown-case", [], evidence)


def test_model_evidence_rejects_legacy_text_only_and_missing_tools():
    def evidence(payload):
        return {
            "model_inputs": 1,
            "events": [
                {"kind": "model_input", "payload": json.dumps(payload)},
            ],
        }

    assert not complete_model_inputs(evidence([{"role": "tool", "content": CONTENT}]))
    assert not complete_model_inputs(evidence({"messages": [], "tools": []}))
    assert complete_model_inputs(
        evidence(
            {
                "evidence_format": "structured-model-input-v2",
                "messages": [],
                "tools": [{"name": "read_doc"}],
            }
        )
    )


def test_model_input_set_metadata_is_reconstructable_and_unknown_types_fail():
    value = model_input_json({"modes": frozenset({"b", "a"}), "set": {"x"}})
    assert value["modes"] == {"__python_type__": "frozenset", "items": ["a", "b"]}
    assert value["set"] == {"__python_type__": "set", "items": ["x"]}
    with pytest.raises(TypeError, match="unsupported model input type"):
        model_input_json({"opaque": object()})


async def test_host_rechecks_after_wait_without_calling_model(tmp_path):
    class Engine:
        async def run(self, *args, **kwargs):
            raise AssertionError("engine must not be admitted after revocation")

    db = Ledger(tmp_path)
    db.set("fault", "revoke_during_wait")
    with pytest.raises(Denied):
        await Host(db, Engine()).execute(action="submit", operation="read")
    assert db.count("model_input") == 0


async def test_host_does_not_hide_arbitrary_engine_failures_as_cancel(tmp_path):
    class Engine:
        async def run(self, *args, **kwargs):
            raise ValueError("bad adapter")

    with pytest.raises(ValueError, match="bad adapter"):
        await Host(Ledger(tmp_path), Engine()).execute(action="submit", operation="read")


async def test_host_refuses_changed_versions_on_cached_results(tmp_path):
    class Engine:
        async def run(self, *args, **kwargs):
            return {"state": "succeeded", "result": CONTENT}

    db = Ledger(tmp_path)
    host = Host(db, Engine())
    await host.execute(action="submit", operation="read")
    db.set("version", 2)
    with pytest.raises(Denied):
        await host.execute(action="submit", operation="read")
    with pytest.raises(Conflict):
        await host.execute(action="submit", operation="read", payload="different")


def test_worker_environment_omits_credentials_and_provider_config(monkeypatch):
    monkeypatch.setenv("SOME_NEW_PROVIDER_API_KEY", "synthetic-sensitive-value")
    monkeypatch.setenv("LOGFIRE_TOKEN", "synthetic-sensitive-value")
    monkeypatch.setenv("FORGE_MODEL", "must-not-use")
    monkeypatch.setenv("PYTHONHOME", "must-not-use")
    env = clean_environment()
    assert "synthetic-sensitive-value" not in json.dumps(env)
    assert "FORGE_MODEL" not in env and "PYTHONHOME" not in env
    assert env["OTEL_SDK_DISABLED"] == "true"


def test_protocol_change_and_incomplete_coverage_cannot_reuse_declaration(tmp_path):
    import hashlib

    from benchmarks.comparison.run import PROTOCOL

    path = tmp_path / "protocol.json"
    path.write_bytes(PROTOCOL.read_bytes())
    seal = path.with_suffix(".sha256")
    seal.write_text(hashlib.sha256(path.read_bytes()).hexdigest())
    assert len(load_protocol(path)["scenarios"]) == 9
    value = json.loads(path.read_text())
    value["scenarios"] = value["scenarios"][:2]
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="changed"):
        load_protocol(path)
    seal.write_text(hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(ValueError, match="coverage"):
        load_protocol(path)


def test_next_pilot_web_app_excludes_legacy_knowledge_chat_routes(tmp_path):
    from fastapi.testclient import TestClient

    from examples.commerce_demo import web_app

    app = web_app(tmp_path)
    paths = set(app.openapi()["paths"])
    assert "/commerce/consult" in paths and "/harness/sessions/{session_id}" in paths
    assert "/health/live" in paths and "/health/ready" in paths
    assert all(not path.startswith(("/api/", "/knowledge/", "/chat")) for path in paths)
    with TestClient(app) as client:
        assert client.get("/health/live").json()["status"] == "ok"
        assert client.get("/health/ready").json()["status"] == "ready"
        assert client.post("/api/chat", json={"text": "must not route"}).status_code == 404
        assert (
            client.post("/commerce/consult", json={"request_id": "r", "query": "price"}).status_code
            == 401
        )
