"""Complete hosted invocation facts, exact identities and recovery accounting."""

from typing import Any

import pytest
from pydantic import ValidationError

from forgeharness.cloud.execution_contract import (
    CallEnvelope,
    PhysicalResult,
    ProtocolErrorFact,
    canon_result_digest,
    canonical_result_digest,
    idempotency_key_for,
    invocation_id_for,
)
from forgeharness.domain.models import FinalAction, ModelResult, ModelUsage, Usage
from forgeharness.models.base import ModelProtocolError
from forgeharness.runtime.model_call import ModelCallKind
from forgeharness.runtime.tool_executor import ToolExecutor
from forgeharness.tools.base import ToolErrorCode, ToolFailure, ToolOutput
from forgeharness.tools.dispatcher import DispatchResult


def envelope(**changes: Any) -> CallEnvelope:
    values: dict[str, Any] = {
        "kind": "model",
        "logical_id": "model-1",
        "canonical_input": {"messages": [{"role": "user", "content": "hello"}]},
        "reservation": Usage(steps=1, input_tokens=20, output_tokens=10),
        "reserve_units": 5,
    }
    return CallEnvelope(**(values | changes))


def model_fact(**changes: Any) -> PhysicalResult:
    values: dict[str, Any] = {
        "kind": "model",
        "receipt_id": "provider-receipt-1",
        "actual_units": 3,
        "model_result": ModelResult(
            action=FinalAction(content="confirmed"),
            usage=ModelUsage(input_tokens=11, output_tokens=7),
            model_name="fixture",
        ),
    }
    return PhysicalResult(**(values | changes))


def test_call_identity_is_stable_and_namespaces_kind_and_logical_id() -> None:
    model = envelope()
    same = envelope()
    tool = envelope(kind="tool")
    other = envelope(logical_id="model-2")

    assert model.invocation_id == same.invocation_id
    assert model.invocation_id == invocation_id_for("model", "model-1")
    assert len({model.invocation_id, tool.invocation_id, other.invocation_id}) == 3
    assert len(model.invocation_id) <= 128


def test_idempotency_key_binds_org_run_deployment_and_invocation() -> None:
    call = envelope()
    original = call.idempotency_key("org-a", "run-a", "a" * 64)

    assert original == idempotency_key_for("org-a", "run-a", "a" * 64, call.invocation_id)
    alternatives = {
        call.idempotency_key("org-b", "run-a", "a" * 64),
        call.idempotency_key("org-a", "run-b", "a" * 64),
        call.idempotency_key("org-a", "run-a", "b" * 64),
        envelope(logical_id="other").idempotency_key("org-a", "run-a", "a" * 64),
    }
    assert original not in alternatives
    assert len(alternatives) == 4
    assert len(original) <= 128


@pytest.mark.parametrize("dimension", list(Usage.model_fields))
def test_arguments_digest_binds_every_usage_dimension(dimension: str) -> None:
    original = envelope()
    changed_usage = original.reservation.model_dump()
    changed_usage[dimension] += 1
    changed = envelope(reservation=changed_usage)

    assert original.invocation_id == changed.invocation_id
    assert original.arguments_digest != changed.arguments_digest


def test_arguments_digest_binds_input_and_is_independent_of_object_key_order() -> None:
    first = envelope(canonical_input={"a": 1, "b": {"x": False, "y": [1, 2]}})
    reordered = envelope(canonical_input={"b": {"y": [1, 2], "x": False}, "a": 1})
    changed = envelope(canonical_input={"a": 2, "b": {"x": False, "y": [1, 2]}})

    assert first.arguments_digest == reordered.arguments_digest
    assert first.arguments_digest != changed.arguments_digest


