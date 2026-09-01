"""Stateless MCP 2026-07-28 HTTP client and Tool adapter."""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationError

from forgeharness.domain.models import FrozenModel
from forgeharness.tools.base import (
    RiskLevel,
    SchemaSource,
    ToolContext,
    ToolOutput,
    ToolSpec,
)

MCP_PROTOCOL_VERSION = "2026-07-28"


class MCPProtocolError(RuntimeError):
    """An MCP peer returned an invalid or unsupported response."""


class MCPClientConfig(BaseModel):
    """Connection and identity settings for stateless MCP HTTP requests."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    endpoint: str
    client_name: str = "forgeharness"
    client_version: str = "0.1.0"
    bearer_token: str | None = None
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_pages: int = Field(default=100, ge=1, le=1_000)
    max_tools: int = Field(default=1_000, ge=1, le=10_000)


class MCPToolDescription(FrozenModel):
    """Remote tool metadata returned by `tools/list`."""

    name: str
    description: str = "MCP tool"
    input_schema: dict[str, Any] = Field(alias="inputSchema")


class MCPToolResult(FrozenModel):
    """Relevant result fields returned by `tools/call`."""

    content: tuple[dict[str, Any], ...] = ()
    is_error: bool = Field(default=False, alias="isError")


class MCPClient(Protocol):
    """Operations required by MCP-backed Harness tools."""

    async def list_tools(self) -> tuple[MCPToolDescription, ...]:
        """List every page of remote tools."""
        ...

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        """Invoke one remote tool."""
        ...


class StatelessHTTPMCPClient:
    """Use self-contained JSON-RPC requests from the 2026-07-28 MCP core."""

    def __init__(self, config: MCPClientConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(timeout=config.timeout_seconds)
        self._next_id = 1

    async def list_tools(self) -> tuple[MCPToolDescription, ...]:
        """Follow opaque cursors until all available tools are collected."""
        tools: list[MCPToolDescription] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(self._config.max_pages):
            params = self._params()
            if cursor is not None:
                params["cursor"] = cursor
            result = await self._request("tools/list", params=params)
            try:
                page = [MCPToolDescription.model_validate(item) for item in result.get("tools", [])]
            except ValidationError as exc:
                raise MCPProtocolError(f"invalid tools/list result: {exc}") from exc
            if len(tools) + len(page) > self._config.max_tools:
                raise MCPProtocolError("tools/list exceeded configured tool limit")
            tools.extend(page)
            next_cursor = result.get("nextCursor")
            if not next_cursor:
                return tuple(tools)
            if not isinstance(next_cursor, str):
                raise MCPProtocolError("tools/list nextCursor must be a string")
            if next_cursor in seen_cursors:
                raise MCPProtocolError("tools/list repeated a pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise MCPProtocolError("tools/list exceeded configured page limit")

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        """Invoke a remote tool with per-request client metadata."""
        result = await self._request(
            "tools/call",
            name=name,
            params={"name": name, "arguments": arguments, **self._params()},
        )
        try:
            return MCPToolResult.model_validate(result)
        except ValidationError as exc:
            raise MCPProtocolError(f"invalid tools/call result: {exc}") from exc

    async def _request(
        self, method: str, *, params: dict[str, Any], name: str | None = None
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "Mcp-Method": method,
        }
        if name is not None:
            headers["Mcp-Name"] = name
        if self._config.bearer_token is not None:
            headers["Authorization"] = f"Bearer {self._config.bearer_token}"
        response = await self._client.post(
            self._config.endpoint,
            headers=headers,
            json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            timeout=self._config.timeout_seconds,
        )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        if content_type != "application/json":
            raise MCPProtocolError(
                f"unsupported MCP response media type {content_type!r}; JSON response required"
            )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise MCPProtocolError("MCP response is not valid JSON") from exc
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            raise MCPProtocolError("MCP response is not a JSON-RPC 2.0 object")
        if payload.get("id") != request_id:
            raise MCPProtocolError("MCP response id does not match the request")
        if "error" in payload:
            raise MCPProtocolError(f"MCP error response: {payload['error']}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise MCPProtocolError("MCP response result must be an object")
        return result

    def _params(self) -> dict[str, Any]:
        return {
            "_meta": {
                "io.modelcontextprotocol/clientInfo": {
                    "name": self._config.client_name,
                    "version": self._config.client_version,
                },
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        }


class MCPArguments(RootModel[dict[str, Any]]):
    """Retain externally validated JSON object arguments."""


class MCPRemoteTool:
    """Adapt a discovered MCP tool to the native dispatcher and policy path."""

    input_model = MCPArguments

    def __init__(
        self,
        *,
        client: MCPClient,
        server_name: str,
        remote: MCPToolDescription,
        risk: RiskLevel = RiskLevel.NETWORK,
    ) -> None:
        local_name = f"mcp.{server_name}.{remote.name}"
        self._client = client
        self._remote_name = remote.name
        self.spec = ToolSpec(
            name=local_name,
            description=f"MCP {server_name}: {remote.description}",
            input_schema=remote.input_schema,
            risk=risk,
            schema_source=SchemaSource.EXTERNAL_JSON_SCHEMA,
        )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        """Call MCP only after native validation and policy authorization."""
        del context
        values = MCPArguments.model_validate(arguments).root
        result = await self._client.call_tool(self._remote_name, values)
        rendered: list[str] = []
        for item in result.content:
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                rendered.append(item["text"])
            else:
                rendered.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
        return ToolOutput(ok=not result.is_error, content="\n".join(rendered))


async def discover_mcp_tools(
    client: MCPClient, *, server_name: str, risk: RiskLevel = RiskLevel.NETWORK
) -> tuple[MCPRemoteTool, ...]:
    """Discover and adapt remote tools without silently changing their risk level."""
    return tuple(
        MCPRemoteTool(client=client, server_name=server_name, remote=remote, risk=risk)
        for remote in await client.list_tools()
    )
