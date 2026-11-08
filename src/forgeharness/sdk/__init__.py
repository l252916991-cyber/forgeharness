"""ForgeHarness public API v1. Business apps import contracts from here."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

from forgeharness.backends.composite import CompositeBackend, composite_factory
from forgeharness.backends.container import (
    CONTAINER_WORKDIR as CONTAINER_WORKDIR,
)
from forgeharness.backends.container import (
    ContainerBackend,
    ContainerLimits,
    WarmContainerPool,
    container_argv,
    container_backend_factory,
    docker_runner,
    exec_argv,
    pooled_argv,
)
from forgeharness.backends.local import LocalBackend, local_backend
from forgeharness.backends.protocol import (
    BackendError,
    BackendFactory,
    FileBackend,
    FileConflict,
    FileEntry,
    FileTooLarge,
    NotRegularFile,
    ReadResult,
    WriteResult,
)
from forgeharness.context.compiler import ContextCompiler, ContextItem
from forgeharness.context.compression import (
    CompressionResult,
    ObservationCompressor,
    SpillCorrupt,
    SpillError,
    SpillNotFound,
    SpillUnavailable,
    observation_spill_dir,
)
from forgeharness.context.provider import ContextMaterial, ContextProvider
from forgeharness.context.spill import MessageSpill
from forgeharness.domain.models import (
    Actor,
    BusinessReceipt,
    FinalAction,
    FrozenModel,
    Message,
    MessageRole,
    ModelResult,
    ModelUsage,
    ResourceScope,
    RunContext,
    RunResult,
    RunStatus,
    ToolAction,
    ToolCall,
    ToolEvidence,
    Usage,
)
from forgeharness.models.base import Model, ModelRequest, ModelRequestRejected
from forgeharness.models.caching import CacheMode, CachePlan, plan_cache, planner_for
from forgeharness.models.capabilities import ModelCapability
from forgeharness.models.prepared import PreparedModelCall, PreparingModel
from forgeharness.models.scripted import ScriptedModel
from forgeharness.observability.hash_chain import HashChainedJSONLTrace, verify_trace
from forgeharness.observability.trace import InMemoryTrace, TraceRecorder
from forgeharness.runtime.budget import RunBudget
from forgeharness.runtime.loop import AgentRuntime
from forgeharness.runtime.todos import (
    TodoBoard,
    TodoItem,
    TodoSnapshot,
    TodoStatus,
    WriteTodosTool,
    render_todos,
)
from forgeharness.runtime.verification import (
    NoopVerifier,
    VerificationRequest,
    VerificationResult,
    Verifier,
)
from forgeharness.sdk.capability import Capability, compose_application
from forgeharness.sdk.collaboration import (
    AgentFinding,
    AgentReport,
    Collaboration,
    CollaborationAgent,
    CollaborationEvidence,
    CollaborationPlan,
    CollaborationSnapshot,
    CollaborationTask,
    CollaborationVerifier,
    DelegationSnapshot,
    collaboration_graph,
)
from forgeharness.sdk.durable import DurableGraph, DurableNode, DurableOrchestrator, ReconciledNode
from forgeharness.sdk.evidence import (
    AllOfVerifier,
    Citation,
    CitationVerifier,
    CitedAnswer,
    PublishedContextProvider,
    PublishedRecordTool,
)
from forgeharness.sdk.harness import Harness
from forgeharness.sdk.imports import (
    ImportConflict,
    ImportOperation,
    ImportRequest,
    ImportService,
    ImportUnavailable,
    MaterialParser,
    MaterialValidator,
    ParsedDocument,
    ParsedSpan,
    RetentionPolicy,
    StagedMaterial,
    ValidationOutcome,
)
from forgeharness.sdk.kernel import (
    KernelBudgetExceeded,
    KernelToolDenied,
    OrchestrationError,
    Orchestrator,
    RunKernel,
)
from forgeharness.sdk.parallel import CapacityError, ModelDispatcher, ParallelExecution
from forgeharness.sdk.policy import AuthorizationRequest, ContextualPolicy
from forgeharness.sdk.privacy import CredentialMask, EvidenceTransform
from forgeharness.sdk.records import (
    ContentRevision,
    RecordConflict,
    RecordDependency,
    RecordDraft,
    RecordOperation,
    RecordPolicy,
    RecordUnavailable,
    RevisionStore,
    RoleRecordPolicy,
    SourceAnchor,
)
from forgeharness.sdk.retrieval import (
    AuthorizedIndex,
    IndexConflict,
    IndexEntry,
    IndexHit,
    RetrievalUnavailable,
)
from forgeharness.sdk.sessions import (
    Session,
    SessionConflict,
    SessionOperation,
    SessionService,
    SessionTurn,
    SessionUnavailable,
)
from forgeharness.sdk.sources import SourceRef, anchors_for, governed_source_refs
from forgeharness.sdk.spec import ApplicationComposition, ApplicationSpec, ScopeError
from forgeharness.sdk.tasks import (
    TaskConflict,
    TaskHandler,
    TaskOperation,
    TaskOutcome,
    TaskRecord,
    TaskRunner,
    TaskState,
    TaskStore,
    TaskUnavailable,
)
from forgeharness.sdk.workflows import (
    WorkflowBudget,
    WorkflowResult,
    WorkflowRunner,
    WorkflowSpec,
    WorkflowStep,
    WorkflowVerificationRequest,
    WorkflowVerifier,
)
from forgeharness.state.approval import (
    ApprovalError,
    ApprovalGrant,
    ApprovalLedger,
    InMemoryApprovalLedger,
    SQLiteApprovalLedger,
)
from forgeharness.state.checkpoint import CheckpointConflict, CheckpointStore, SQLiteCheckpointStore
from forgeharness.state.invocation_journal import InvocationJournal, SQLiteInvocationJournal
from forgeharness.tools.base import (
    RiskLevel,
    Tool,
    ToolContext,
    ToolEffectClass,
    ToolFailure,
    ToolOutput,
    ToolSpec,
    TransientToolError,
)
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.paths import WorkspacePathError, resolve_workspace_path
from forgeharness.tools.policy import (
    DEFAULT_DENIED_COMMANDS,
    DEFAULT_SECRET_PATH_RULES,
    CallTargets,
    CommandRule,
    ParameterAwarePolicy,
    PathRule,
    PolicyDecision,
    PolicyDecisionType,
    RateLimit,
    RateLimitScope,
    RiskBasedPolicy,
    SlidingWindowLimiter,
    ToolPolicy,
    extract_targets,
    limited_policy,
    normalize_workspace_path,
    path_matches,
)
from forgeharness.tools.process import run_command
from forgeharness.tools.registry import ToolRegistry

API_VERSION = 1

if TYPE_CHECKING:
    from forgeharness.skills.install import (
        InstalledSkill,
        InstallPolicy,
        SkillInstaller,
        SkillInstallError,
        SmokeResult,
    )
    from forgeharness.skills.loader import (
        LoadedSkill,
        SkillLoader,
        SkillLoadError,
        SkillManifest,
        SkillRegistry,
    )

_SKILL_EXPORTS = {
    "InstalledSkill": "install",
    "InstallPolicy": "install",
    "SkillInstaller": "install",
    "SkillInstallError": "install",
    "SmokeResult": "install",
    "LoadedSkill": "loader",
    "SkillLoader": "loader",
    "SkillLoadError": "loader",
    "SkillManifest": "loader",
    "SkillRegistry": "loader",
}


def __getattr__(name: str) -> Any:
    """Load the optional Skills API only when that API is explicitly requested."""
    module = _SKILL_EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        value = getattr(import_module(f"forgeharness.skills.{module}"), name)
    except ModuleNotFoundError as exc:
        if exc.name == "yaml":
            raise ImportError("Skills require the forgeharness[skills] extra") from exc
        raise
    globals()[name] = value
    return value


__all__ = [
    "API_VERSION",
    "CONTAINER_WORKDIR",
    "DEFAULT_DENIED_COMMANDS",
    "DEFAULT_SECRET_PATH_RULES",
    "Actor",
    "AgentFinding",
    "AgentReport",
    "AgentRuntime",
    "AllOfVerifier",
    "ApplicationComposition",
    "ApplicationSpec",
    "ApprovalError",
    "ApprovalGrant",
    "ApprovalLedger",
    "AuthorizationRequest",
    "AuthorizedIndex",
    "BackendError",
    "BackendFactory",
    "BusinessReceipt",
    "CacheMode",
    "CachePlan",
    "CallTargets",
    "Capability",
    "CapacityError",
    "CheckpointConflict",
    "CheckpointStore",
    "Citation",
    "CitationVerifier",
    "CitedAnswer",
    "Collaboration",
    "CollaborationAgent",
    "CollaborationEvidence",
    "CollaborationPlan",
    "CollaborationSnapshot",
    "CollaborationTask",
    "CollaborationVerifier",
    "CommandRule",
    "CompositeBackend",
    "CompressionResult",
    "ContainerBackend",
    "ContainerLimits",
    "ContentRevision",
    "ContextCompiler",
    "ContextItem",
    "ContextMaterial",
    "ContextProvider",
    "ContextualPolicy",
    "CredentialMask",
    "DelegationSnapshot",
    "DurableGraph",
    "DurableNode",
    "DurableOrchestrator",
    "EvidenceTransform",
    "FileBackend",
    "FileConflict",
    "FileEntry",
    "FileTooLarge",
    "FinalAction",
    "FrozenModel",
    "Harness",
    "HashChainedJSONLTrace",
    "ImportConflict",
    "ImportOperation",
    "ImportRequest",
    "ImportService",
    "ImportUnavailable",
    "InMemoryApprovalLedger",
    "InMemoryTrace",
    "IndexConflict",
    "IndexEntry",
    "IndexHit",
    "InstallPolicy",
    "InstalledSkill",
    "InvocationJournal",
    "KernelBudgetExceeded",
    "KernelToolDenied",
    "LoadedSkill",
    "LocalBackend",
    "MaterialParser",
    "MaterialValidator",
    "Message",
    "MessageRole",
    "MessageSpill",
    "Model",
    "ModelCapability",
    "ModelDispatcher",
    "ModelRequest",
    "ModelRequestRejected",
    "ModelResult",
    "ModelUsage",
    "NoopVerifier",
    "NotRegularFile",
    "ObservationCompressor",
    "OrchestrationError",
    "Orchestrator",
    "ParallelExecution",
    "ParameterAwarePolicy",
    "ParsedDocument",
    "ParsedSpan",
    "PathRule",
    "PolicyDecision",
    "PolicyDecisionType",
    "PreparedModelCall",
    "PreparingModel",
    "PublishedContextProvider",
    "PublishedRecordTool",
    "RateLimit",
    "RateLimitScope",
    "ReadResult",
    "ReconciledNode",
    "RecordConflict",
    "RecordDependency",
    "RecordDraft",
    "RecordOperation",
    "RecordPolicy",
    "RecordUnavailable",
    "ResourceScope",
    "RetentionPolicy",
    "RetrievalUnavailable",
    "RevisionStore",
    "RiskBasedPolicy",
    "RiskLevel",
    "RoleRecordPolicy",
    "RunBudget",
    "RunContext",
    "RunKernel",
    "RunResult",
    "RunStatus",
    "SQLiteApprovalLedger",
    "SQLiteCheckpointStore",
    "SQLiteInvocationJournal",
    "ScopeError",
    "ScriptedModel",
    "Session",
    "SessionConflict",
    "SessionOperation",
    "SessionService",
    "SessionTurn",
    "SessionUnavailable",
    "SkillInstallError",
    "SkillInstaller",
    "SkillLoadError",
    "SkillLoader",
    "SkillManifest",
    "SkillRegistry",
    "SlidingWindowLimiter",
    "SmokeResult",
    "SourceAnchor",
    "SourceRef",
    "SpillCorrupt",
    "SpillError",
    "SpillNotFound",
    "SpillUnavailable",
    "StagedMaterial",
    "TaskConflict",
    "TaskHandler",
    "TaskOperation",
    "TaskOutcome",
    "TaskRecord",
    "TaskRunner",
    "TaskState",
    "TaskStore",
    "TaskUnavailable",
    "TodoBoard",
    "TodoItem",
    "TodoSnapshot",
    "TodoStatus",
    "Tool",
    "ToolAction",
    "ToolCall",
    "ToolContext",
    "ToolDispatcher",
    "ToolEffectClass",
    "ToolEvidence",
    "ToolFailure",
    "ToolOutput",
    "ToolPolicy",
    "ToolRegistry",
    "ToolSpec",
    "TraceRecorder",
    "TransientToolError",
    "Usage",
    "ValidationOutcome",
    "VerificationRequest",
    "VerificationResult",
    "Verifier",
    "WarmContainerPool",
    "WorkflowBudget",
    "WorkflowResult",
    "WorkflowRunner",
    "WorkflowSpec",
    "WorkflowStep",
    "WorkflowVerificationRequest",
    "WorkflowVerifier",
    "WorkspacePathError",
    "WriteResult",
    "WriteTodosTool",
    "anchors_for",
    "collaboration_graph",
    "compose_application",
    "composite_factory",
    "container_argv",
    "container_backend_factory",
    "docker_runner",
    "exec_argv",
    "extract_targets",
    "governed_source_refs",
    "limited_policy",
    "local_backend",
    "normalize_workspace_path",
    "observation_spill_dir",
    "path_matches",
    "plan_cache",
    "planner_for",
    "pooled_argv",
    "render_todos",
    "resolve_workspace_path",
    "run_command",
    "verify_trace",
]