def test_contract_detaches_input_and_freezes_top_level_fields() -> None:
    raw = {"items": [{"value": "initial"}]}
    call = envelope(canonical_input=raw)
    digest = call.arguments_digest
    raw["items"][0]["value"] = "modified"

    assert call.canonical_input["items"][0]["value"] == "initial"
    assert call.arguments_digest == digest
    with pytest.raises(ValidationError):
        call.reserve_units = 6


@pytest.mark.parametrize("bad", [True, False, 1.0, "1", -1])
def test_units_are_strict_nonnegative_integers(bad: Any) -> None:
    with pytest.raises(ValidationError):
        envelope(reserve_units=bad)
    with pytest.raises(ValidationError):
        model_fact(actual_units=bad)


@pytest.mark.parametrize("dimension", list(Usage.model_fields))
@pytest.mark.parametrize("bad", [True, 1.0, "1", -1])
def test_reservation_counters_are_strict(dimension: str, bad: Any) -> None:
    reservation: dict[str, Any] = Usage().model_dump()
    reservation[dimension] = bad
    with pytest.raises(ValidationError):
        envelope(reservation=reservation)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), object()])
def test_canonical_input_rejects_nonfinite_and_non_json_values(bad: Any) -> None:
    with pytest.raises(ValidationError):
        envelope(canonical_input={"value": bad})


def test_documents_reject_invalid_keys_unicode_depth_and_bytes() -> None:
    with pytest.raises(ValidationError):
        envelope(canonical_input={1: "invalid key"})
    with pytest.raises(ValidationError):
        envelope(canonical_input={"invalid": "\ud800"})
    nested: dict[str, Any] = {}
    for _ in range(40):
        nested = {"next": nested}
    with pytest.raises(ValidationError):
        envelope(canonical_input=nested)
    with pytest.raises(ValidationError):
        envelope(canonical_input={"text": "x" * (16 * 1024 * 1024)})


@pytest.mark.parametrize(
    "org_id,run_id,binding,invocation",
    [
        ("", "run", "a" * 64, "call"),
        ("org", "../run", "a" * 64, "call"),
        ("org", "run", "not-a-digest", "call"),
        ("org", "run", "a" * 64, ""),
    ],
)
def test_identity_helpers_reject_invalid_bindings(
    org_id: str, run_id: str, binding: str, invocation: str
) -> None:
    with pytest.raises(ValueError):
        idempotency_key_for(org_id, run_id, binding, invocation)


@pytest.mark.parametrize("logical_id", ["", "\x00", "x" * 257])
def test_logical_identity_is_bounded(logical_id: str) -> None:
    with pytest.raises(ValueError):
        invocation_id_for("model", logical_id)


def test_successful_model_fact_restores_complete_response_and_cumulative_usage() -> None:
    fact = model_fact()
    restored = PhysicalResult.model_validate_json(fact.model_dump_json())
    before = Usage(steps=2, tool_calls=4, input_tokens=20, output_tokens=5)

    outcome = restored.restore_model(before)

    assert fact.actual_usage == Usage(steps=1, input_tokens=11, output_tokens=7)
    assert outcome.kind is ModelCallKind.SUCCESS
    assert outcome.usage == Usage(steps=3, tool_calls=4, input_tokens=31, output_tokens=12)
    assert outcome.result == fact.model_result
    assert outcome.response_received
    assert not outcome.not_sent


