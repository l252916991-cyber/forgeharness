"""Validated and time-bounded tool execution."""

from __future__ import annotations

import asyncio
from time import monotonic

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError
from pydantic import ValidationError

from forgeharness.domain.models import FrozenModel, ToolCall
from forgeharness.tools.base import SchemaSource, ToolContext, ToolOutput
from forgeharness.tools.registry import ToolRegistry


class DispatchResult(FrozenModel):
    """Tool result plus Harness-owned execution metadata."""

    output: ToolOutput
    elapsed_ms: int


class ToolDispatcher:
    """Resolve, validate, and execute tools without leaking exceptions to the loop."""

    def __init__(self, registry: ToolRegistry, *, timeout_seconds: float = 30.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._registry = registry
        self._timeout_seconds = timeout_seconds

    async def dispatch(self, call: ToolCall, context: ToolContext) -> DispatchResult:
        """Execute one call and convert failures into structured observations."""
        started = monotonic()
        tool = self._registry.get(call.name)
        if tool is None:
            return self._result(started, ok=False, content=f"unknown tool: {call.name}")
        try:
            if tool.spec.schema_source == SchemaSource.EXTERNAL_JSON_SCHEMA:
                Draft202012Validator(tool.spec.input_schema).validate(call.arguments)
            arguments = tool.input_model.model_validate(call.arguments)
        except (ValidationError, JSONSchemaValidationError) as exc:
            return self._result(started, ok=False, content=f"invalid tool arguments: {exc}")
        try:
            async with asyncio.timeout(self._timeout_seconds):
                output = await tool.execute(arguments, context)
        except TimeoutError:
            return self._result(started, ok=False, content="tool execution timed out")
        except Exception as exc:
            return self._result(
                started,
                ok=False,
                content=f"tool execution failed: {type(exc).__name__}: {exc}",
            )
        return DispatchResult(output=output, elapsed_ms=self._elapsed_ms(started))

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, int((monotonic() - started) * 1000))

    @classmethod
    def _result(cls, started: float, *, ok: bool, content: str) -> DispatchResult:
        return DispatchResult(
            output=ToolOutput(ok=ok, content=content), elapsed_ms=cls._elapsed_ms(started)
        )
