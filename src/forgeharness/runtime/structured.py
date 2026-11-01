"""Local JSON-schema validation at the model-result boundary."""

from __future__ import annotations

import json
import math
from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


def validate_output_schema(schema: dict[str, Any]) -> None:
    """Accept bounded local schemas; never resolve a schema over the network."""
    if len(json.dumps(schema, allow_nan=False)) > 65_536:
        raise ValueError("model output schema is too large")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError("invalid model output schema") from exc
    stack: list[Any] = [schema]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if "$id" in value or "$dynamicRef" in value:
                raise ValueError("schema identifiers and dynamic references are unsupported")
            if "$ref" in value:
                ref = value["$ref"]
                if not isinstance(ref, str) or not ref.startswith("#/"):
                    raise ValueError("only local JSON pointer schema references are supported")
                target: Any = schema
                try:
                    for part in ref[2:].split("/"):
                        target = target[part.replace("~1", "/").replace("~0", "~")]
                except (KeyError, TypeError) as exc:
                    raise ValueError("unresolved local schema reference") from exc
                if not isinstance(target, dict | bool):
                    raise ValueError("local reference must target a schema")
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _invalid_constant(value: str) -> None:
    raise ValueError("non-finite JSON number: " + value)


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def parse_structured_output(content: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Reject ambiguous JSON and validate before another node consumes it."""
    if len(content) > 65_536:
        raise ValueError("structured model output is too large")
    try:
        validate_output_schema(schema)
        value = json.loads(
            content,
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
            parse_float=_finite_float,
        )
        if not isinstance(value, dict):
            raise ValueError("structured model output must be an object")
        Draft202012Validator(schema).validate(value)
    except Exception as exc:
        # Schema resolution errors also fail closed. No provider retry or resource
        # lookup is permitted as a side effect of accepting a result.
        raise ValueError("invalid structured model output") from exc
    return cast(dict[str, Any], value)
