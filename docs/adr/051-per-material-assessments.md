# 051 — Collaboration reports can require one assessment per assigned material

Date: 2026-11-19. Status: accepted.

## Context

In the collaboration ablation (`reports/collaboration-ablation/REPORT.md`) the team
arm's errors on 32 materials were omissions: a specialist wrote a summary first and
then left violating services out of its findings. One added sentence ("write findings
before the summary") removed every omission (11/12, not significant), and the traces
showed why: the model began reasoning inside each claim. The report recommended a
structured form of the same idea — the specialist judges each material in its own
field before concluding — as a framework change to be designed separately and
evaluated on fresh seeds.

Schema 1 (`AgentReport`) cannot express "every assigned material was looked at". A
specialist that skips a material produces a valid report; the coordinator and the
verifier cannot tell an omission from a negative judgement.

## Decision

- `CollaborationPlan.assessment_verdicts` (optional, 2–8 distinct lowercase labels)
  opts a plan into schema 2. The host chooses the labels, for example
  `("violation", "compliant", "insufficient")`; the SDK gives them no meaning.
- Schema 2 is `AssessedAgentReport`: `assessments`, then `findings`, `summary`,
  `unresolved`. `assessments` maps each assigned evidence ID to
  `{verdict, reason}`. The node's JSON schema lists every assigned ID as required and
  rejects other keys, so an omitted, invented or duplicated material is a schema
  failure at the node boundary, before any dependant consumes the report. The same
  rule applies to the coordinator over all assigned materials.
- Field order is part of the decision: the schema and the system message ask for
  assessments before cross-material findings and the summary.
- `CollaborationVerifier` validates the final report with the plan's schema and
  counts assessed materials as cited evidence. It still does not judge whether a
  verdict is semantically right.
- Without `assessment_verdicts` nothing changes: the compiled graph for schema 1 is
  byte-identical to 0.14.0 (`test_schema1_graph_is_unchanged_without_verdicts` pins
  its hash), so in-flight runs resume and sealed experiments reproduce.

## Consequences

- A specialist can no longer drop a material silently; it must commit to a verdict,
  and a wrong verdict is visible per material in `Collaboration.inspect`.
- Reports are longer. A coordinator over 32 materials needs a larger output
  allowance; the 32-material and 8-per-task limits are unchanged.
- This ADR does not claim schema 2 improves accuracy. That is measured in the
  cross-material synthesis study (`reports/cross-material-synthesis/PLAN.md`), which
  runs schema 1 and schema 2 team arms side by side.
- Schema 2 is a new report contract; a run started under one schema cannot be resumed
  under the other, because the compiled graph differs.
