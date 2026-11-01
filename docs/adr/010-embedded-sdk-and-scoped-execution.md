# ADR 010 — Embedded SDK and scoped execution (accepted)

The first framework release keeps one wheel and the existing module locations.
The supported application boundary is `forgeharness.sdk`; core modules cannot
import business applications. Historical CodingVerifier imports are lazy v1
compatibility shims; ordinary core initialization never loads the coding app.
Model capabilities belong to the provider-neutral model layer.

The host authenticates and constructs immutable RunContext/Actor/ResourceScope.
These are trusted Python values, not model arguments. Business adapters are trusted
code and must enforce resource ownership again at their own storage boundary.
No arbitrary configuration class loading or plugin sandbox is offered.

SDK runs are namespaced by application and tenant, with an exact persisted actor,
session, scope, application/policy version binding. Scope changes require a new
run; current policy is evaluated on approval and replay. Workspace is optional;
file tools explicitly require it. Legacy runtime APIs remain local trusted APIs.

Verification evidence is ordered and persisted with checkpoint control state.
Coding acceptance requires passing tests after the last successful write.
State contract v1 and recovery generation 3 distinguish this from historical
retry/deadline generation 2. Older unresolved calls fail closed.

SDK instances assemble a fresh runtime per operation; ContextVar holds per-task
transient binding/evidence. Installed tools remain trusted executable code.
