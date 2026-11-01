# ADR 021: A focused commerce reader with explicit current host authorization

Date: 2026-10-17. Status: accepted.

## Context

The existing commerce application exposes five capabilities including approved
ticket creation. A first read-only integration should expose one product query
and keep host identity, scope, current access and ledger verification explicit.
The source review of [smolagents](https://github.com/huggingface/smolagents/blob/96f33faaf028479119ec8d34507b47694cf14e34/src/smolagents/agents.py)
informed the focused onboarding path; no code was copied. Simplicity has not yet
been measured with an independent developer.

## Decision

Add commerce_readonly_application with a required trusted authorize callback,
one ProductTool, a deny-by-default read policy and the existing ledger verifier.
It is a distinct application/policy identity. Model-generated tool names cannot
enable ticket creation, and this application never approves writes.

Add optional ApplicationSpec.authorize_run, default None for compatibility.
Harness applies it on scoped control access and immediately before preparing or
dispatching model inputs. The new commerce factory supplies its required callback;
its tool policy and final verifier also query the callback. This closes the window
where access revoked after a read could otherwise reach a later model invocation.
Use Context-bound runtime composition rather than global user state.

The example uses only public SDK/application exports and deterministic synthetic
fixtures. Include it in sdist and all minimal-install demonstration gates.

## Limits

Host authentication/resource assignment, concurrent authorization transactions and
real store integration remain host responsibilities. Callback None does not add
automatic IAM to old applications, nor replace SessionService/source governance.
Already delivered content cannot be recalled. Current ledger version is rechecked
at final verification; the example is one consultation, not a multi-turn history
or semantic quality benchmark. The callback's implementation is not fingerprinted;
hosts must version app/policy when they change its semantics, as for existing policy
and model implementations.

See [COMMERCE_READONLY](../COMMERCE_READONLY.md).
