"""Tool contracts shared by native and protocol adapters."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from forgeharness.domain.models import FrozenModel, Usage


class RiskLevel(StrEnum):
    """Coarse side-effect level used as policy input."""

    READ = "read"
    WRITE = "write"
    PROCESS = "process"
    NETWORK = "network"


class SchemaSource(StrEnum):
    """Authority used to validate a tool's advertised input schema."""

    PYDANTIC = "pydantic"
    EXTERNAL_JSON_SCHEMA = "external_json_schema"


class ToolSpec(FrozenModel):
    """Model-visible tool metadata and Harness-visible risk metadata."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    description: str = Field(min_length=1, max_length=1000)
    input_schema: dict[str, Any]
    risk: RiskLevel = RiskLevel.READ
    schema_source: SchemaSource = SchemaSource.PYDANTIC


class ToolContext(BaseModel):
    """Runtime-owned values passed to a tool after policy approval."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    task_id: str
    workspace: Path


class ToolOutput(FrozenModel):
    """Structured result returned to the model and trace."""

    ok: bool
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    usage: Usage | None = None


class Tool(Protocol):
    """Executable capability registered with the Harness."""

    @property
    def spec(self) -> ToolSpec:
        """Return stable metadata for validation, policy, and model requests."""
        ...

    @property
    def input_model(self) -> type[BaseModel]:
        """Return the Pydantic model that validates invocation arguments."""
        ...

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        """Execute already validated and policy-approved arguments."""
        ...
