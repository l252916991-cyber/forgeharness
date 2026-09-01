# ADR-001: Implement the core runtime directly

- Status: accepted
- Date: 2026-09-01

## Context

The target role evaluates understanding of Agent Loop, tools, context, memory, state, and Harness engineering. Building the core on a high-level orchestration framework would reduce implementation effort but hide the decisions the project must demonstrate.

## Decision

Implement the core runtime against small internal protocols. Provider SDKs, FastAPI, MCP libraries, and storage drivers may be used at their integration boundaries, but no external Agent framework owns loop transitions, budgets, approvals, or checkpoints.

## Consequences

The codebase owns more tests and failure behavior. In exchange, runtime decisions remain inspectable, replaceable, and suitable for controlled experiments. The project will compare against external baselines rather than copy their implementation.

