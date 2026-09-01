"""Tool registration with duplicate and schema checks."""

from __future__ import annotations

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from forgeharness.tools.base import SchemaSource, Tool, ToolSpec


class ToolRegistry:
    """Own the unique set of tools available to one runtime composition."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register one tool and reject ambiguous names or schemas."""
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        if tool.spec.schema_source == SchemaSource.PYDANTIC:
            generated_schema = tool.input_model.model_json_schema()
            if generated_schema != tool.spec.input_schema:
                raise ValueError(f"tool schema does not match input model: {name}")
        else:
            try:
                Draft202012Validator.check_schema(tool.spec.input_schema)
            except SchemaError as exc:
                raise ValueError(f"invalid external JSON schema for tool {name}: {exc}") from exc
            if tool.spec.input_schema.get("type") != "object":
                raise ValueError(f"external tool schema must have object root: {name}")
        self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        """Return a named tool, or `None` for an unknown model request."""
        return self._tools.get(name)

    def specs(self) -> tuple[ToolSpec, ...]:
        """Return model-visible specs in deterministic name order."""
        return tuple(self._tools[name].spec for name in sorted(self._tools))
