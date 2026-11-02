# 047 — Field-level schema feedback for invalid tool arguments

Date: 2026-11-02. Status: accepted.

## Context

Until now a schema failure reached the model as one fixed sentence, "The arguments
do not match the tool's required schema", with no indication of which field failed.
The rule existed to keep validator prose, which can quote the submitted value, out
of the model's context.

The first DeepSeek (`deepseek-flash`) baseline showed the cost. In
`long-todo-gate-cleared` the model sent `write_todos` with a 331-character
`rationale` against a 300-character limit, received the fixed sentence, and resent
the identical call twenty times until the step budget was exhausted.

## Decision

For the local dispatcher, an invalid-arguments failure now carries
`details: {argument location: violated constraint}`:

- the location is the path inside the model's own arguments, such as
  `todos[1].status` or `rationale`;
- the constraint comes from the declared schema (a length, pattern, enum, type,
  missing or unexpected field);
- the submitted value is never echoed. Pydantic messages are used only for error
  types whose text is built from the constraint; other types, including custom
  `value_error` text, become a generic "does not satisfy the input schema". External
  JSON Schema failures are described from the keyword and schema, never from
  `jsonschema`'s message, which quotes the instance;
- at most five violations are reported, each bounded in length.

`OBSERVATION_CONTRACT_VERSION` becomes 2, so Agent Benchmark reports made before
and after the change cannot be read as one improving line.

## Consequences

The model can correct a malformed call in one step instead of guessing. The hosted
PostgreSQL path still reports the fixed sentence, because its deterministic refusal
is reconstructed byte-for-byte during recovery; extending field-level details to it
needs the same reconstruction and is left for a later change.
