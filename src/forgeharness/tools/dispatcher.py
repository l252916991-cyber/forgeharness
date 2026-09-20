"""Validated and time-bounded tool execution with classified failures."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError
from pydantic import ValidationError

from forgeharness.domain.models import FrozenModel, ToolCall
from forgeharness.tools.base import (
    SchemaSource,
    Tool,
    ToolContext,
    ToolErrorCode,
    ToolFailure,
    ToolOutput,
)
from forgeharness.tools.paths import WorkspacePathError
from forgeharness.tools.registry import ToolRegistry


class DispatchResult(FrozenModel):
    """Tool result plus Harness-owned execution metadata."""

    output: ToolOutput
    elapsed_ms: int


# Exception type -> (code, model-facing message, recoverable, suggestion).
# Classification is by *type*, never by parsing an exception message: a message can
# carry host paths, and matching on prose breaks the moment it is reworded.
_CLASSIFIED_EXCEPTIONS: tuple[tuple[type[BaseException], ToolErrorCode, str, bool, str], ...] = (
    (
        FileNotFoundError,
        ToolErrorCode.PATH_NOT_FOUND,
        "Requested path does not exist.",
        True,
        "Inspect the available paths before retrying.",
    ),
    (
        NotADirectoryError,
        ToolErrorCode.PATH_NOT_FOUND,
        "Requested path exists but is not a directory.",
        True,
        "Inspect the available paths before retrying.",
    ),
    (
        WorkspacePathError,
        ToolErrorCode.INVALID_ARGUMENTS,
        "The path is not a valid workspace-relative path.",
        True,
        "Use a relative path inside the workspace.",
    ),
)


class ToolDispatcher:
    """Resolve, validate, and execute tools without leaking exceptions to the loop."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        timeout_seconds: float | None = None,
        max_output_chars: int = 50_000,
    ) -> None:
        """Configure the dispatcher.

        ``timeout_seconds`` is an explicit composition override for every tool under
        this dispatcher (a suite known to run slow). When it is None each call uses
        the tool's own ``ToolSpec.timeout_seconds``, which is the normal case.
        """
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_output_chars < 100:
            raise ValueError("max_output_chars must be at least 100")
        self._registry = registry
        self._timeout_seconds = timeout_seconds
        self._max_output_chars = max_output_chars

    async def dispatch(self, call: ToolCall, context: ToolContext) -> DispatchResult:
        """Execute one call and convert failures into structured observations."""
        started = monotonic()
        tool = self._registry.get(call.name)
        if tool is None:
            return self._failure(
                started,
                ToolFailure(
                    code=ToolErrorCode.INVALID_ARGUMENTS,
                    message="No such tool is registered.",
                    recoverable=True,
                    suggestion="Choose one of the available tools.",
                ),
                exception_class="UnknownTool",
                extra_metadata={"tool": call.name},
            )
        try:
            if tool.spec.schema_source == SchemaSource.EXTERNAL_JSON_SCHEMA:
                Draft202012Validator(tool.spec.input_schema).validate(call.arguments)
            arguments = tool.input_model.model_validate(call.arguments)
        except (ValidationError, JSONSchemaValidationError) as exc:
            return self._invalid_arguments(started, call, type(exc).__name__)
        effective_timeout = self._effective_timeout(tool)
        try:
            async with asyncio.timeout(effective_timeout):
                output = await tool.execute(arguments, context)
        except TimeoutError as exc:
            return self._failure(
                started,
                ToolFailure(
                    code=ToolErrorCode.TIMEOUT,
                    message="The tool did not finish within its time limit.",
                    recoverable=True,
                    suggestion="Choose a narrower operation or a different tool.",
                ),
                exception_class=type(exc).__name__,
                extra_metadata={"timeout_seconds": effective_timeout},
            )
        except Exception as exc:
            return self._classify_exception(started, call, exc)
        return DispatchResult(
            output=self._bound_output(output), elapsed_ms=self._elapsed_ms(started)
        )

    def _effective_timeout(self, tool: Tool) -> float:
        """The ceiling for this call: an explicit override, else the tool's own."""
        return (
            self._timeout_seconds
            if self._timeout_seconds is not None
            else tool.spec.timeout_seconds
        )

    def _invalid_arguments(
        self, started: float, call: ToolCall, exception_class: str
    ) -> DispatchResult:
        """Report a schema violation without echoing the validator's prose."""
        return self._failure(
            started,
            ToolFailure(
                code=ToolErrorCode.INVALID_ARGUMENTS,
                message="The arguments do not match the tool's required schema.",
                recoverable=True,
                suggestion="Send arguments that satisfy the tool's input schema.",
            ),
            exception_class=exception_class,
            extra_metadata={"tool": call.name},
        )

    def _classify_exception(
        self, started: float, call: ToolCall, exc: BaseException
    ) -> DispatchResult:
        """Classify a tool exception into the frozen taxonomy.

        Details are taken from the model's own arguments rather than the exception,
        so a host path cannot reach the model. Anything unrecognised falls back to
        ``tool_execution_error`` and is marked non-recoverable, because a failure
        the harness cannot characterise is not one it can promise is retryable.
        """
        for exception_type, code, message, recoverable, suggestion in _CLASSIFIED_EXCEPTIONS:
            if isinstance(exc, exception_type):
                return self._failure(
                    started,
                    ToolFailure(
                        code=code,
                        message=message,
                        recoverable=recoverable,
                        details=_declared_details(call),
                        suggestion=suggestion,
                    ),
                    exception_class=type(exc).__name__,
                )
        return self._failure(
            started,
            ToolFailure(
                code=ToolErrorCode.TOOL_EXECUTION_ERROR,
                message="The tool failed while running.",
                recoverable=False,
                details=_declared_details(call),
            ),
            exception_class=type(exc).__name__,
        )

    def _bound_output(self, output: ToolOutput) -> ToolOutput:
        if len(output.content) <= self._max_output_chars:
            return output
        suffix = "\n[tool output truncated]"
        content = output.content[: self._max_output_chars - len(suffix)] + suffix
        return output.model_copy(
            update={
                "content": content,
                "metadata": {**output.metadata, "output_truncated": True},
            }
        )

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return max(0, int((monotonic() - started) * 1000))

    @classmethod
    def _failure(
        cls,
        started: float,
        failure: ToolFailure,
        *,
        exception_class: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> DispatchResult:
        """Build a failed result: structured for the model, diagnostic for the trace.

        The model sees ``failure.as_observation()``; the trace gets the exception
        class and error code, which is where host-facing detail belongs.
        """
        metadata: dict[str, Any] = {
            "error_code": failure.code.value,
            "exception_class": exception_class,
            "recoverable": failure.recoverable,
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        return DispatchResult(
            output=ToolOutput(
                ok=False,
                content=failure.as_observation(),
                error=failure,
                metadata=metadata,
            ),
            elapsed_ms=cls._elapsed_ms(started),
        )


def _declared_details(call: ToolCall) -> dict[str, str]:
    """Copy model-supplied string arguments that explain the failure.

    Only values the model itself provided are echoed, so the model sees what it
    asked for without the harness revealing where the workspace lives.
    """
    details: dict[str, str] = {}
    for key in ("path", "query"):
        value = call.arguments.get(key)
        if isinstance(value, str):
            details[key] = value
    return details
