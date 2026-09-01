"""Safe built-in tools used by the initial keyless vertical slice."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from forgeharness.tools.base import RiskLevel, ToolContext, ToolOutput, ToolSpec


class EchoInput(BaseModel):
    """Validated arguments for the echo tool."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=1000)


class EchoTool:
    """Return validated text without external side effects."""

    @property
    def input_model(self) -> type[BaseModel]:
        """Return the echo argument model."""
        return EchoInput

    @property
    def spec(self) -> ToolSpec:
        """Return stable echo metadata."""
        return ToolSpec(
            name="echo",
            description="Return the supplied text unchanged.",
            input_schema=EchoInput.model_json_schema(),
            risk=RiskLevel.READ,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        """Echo the validated text."""
        del context
        validated = EchoInput.model_validate(arguments)
        return ToolOutput(ok=True, content=validated.text)