@pytest.mark.parametrize("recoverable", [True, False])
def test_protocol_fact_preserves_exception_semantics_and_all_confirmed_fees(
    recoverable: bool,
) -> None:
    protocol = ProtocolErrorFact(
        code="multiple_tool_calls_not_allowed",
        message="response supplied two actions",
        received_tool_calls=2,
        recoverable=recoverable,
        usage=ModelUsage(input_tokens=80, output_tokens=12),
    )
    fact = PhysicalResult(
        kind="model", receipt_id="protocol-1", actual_units=7, protocol_error=protocol
    )

    outcome = fact.restore_model(Usage(steps=2, tool_calls=3, input_tokens=20, output_tokens=5))

    assert fact.actual_usage == Usage(steps=1, input_tokens=80, output_tokens=12)
    assert outcome.usage == Usage(steps=3, tool_calls=3, input_tokens=100, output_tokens=17)
    assert outcome.response_received
    assert isinstance(outcome.violation, ModelProtocolError)
    assert str(outcome.violation) == protocol.message
    assert outcome.violation.code == protocol.code
    assert outcome.violation.received_tool_calls == 2
    assert outcome.violation.recoverable is recoverable
    assert outcome.violation.usage == protocol.usage
    if recoverable:
        assert outcome.kind is ModelCallKind.PROTOCOL_VIOLATION
        assert outcome.error is None
    else:
        assert outcome.kind is ModelCallKind.MODEL_FAILED
        assert outcome.error_type == "ModelProtocolError"


@pytest.mark.parametrize("usage", [None, {"input_tokens": True}, {"output_tokens": "2"}])
def test_protocol_fact_requires_trustworthy_strict_usage(usage: Any) -> None:
    with pytest.raises(ValidationError):
        ProtocolErrorFact(code="invalid", message="rejected", recoverable=False, usage=usage)


@pytest.mark.parametrize("field", ["received_tool_calls", "recoverable"])
def test_protocol_metadata_rejects_integer_boolean_coercion(field: str) -> None:
    values: dict[str, Any] = {
        "code": "invalid",
        "message": "rejected",
        "recoverable": False,
        "usage": ModelUsage(),
    }
    values[field] = True if field == "received_tool_calls" else 1
    with pytest.raises(ValidationError):
        ProtocolErrorFact(**values)


@pytest.mark.parametrize("ok", [True, False])
def test_tool_result_accounts_physical_attempt_and_nested_usage_even_on_failure(ok: bool) -> None:
    nested = Usage(steps=2, tool_calls=3, input_tokens=20, output_tokens=5)
    output = ToolOutput(
        ok=ok,
        content="tool response",
        usage=nested,
        error=None
        if ok
        else ToolFailure(code=ToolErrorCode.TIMEOUT, message="request timed out", recoverable=True),
    )
    dispatch = DispatchResult(output=output, elapsed_ms=7)
    fact = PhysicalResult(
        kind="tool", receipt_id="tool-receipt", actual_units=5, tool_result=dispatch
    )
    restored = PhysicalResult.model_validate_json(fact.model_dump_json())

    assert restored.tool_result == dispatch
    assert restored.actual_usage == ToolExecutor.account_usage(Usage(), nested)
    assert restored.actual_usage == Usage(steps=2, tool_calls=4, input_tokens=20, output_tokens=5)
    assert restored.actual_units == 5
    with pytest.raises(ValueError, match="tool result"):
        restored.restore_model(Usage())


def test_tool_without_child_usage_still_charges_one_physical_call() -> None:
    fact = PhysicalResult(
        kind="tool",
        receipt_id="tool-receipt",
        actual_units=0,
        tool_result=DispatchResult(output=ToolOutput(ok=True, content="done"), elapsed_ms=0),
    )
    assert fact.actual_usage == Usage(tool_calls=1)


@pytest.mark.parametrize("elapsed", [True, 1.0, "1", -1])
def test_tool_elapsed_time_is_a_strict_nonnegative_integer(elapsed: Any) -> None:
    with pytest.raises(ValidationError):
        PhysicalResult.model_validate(
            {
                "kind": "tool",
                "receipt_id": "tool-receipt",
                "actual_units": 0,
                "tool_result": {"output": {"ok": True, "content": "done"}, "elapsed_ms": elapsed},
            }
        )


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens"])
def test_model_reported_usage_rejects_booleans(field: str) -> None:
    with pytest.raises(ValidationError):
        model_fact(
            model_result={
                "action": {"kind": "final", "content": "confirmed"},
                "usage": {field: True},
            }
        )


