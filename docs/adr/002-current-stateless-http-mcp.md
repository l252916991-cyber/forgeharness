# ADR-002: Target the current stateless HTTP MCP lifecycle

- Status: accepted
- Date: 2026-09-01
- Protocol revision: 2026-07-28

## Context

The MCP protocol changed its transport and lifecycle model. Implementing examples from older SDKs would add initialization and session behavior that the current specification has retired, while hiding the same tool policy boundary that ForgeHarness is intended to demonstrate.

## Decision

Implement a small stateless HTTP JSON-RPC client for `tools/list` pagination and `tools/call`. Each request sends the current protocol version and request metadata. Remote tool schemas enter the native registry as external JSON Schema and therefore reuse the same validation, policy, dispatcher, timeout, and trace path as native tools.

The initial adapter accepts JSON responses and explicitly rejects `text/event-stream`; it does not implement legacy initialization/session negotiation.

## Consequences

The supported subset is easy to test and audit, but it is not marketed as complete MCP coverage. Streaming, other capability families, authentication profiles, and transport compatibility require later adapters and conformance cases.
