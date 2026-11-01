"""Bounded, typed facts for one hosted physical model or tool invocation.

These contracts validate complete inputs and results; they do not authenticate
providers or prove that a request executed. A trusted application must verify
each fact before storage. In particular, ``ToolOutput.ok=False`` is still a
physical result and never constitutes proof of nonexecution.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from forgeharness.cloud.models import MAX_UNITS, Frozen
from forgeharness.domain.models import ModelResult, ModelUsage, Usage
from forgeharness.models.base import ModelProtocolError
from forgeharness.runtime.model_call import ModelCallKind, ModelCallOutcome
from forgeharness.runtime.tool_executor import ToolExecutor
from forgeharness.sdk.privacy import CredentialMask
from forgeharness.tools.dispatcher import DispatchResult

CallKind = Literal["model", "tool"]
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_MAX_BYTES = 16 * 1024 * 1024
_MAX_DEPTH = 32
_MAX_NODES = 100_000


def _document(value: Any) -> Any:
    """Detach strict JSON, reject credentials, and bound its canonical bytes."""
    nodes = 0

    def copy(item: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if depth > _MAX_DEPTH or nodes > _MAX_NODES:
            raise ValueError("execution fact exceeds structure limits")
        kind = type(item)
        if item is None or kind is bool or kind is int:
            return item
        if kind is float:
            if not math.isfinite(item):
                raise ValueError("execution fact numbers must be finite")
            return item
        if kind is str:
            try:
                item.encode("utf-8")
            except UnicodeError:
                raise ValueError("execution fact contains invalid Unicode") from None
            return item
        if kind is list:
            return [copy(child, depth + 1) for child in item]
        if kind is dict:
            if any(type(key) is not str for key in item):
                raise ValueError("execution fact object keys must be strings")
            return {copy(key, depth + 1): copy(child, depth + 1) for key, child in item.items()}
        raise ValueError("execution fact must contain JSON values only")

    detached = copy(value, 0)
    encoded = _json(detached)
    if len(encoded.encode("utf-8")) > _MAX_BYTES:
        raise ValueError("execution fact exceeds byte limit")
    if CredentialMask().transform(detached) != detached:
        raise ValueError("execution fact contains credentials")
    return detached


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(_document(value)).encode("utf-8")).hexdigest()


def _identifier(value: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("execution identity needs 1-128 identifier characters")
    return value


def _logical_id(value: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= 256 or "\x00" in value:
        raise ValueError("logical call identity needs 1-256 non-NUL characters")
    _document(value)
    return value


def invocation_id_for(kind: CallKind, logical_id: str) -> str:
    """Stable within a run; namespaces model and tool logical call identities."""
    if type(kind) is not str or kind not in {"model", "tool"}:
        raise ValueError("unknown physical call kind")
    return "exec-" + _digest({"kind": kind, "logical_id": _logical_id(logical_id)})


def idempotency_key_for(org_id: str, run_id: str, binding_digest: str, invocation_id: str) -> str:
    """Bind replay identity to tenant, run, immutable deployment and invocation."""
    if type(binding_digest) is not str or _DIGEST.fullmatch(binding_digest) is None:
        raise ValueError("execution binding needs a SHA-256 digest")
    return "exec-key-" + _digest(
        {
            "org_id": _identifier(org_id),
            "run_id": _identifier(run_id),
            "binding_digest": binding_digest,
            "invocation_id": _identifier(invocation_id),
        }
    )


class _Contract(Frozen):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
        revalidate_instances="always",
    )


def _model_value(value: Any) -> Any:
    """Encode domain enums/tuples without JSON mode silently turning NaN into null."""
    nodes = 0

    def normalize(item: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if depth > _MAX_DEPTH or nodes > _MAX_NODES:
            raise ValueError("execution fact exceeds structure limits")
        if isinstance(item, StrEnum):
            return str(item)
        if type(item) is tuple or type(item) is list:
            return [normalize(child, depth + 1) for child in item]
        if type(item) is dict:
            return {key: normalize(child, depth + 1) for key, child in item.items()}
        return item

    return normalize(value, 0)


def _typed_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = _model_value(value.model_dump(mode="python", warnings=False))
    return _json(_document(value))


def _usage(value: Any) -> Usage:
    return Usage.model_validate_json(_typed_json(value), strict=True)


class CallEnvelope(_Contract):
    """Exact canonical input and complete parent-owned admission reservation."""

    kind: CallKind
    logical_id: str
    canonical_input: dict[str, Any]
    reservation: Usage
    reserve_units: int = Field(strict=True, ge=0, le=MAX_UNITS)

    @field_validator("logical_id")
    @classmethod
    def valid_logical_id(cls, value: str) -> str:
        return _logical_id(value)

    @field_validator("canonical_input", mode="before")
    @classmethod
    def canonical_document(cls, value: Any) -> Any:
        return _document(value)

    @field_validator("reservation", mode="before")
    @classmethod
    def strict_reservation(cls, value: Any) -> Usage:
        return _usage(value)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        _typed_json(self)
        return self

    @property
    def invocation_id(self) -> str:
        return invocation_id_for(self.kind, self.logical_id)

    @property
    def arguments_digest(self) -> str:
        """Approval also binds reserve_units through the governance CallRequest."""
        return _digest(
            {
                "kind": self.kind,
                "logical_id": self.logical_id,
                "canonical_input": self.canonical_input,
                "reservation": _usage(self.reservation).model_dump(mode="json"),
            }
        )

    def idempotency_key(self, org_id: str, run_id: str, binding_digest: str) -> str:
        return idempotency_key_for(org_id, run_id, binding_digest, self.invocation_id)


class ProtocolErrorFact(_Contract):
    """A confirmed protocol response with trustworthy provider-reported usage."""

    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=16_000)
    received_tool_calls: int | None = Field(default=None, strict=True, ge=0)
    recoverable: bool
    usage: ModelUsage

    @field_validator("usage", mode="before")
    @classmethod
    def strict_usage(cls, value: Any) -> ModelUsage:
        return ModelUsage.model_validate_json(_typed_json(value), strict=True)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        _typed_json(self)
        return self


class PhysicalResult(_Contract):
    """One confirmed result, complete enough to restore without dispatching."""

    receipt_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    actual_units: int = Field(strict=True, ge=0, le=MAX_UNITS)
    kind: CallKind
    model_result: ModelResult | None = None
    tool_result: DispatchResult | None = None
    protocol_error: ProtocolErrorFact | None = None

    @field_validator("model_result", mode="before")
    @classmethod
    def strict_model_result(cls, value: Any) -> ModelResult | None:
        if value is None:
            return None
        return ModelResult.model_validate_json(_typed_json(value), strict=True)

    @field_validator("tool_result", mode="before")
    @classmethod
    def strict_tool_result(cls, value: Any) -> DispatchResult | None:
        if value is None:
            return None
        result = DispatchResult.model_validate_json(_typed_json(value), strict=True)
        if result.elapsed_ms < 0:
            raise ValueError("tool elapsed milliseconds must be nonnegative")
        return result

    @field_validator("protocol_error", mode="before")
    @classmethod
    def strict_protocol_error(cls, value: Any) -> ProtocolErrorFact | None:
        if value is None:
            return None
        return ProtocolErrorFact.model_validate_json(_typed_json(value), strict=True)

    @model_validator(mode="after")
    def exact_result(self) -> Self:
        if (
            sum(
                value is not None
                for value in (self.model_result, self.tool_result, self.protocol_error)
            )
            != 1
        ):
            raise ValueError("a physical result needs exactly one complete outcome")
        if (self.kind == "tool") != (self.tool_result is not None):
            raise ValueError("physical result kind does not match its outcome")
        _typed_json(self)
        return self

    @property
    def actual_usage(self) -> Usage:
        if self.tool_result is not None:
            return ToolExecutor.account_usage(Usage(), self.tool_result.output.usage)
        reported = (
            self.model_result.usage
            if self.model_result is not None
            else self.protocol_error.usage
            if self.protocol_error is not None
            else None
        )
        if reported is None:
            raise ValueError("model result requires trustworthy reported usage")
        return Usage(
            steps=1, input_tokens=reported.input_tokens, output_tokens=reported.output_tokens
        )

    def restore_model(self, usage: Usage) -> ModelCallOutcome:
        """Restore protocol semantics while retaining every confirmed physical fee.

        Cloud charges even nonrecoverable protocol responses, which are known
        executed requests. The legacy synchronous path retains its own existing
        accounting. This operation performs no provider verification or I/O.
        """
        if self.kind != "model":
            raise ValueError("a tool result cannot restore a model outcome")
        previous = _usage(usage)
        delta = self.actual_usage
        charged = Usage(
            **{key: getattr(previous, key) + getattr(delta, key) for key in Usage.model_fields}
        )
        if self.model_result is not None:
            return ModelCallOutcome(
                kind=ModelCallKind.SUCCESS,
                usage=charged,
                result=self.model_result,
                response_received=True,
            )
        assert self.protocol_error is not None
        fact = self.protocol_error
        error = ModelProtocolError(
            fact.message,
            code=fact.code,
            received_tool_calls=fact.received_tool_calls,
            usage=fact.usage,
            recoverable=fact.recoverable,
        )
        return ModelCallOutcome(
            kind=(
                ModelCallKind.PROTOCOL_VIOLATION if fact.recoverable else ModelCallKind.MODEL_FAILED
            ),
            usage=charged,
            error=None if fact.recoverable else f"model failed: ModelProtocolError: {fact.message}",
            error_type=None if fact.recoverable else "ModelProtocolError",
            violation=error,
            response_received=True,
        )


def canon_result_digest(result: PhysicalResult) -> str:
    """Bind semantic receipt facts, excluding host-observed tool duration.

    Reconciliation may prove the same output and fees without reconstructing a
    worker's elapsed time. Storage retains the first complete payload; duration
    is normalized only for duplicate-fact comparison, never changed in-place.
    """
    validated = PhysicalResult.model_validate_json(_typed_json(result), strict=True)
    document = validated.model_dump(mode="json")
    if document["tool_result"] is not None:
        document["tool_result"]["elapsed_ms"] = 0
    return _digest(document)


canonical_result_digest = canon_result_digest
