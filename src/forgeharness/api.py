"""Local control plane and R&D knowledge-agent HTTP API."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import (
    FastAPI,
    File,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi import Path as ApiPath
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from forgeharness import __version__
from forgeharness.domain.models import FinalAction, ModelResult, RunResult, ToolAction, ToolCall
from forgeharness.knowledge.application import (
    KnowledgeApplication,
    KnowledgeSettings,
    build_knowledge_application,
)
from forgeharness.knowledge.models import (
    AgentResponse,
    IngestJob,
    JobStatus,
    RetrievalReport,
    Session,
)
from forgeharness.knowledge.parsers import MAX_UPLOAD_BYTES, DocumentParseError
from forgeharness.models.omlx import OMLXProtocolError
from forgeharness.models.scripted import ScriptedModel
from forgeharness.observability.hash_chain import (
    HashChainedJSONLTrace,
    TraceVerification,
    verify_trace,
)
from forgeharness.observability.logging import configure_logging, get_logger
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.state.checkpoint import SQLiteCheckpointStore
from forgeharness.state.memory import MemoryRecord, MemoryStoreError
from forgeharness.tools.builtin import EchoTool
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.paths import WorkspacePathError, resolve_workspace_path
from forgeharness.tools.policy import RiskBasedPolicy
from forgeharness.tools.registry import ToolRegistry
from forgeharness.web import UI, UI_SCRIPT

ResourceId = Annotated[str, ApiPath(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]


class KeylessDemoRequest(BaseModel):
    """Validated input for the deterministic demo endpoint."""

    model_config = ConfigDict(extra="forbid")
    text: str = Field(default="ForgeHarness is running", min_length=1, max_length=1_000)


class HealthResponse(BaseModel):
    """Stable service identity returned to liveness probes."""

    service: str
    version: str
    status: str


class ReadinessResponse(BaseModel):
    """Dependency-specific readiness evidence."""

    status: str
    mode: str
    components: dict[str, bool]
    models: dict[str, bool] = Field(default_factory=dict)


class MessageRequest(BaseModel):
    """Text plus application-owned media references."""

    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=50_000)
    media_ids: tuple[Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")], ...] = Field(
        default=(), max_length=8
    )


class SessionCreateRequest(BaseModel):
    """Optional workspace is always relative to the configured coding root."""

    model_config = ConfigDict(extra="forbid")
    workspace: str | None = Field(default=None, min_length=1, max_length=500)


class SearchRequest(BaseModel):
    """Inspectable hybrid-search input."""

    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=10_000)
    limit: int = Field(default=5, ge=1, le=20)


class MemoryReviewRequest(BaseModel):
    """One explicit human review transition."""

    model_config = ConfigDict(extra="forbid")
    approve: bool


class RunApprovalRequest(BaseModel):
    """External identity attached to an exact pending action."""

    model_config = ConfigDict(extra="forbid")
    granted_by: str = Field(min_length=1, max_length=200)
    checkpoint_revision: int = Field(ge=1)


class _Metrics:
    def __init__(self) -> None:
        self.requests = 0
        self.request_latency_ms = 0.0
        self.ingest_succeeded = 0
        self.ingest_failed = 0

    def render(self) -> str:
        return "\n".join(
            (
                "# TYPE forgeharness_http_requests_total counter",
                f"forgeharness_http_requests_total {self.requests}",
                "# TYPE forgeharness_http_request_latency_ms_total counter",
                f"forgeharness_http_request_latency_ms_total {self.request_latency_ms:.3f}",
                "# TYPE forgeharness_ingest_succeeded_total counter",
                f"forgeharness_ingest_succeeded_total {self.ingest_succeeded}",
                "# TYPE forgeharness_ingest_failed_total counter",
                f"forgeharness_ingest_failed_total {self.ingest_failed}",
                "",
            )
        )


def create_app(
    data_dir: Path | None = None,
    *,
    settings: KnowledgeSettings | None = None,
    knowledge_app: KnowledgeApplication | None = None,
) -> FastAPI:
    """Create an isolated app whose durable files remain under one directory."""
    configure_logging()
    logger = get_logger("api")
    root = (data_dir or Path(".forgeharness")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    checkpoints = SQLiteCheckpointStore(root / "runs.sqlite3")
    traces = root / "traces"
    traces.mkdir(parents=True, exist_ok=True)
    application = knowledge_app or build_knowledge_application(root, settings)
    metrics = _Metrics()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await application.close()

    app = FastAPI(title="ForgeHarness R&D Knowledge Agent", version=__version__, lifespan=lifespan)
    # A loopback bind alone does not prevent browser DNS-rebinding requests.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    app.state.knowledge = application
    app.state.metrics = metrics
    service_api_key = (settings or KnowledgeSettings()).service_api_key

    @app.middleware("http")
    async def record_metrics(request: Request, call_next: Any) -> Any:
        if service_api_key and request.url.path not in {
            "/",
            "/health",
            "/health/live",
            "/docs",
            "/docs/oauth2-redirect",
            "/redoc",
            "/openapi.json",
        }:
            supplied = request.headers.get("x-api-key")
            authorization = request.headers.get("authorization", "")
            if supplied is None and authorization.lower().startswith("bearer "):
                supplied = authorization[7:].strip()
            if supplied is None or not secrets.compare_digest(
                supplied.encode("utf-8"), service_api_key.encode("utf-8")
            ):
                return PlainTextResponse(
                    "authentication required",
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        origin = request.headers.get("origin")
        expected_origin = f"{request.url.scheme}://{request.headers.get('host', '')}"
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin not in {
            None,
            expected_origin,
        }:
            logger.warning(
                "cross_origin_mutation_blocked",
                extra={"method": request.method, "path": request.url.path, "origin": origin},
            )
            return PlainTextResponse("cross-origin mutations are not allowed", status_code=403)
        started = time.perf_counter()
        metrics.requests += 1
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1_000
        metrics.request_latency_ms += elapsed_ms
        log = logger.info if response.status_code < 500 else logger.error
        log(
            "http_request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(elapsed_ms, 2),
            },
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path == "/":
            script_hash = base64.b64encode(hashlib.sha256(UI_SCRIPT.encode()).digest()).decode()
            response.headers["Content-Security-Policy"] = (
                f"default-src 'none'; script-src 'sha256-{script_hash}'; "
                "style-src 'unsafe-inline'; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def user_interface() -> str:
        return UI

    @app.get("/health", response_model=HealthResponse)
    @app.get("/health/live", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(service="forgeharness", version=__version__, status="ok")

    @app.get("/health/ready", response_model=ReadinessResponse)
    async def readiness(response: Response) -> ReadinessResponse:
        components = {
            "sqlite": await _safe_probe(asyncio.to_thread(application.store.ping)),
            "session_store": await _safe_probe(application.sessions.ping()),
            "knowledge_vector": await _safe_probe(application.vector.ping()),
            "memory_vector": await _safe_probe(application.memory_vector.ping()),
            "queue": await _safe_probe(application.queue.ping()),
        }
        models: dict[str, bool] = {}
        if application.omlx is not None:
            try:
                capabilities = await application.omlx.capabilities()
                models = {item.model_id: item.available for item in capabilities}
            except OMLXProtocolError:
                models = {"omlx": False}
            if not models or not all(models.values()):
                response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
                return ReadinessResponse(
                    status="not_ready",
                    mode=application.mode,
                    components=components,
                    models=models,
                )
        if not all(components.values()):
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(
            status="ready" if all(components.values()) else "not_ready",
            mode=application.mode,
            components=components,
            models=models,
        )

    @app.get("/metrics", response_class=PlainTextResponse)
    async def get_metrics() -> str:
        return metrics.render()

    @app.post("/runs/keyless-demo", response_model=RunResult)
    async def keyless_demo(request: KeylessDemoRequest) -> RunResult:
        task_id = f"demo-{uuid4().hex}"
        registry = ToolRegistry()
        registry.register(EchoTool())
        trace = HashChainedJSONLTrace(traces / f"{task_id}.jsonl", task_id)
        model = ScriptedModel(
            [
                ModelResult(
                    action=ToolAction(
                        call=ToolCall(id="echo-1", name="echo", arguments={"text": request.text})
                    ),
                    model_name="scripted",
                ),
                ModelResult(
                    action=FinalAction(content=f"Echo verified: {request.text}"),
                    model_name="scripted",
                ),
            ]
        )
        runtime = AgentRuntime(
            model=model,
            registry=registry,
            dispatcher=ToolDispatcher(registry),
            policy=RiskBasedPolicy(),
            trace=trace,
            checkpoint_store=checkpoints,
        )
        return await runtime.run(task_id=task_id, task="Verify the echo tool", workspace=root)

    @app.post("/v1/documents", response_model=IngestJob, status_code=status.HTTP_202_ACCEPTED)
    async def upload_document(
        idempotency_key: IdempotencyKey,
        file: Annotated[UploadFile, File()],
    ) -> IngestJob:
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        try:
            document, _ = application.parser.parse(
                filename=file.filename or "", data=data, mime_type=file.content_type
            )
        except DocumentParseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        candidate = IngestJob(document_id=document.id)
        try:
            job = application.store.create_job(candidate, idempotency_key=idempotency_key)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        schedule = job.id == candidate.id
        if job.status == JobStatus.FAILED:
            job = application.store.update_job(job.id, status=JobStatus.QUEUED)
            schedule = True
        if schedule:
            try:
                application.store.put(document, data)
                await application.queue.submit(job)
            except Exception as exc:
                job = application.store.update_job(
                    job.id,
                    status=JobStatus.FAILED,
                    error=f"upload persistence or queue submission failed: {type(exc).__name__}"[
                        :2_000
                    ],
                )
                metrics.ingest_failed += 1
                raise HTTPException(
                    status_code=503, detail="ingestion temporarily unavailable"
                ) from exc
            job = application.store.get_job(job.id) or job
            if job.status == JobStatus.SUCCEEDED:
                metrics.ingest_succeeded += 1
            elif job.status == JobStatus.FAILED:
                metrics.ingest_failed += 1
        return job

    @app.get("/v1/jobs/{job_id}", response_model=IngestJob)
    async def get_job(job_id: ResourceId) -> IngestJob:
        job = application.store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @app.post("/v1/sessions", response_model=Session, status_code=status.HTTP_201_CREATED)
    async def create_session(request: SessionCreateRequest | None = None) -> Session:
        workspace: Path | None = None
        if request is not None and request.workspace is not None:
            configured_root = (settings or KnowledgeSettings()).coding_workspace_root
            if configured_root is None:
                raise HTTPException(status_code=403, detail="coding workspace root is disabled")
            try:
                workspace = resolve_workspace_path(
                    configured_root, request.workspace, must_exist=True
                )
            except (WorkspacePathError, FileNotFoundError) as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            if not (workspace / ".git").is_dir():
                raise HTTPException(status_code=422, detail="coding workspace must be a Git repo")
        session = await application.sessions.create()
        if workspace is not None:
            application.store.bind_workspace(session.id, workspace)
        return session

    @app.get("/v1/sessions/{session_id}", response_model=dict[str, Any])
    async def get_session(session_id: ResourceId) -> dict[str, Any]:
        session = await application.sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return {
            "session": session.model_dump(mode="json"),
            "messages": [
                item.model_dump(mode="json")
                for item in await application.sessions.messages(session_id, limit=100)
            ],
        }

    @app.post("/v1/sessions/{session_id}/messages", response_model=AgentResponse)
    async def send_message(session_id: ResourceId, request: MessageRequest) -> AgentResponse:
        try:
            return await application.conversations.send(
                session_id=session_id, text=request.text, media_ids=request.media_ids
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/search", response_model=RetrievalReport)
    async def search(request: SearchRequest) -> RetrievalReport:
        return await application.knowledge.search(request.query, limit=request.limit)

    @app.post("/v1/memories/{memory_id}/review", response_model=MemoryRecord)
    async def review_memory(memory_id: ResourceId, request: MemoryReviewRequest) -> MemoryRecord:
        try:
            return await application.semantic_memories.review(memory_id, approve=request.approve)
        except MemoryStoreError as exc:
            code = 404 if "unknown" in str(exc) else 409
            raise HTTPException(status_code=code, detail=str(exc)) from exc

    @app.get("/runs/{task_id}", response_model=RunResult | AgentResponse)
    @app.get("/v1/runs/{task_id}", response_model=RunResult | AgentResponse)
    async def get_run(task_id: ResourceId) -> RunResult | AgentResponse:
        result = checkpoints.load(task_id)
        if result is not None:
            return result
        knowledge_result = application.runs.get(task_id)
        if knowledge_result is not None:
            return knowledge_result
        raise HTTPException(status_code=404, detail="run not found")

    @app.delete("/v1/memories/{memory_id}", response_model=MemoryRecord)
    async def delete_memory(memory_id: ResourceId) -> MemoryRecord:
        try:
            return await application.semantic_memories.revoke(memory_id, deleted=True)
        except MemoryStoreError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/runs/{task_id}/approve", response_model=RunResult)
    async def approve_run(task_id: ResourceId, request: RunApprovalRequest) -> RunResult:
        result = checkpoints.load(task_id)
        if result is None:
            raise HTTPException(status_code=404, detail="run not found")
        if result.pending_approval is None:
            raise HTTPException(status_code=409, detail="run has no pending approval")
        if application.coding is None:
            raise HTTPException(
                status_code=409,
                detail="run was not created by an active API coding session; use forge repair",
            )
        try:
            return await application.coding.approve(
                task_id,
                granted_by=request.granted_by,
                checkpoint_revision=request.checkpoint_revision,
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/traces/{task_id}/verify", response_model=TraceVerification)
    @app.get("/v1/traces/{task_id}/verify", response_model=TraceVerification)
    async def get_trace_verification(task_id: ResourceId) -> TraceVerification:
        path = traces / f"{task_id}.jsonl"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="trace not found")
        return verify_trace(path)

    return app


async def _safe_probe(operation: Any) -> bool:
    try:
        return bool(await operation)
    except Exception:
        return False
