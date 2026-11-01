"""Async, operation-level execution boundary for hosted runtimes.

No database or cloud imports belong here. A backend owns atomic admission,
checkpoint/evidence commits, current authorization and physical-call recovery.
It must persist a send permit before invoking a dispatch closure. A closure is
single-use; exceptions or transport timeouts never imply nonexecution.

The local synchronous stores retain their existing contracts. This port is not
a declaration that any particular backend implements hosted execution yet.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol

from forgeharness.domain.models import RunResult, ToolCall, Usage
from forgeharness.models.prepared import PreparedModelCall
from forgeharness.state.approval import ApprovalGrant
from forgeharness.tools.dispatcher import DispatchResult

if TYPE_CHECKING:
    from forgeharness.runtime.model_call import ModelCallOutcome


class ExecutionSuspended(RuntimeError):
    """Durable execution needs reconciliation; do not report a terminal failure."""


class AsyncExecutionBackend(Protocol):
    """Trusted service boundary, never constructed from model/user arguments.

    The backend generates and commits ``checkpoint.saved`` and
    ``approval.consumed`` inside the corresponding state transaction. The core
    never appends these audit events after a hosted operation returns.
    """

    async def save(self, result: RunResult) -> RunResult:
        """CAS checkpoint plus pending evidence and checkpoint.saved atomically."""
        ...

    async def assert_current(self, previous: RunResult) -> None:
        """Compare the complete binding/revision and current execution ownership."""
        ...

    async def consume_approval(
        self,
        grant: ApprovalGrant,
        *,
        previous: RunResult,
        candidate: RunResult,
        call: ToolCall,
    ) -> RunResult:
        """Consume/admit/CAS and commit approval.consumed/checkpoint.saved atomically."""
        ...

    async def invoke_model(
        self,
        *,
        prepared: PreparedModelCall,
        usage: Usage,
        phase: str,
        dispatch: Callable[[], Awaitable[ModelCallOutcome]],
    ) -> ModelCallOutcome:
        """Persist exact request/permit or reuse confirmed result; never blindly resend."""
        ...

    async def invoke_tool(
        self,
        *,
        call: ToolCall,
        usage: Usage,
        task_id: str,
        checkpoint_revision: int,
        dispatch: Callable[[str], Awaitable[DispatchResult]],
    ) -> DispatchResult:
        """Persist outcome; dispatch receives the exact durably admitted idempotency key."""
        ...

    async def recover_tool(self, *, task_id: str, call: ToolCall) -> DispatchResult | None:
        """Reuse confirmed output; None means proven unadmitted, unknown suspends."""
        ...
