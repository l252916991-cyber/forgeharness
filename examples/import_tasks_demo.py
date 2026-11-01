"""Synthetic legal and commerce import → task → review → publish → search."""

import asyncio
import json
import tempfile
from pathlib import Path

from forgeharness.sdk import Actor, ResourceScope, RevisionStore, RoleRecordPolicy, RunContext
from forgeharness.sdk.imports import ImportService, ParsedDocument, StagedMaterial
from forgeharness.sdk.retrieval import AuthorizedIndex
from forgeharness.sdk.tasks import TaskRunner, TaskState, TaskStore


class TextParser:
    def parse(self, *, filename: str, data: bytes, content_type: str) -> ParsedDocument:
        return ParsedDocument(content=data.decode("utf-8"))


class SyntheticValidator:
    def validate(
        self, context: RunContext, staged: StagedMaterial, parsed: ParsedDocument
    ) -> tuple[bool, str]:
        return "synthetic" in parsed.content.lower(), "Only synthetic demo material is accepted"


def identity(domain: str, role: str) -> RunContext:
    return RunContext(
        application_id="import-demo",
        application_version="1",
        policy_version="1",
        run_id="request",
        session_id=domain,
        correlation_id="synthetic",
        actor=Actor(id=role, roles=(role,)),
        scope=ResourceScope(tenant_id=domain, resources=(f"{domain}:material",)),
    )


async def demo(root: Path) -> dict[str, object]:
    records = RevisionStore(root / "records.sqlite", policy=RoleRecordPolicy())
    tasks = TaskStore(root / "tasks", authorize=lambda *_: True)
    service = ImportService(
        state_dir=root / "imports",
        records=records,
        tasks=tasks,
        parser=TextParser(),
        validator=SyntheticValidator(),
        authorize=lambda *_: True,
    )
    index = AuthorizedIndex(root / "index.sqlite", store=records, validate_sources=lambda *_: True)
    result = {}
    for domain in ("legal", "commerce"):
        caller = identity(domain, "editor")
        staged = service.stage(
            caller, filename="material.txt", data=f"Synthetic {domain} evidence.".encode()
        )
        task = service.start_import(
            caller,
            staged_id=staged.id,
            resource_id=f"{domain}:material",
            kind=domain,
            request_id="import-1",
        )
        finished = await TaskRunner(tasks, handlers=service.handlers()).run_once(caller, task.id)
        assert finished.state is TaskState.SUCCEEDED
        records.review(
            identity(domain, "reviewer"), f"{domain}:material", expected_version=1, approve=True
        )
        records.publish(identity(domain, "publisher"), f"{domain}:material", expected_version=1)
        index.refresh(caller)
        hits = index.search(caller, "evidence")
        assert len(hits) == 1
        result[domain] = {
            "task": finished.state,
            "version": hits[0].entry.version,
            "source": hits[0].source.anchor,
        }
    return {"data": "synthetic only", "domains": result}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="forge-import-") as directory:
        print(json.dumps(asyncio.run(demo(Path(directory))), ensure_ascii=False, indent=2))