def test_result_requires_exactly_one_matching_outcome() -> None:
    dispatch = DispatchResult(output=ToolOutput(ok=True, content="done"), elapsed_ms=0)
    common: dict[str, Any] = {"receipt_id": "r", "actual_units": 0}
    with pytest.raises(ValidationError):
        PhysicalResult(kind="model", **common)
    with pytest.raises(ValidationError):
        PhysicalResult(kind="model", tool_result=dispatch, **common)
    with pytest.raises(ValidationError):
        model_fact(kind="tool")
    with pytest.raises(ValidationError):
        model_fact(tool_result=dispatch)


def test_fact_digest_binds_complete_receipt_response_and_usage() -> None:
    original = model_fact()
    restored = PhysicalResult.model_validate_json(original.model_dump_json())

    assert canon_result_digest(original) == canonical_result_digest(restored)
    assert canon_result_digest(original) != canon_result_digest(model_fact(actual_units=4))
    assert canon_result_digest(original) != canon_result_digest(model_fact(receipt_id="other"))
    assert canon_result_digest(original) != canon_result_digest(
        model_fact(model_result=ModelResult(action=FinalAction(content="changed")))
    )


def test_tool_digest_ignores_only_host_elapsed_time_and_does_not_mutate_payload() -> None:
    original = PhysicalResult(
        kind="tool",
        receipt_id="r",
        actual_units=5,
        tool_result=DispatchResult(
            output=ToolOutput(
                ok=True,
                content="confirmed",
                usage=Usage(input_tokens=7),
                metadata={"business_version": "v1"},
            ),
            elapsed_ms=41,
        ),
    )
    assert original.tool_result is not None
    resolver = original.model_copy(
        update={"tool_result": original.tool_result.model_copy(update={"elapsed_ms": 0})}
    )

    digest = canon_result_digest(original)

    assert digest == canon_result_digest(resolver)
    assert original.tool_result.elapsed_ms == 41
    assert digest != canon_result_digest(original.model_copy(update={"receipt_id": "other"}))
    assert digest != canon_result_digest(original.model_copy(update={"actual_units": 6}))
    for changed in (
        original.tool_result.output.model_copy(update={"content": "other"}),
        original.tool_result.output.model_copy(update={"usage": Usage(input_tokens=8)}),
        original.tool_result.output.model_copy(update={"metadata": {"business_version": "v2"}}),
    ):
        changed_fact = original.model_copy(
            update={"tool_result": original.tool_result.model_copy(update={"output": changed})}
        )
        assert digest != canon_result_digest(changed_fact)


def test_credentials_are_rejected_in_inputs_results_and_protocol_facts() -> None:
    with pytest.raises(ValidationError, match="credentials"):
        envelope(canonical_input={"password": "synthetic-value"})
    with pytest.raises(ValidationError, match="credentials"):
        model_fact(model_result=ModelResult(action=FinalAction(content="password=synthetic-value")))
    with pytest.raises(ValidationError, match="credentials"):
        ProtocolErrorFact(
            code="rejected",
            message="password=synthetic-value",
            recoverable=False,
            usage=ModelUsage(),
        )


def test_nested_fact_metadata_rejects_nonfinite_numbers() -> None:
    with pytest.raises(ValidationError):
        PhysicalResult(
            kind="tool",
            receipt_id="r",
            actual_units=0,
            tool_result=DispatchResult(
                output=ToolOutput(ok=True, content="done", metadata={"value": float("nan")}),
                elapsed_ms=0,
            ),
        )


def test_digest_revalidates_mutated_nested_values() -> None:
    fact = PhysicalResult(
        kind="tool",
        receipt_id="r",
        actual_units=0,
        tool_result=DispatchResult(output=ToolOutput(ok=True, content="done"), elapsed_ms=0),
    )
    assert fact.tool_result is not None
    fact.tool_result.output.metadata["value"] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        canon_result_digest(fact)
