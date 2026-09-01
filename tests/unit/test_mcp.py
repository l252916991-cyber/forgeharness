"""Tests for current stateless MCP requests and native tool adaptation."""

import json
from pathlib import Path

import httpx
import pytest

from forgeharness.domain.models import ToolCall
from forgeharness.tools.base import RiskLevel, ToolContext
from forgeharness.tools.dispatcher import ToolDispatcher
from forgeharness.tools.mcp import (
    MCPClientConfig,
    MCPProtocolError,
    StatelessHTTPMCPClient,
    discover_mcp_tools,
)
from forgeharness.tools.registry import ToolRegistry


def client_with_responses(
    responses: list[dict[str, object]], captured: list[httpx.Request]
) -> StatelessHTTPMCPClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        response = responses.pop(0)
        return httpx.Response(200, headers={"content-type": "application/json"}, json=response)

    return StatelessHTTPMCPClient(
        MCPClientConfig(endpoint="https://mcp.example/mcp", bearer_token="token"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_client_lists_pages_without_initialize_handshake() -> None:
    captured: list[httpx.Request] = []
    client = client_with_responses(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "tools": [
                        {
                            "name": "search",
                            "description": "Search data",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"q": {"type": "string"}},
                                "required": ["q"],
                            },
                        }
                    ],
                    "nextCursor": "page-2",
                },
            },
            {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}},
        ],
        captured,
    )

    tools = await client.list_tools()

    assert [tool.name for tool in tools] == ["search"]
    assert len(captured) == 2
    first = json.loads(captured[0].content)
    second = json.loads(captured[1].content)
    assert first["method"] == "tools/list"
    assert second["params"]["cursor"] == "page-2"
    assert captured[0].headers["MCP-Protocol-Version"] == "2026-07-28"
    assert captured[0].headers["Authorization"] == "Bearer token"
    assert all(json.loads(request.content)["method"] != "initialize" for request in captured)


async def test_discovered_tool_uses_registry_validation_and_dispatch(tmp_path: Path) -> None:
    captured: list[httpx.Request] = []
    client = client_with_responses(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "tools": [
                        {
                            "name": "lookup",
                            "description": "Lookup a value",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"key": {"type": "string"}},
                                "required": ["key"],
                                "additionalProperties": False,
                            },
                        }
                    ]
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"content": [{"type": "text", "text": "value"}], "isError": False},
            },
        ],
        captured,
    )
    remote = (await discover_mcp_tools(client, server_name="demo", risk=RiskLevel.READ))[0]
    registry = ToolRegistry()
    registry.register(remote)
    dispatcher = ToolDispatcher(registry)

    invalid = await dispatcher.dispatch(
        ToolCall(id="bad", name="mcp.demo.lookup", arguments={}),
        ToolContext(task_id="task", workspace=tmp_path),
    )
    valid = await dispatcher.dispatch(
        ToolCall(id="ok", name="mcp.demo.lookup", arguments={"key": "answer"}),
        ToolContext(task_id="task", workspace=tmp_path),
    )

    assert invalid.output.ok is False
    assert invalid.output.content.startswith("invalid tool arguments:")
    assert valid.output.content == "value"
    call = json.loads(captured[-1].content)
    assert call["method"] == "tools/call"
    assert call["params"]["arguments"] == {"key": "answer"}
    assert captured[-1].headers["Mcp-Name"] == "lookup"


async def test_client_rejects_json_rpc_error_and_wrong_media_type() -> None:
    error_client = client_with_responses(
        [{"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "missing"}}],
        [],
    )
    with pytest.raises(MCPProtocolError, match="error response"):
        await error_client.list_tools()

    async def sse_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="data: {}")

    sse_client = StatelessHTTPMCPClient(
        MCPClientConfig(endpoint="https://mcp.example/mcp"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(sse_handler)),
    )
    with pytest.raises(MCPProtocolError, match="unsupported MCP response media type"):
        await sse_client.list_tools()
