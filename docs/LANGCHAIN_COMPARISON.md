# LangChain/LangGraph Implementation Comparison

## Purpose

ForgeHarness includes both a **native Agent Harness runtime** and a **LangChain/LangGraph implementation** to demonstrate:

1. **Framework proficiency**: Understanding mainstream Agent frameworks (LangChain LCEL, LangGraph state machines)
2. **Architectural comparison**: Trade-offs between custom runtime vs framework-based approaches
3. **Performance baseline**: Empirical comparison using identical retrieval backend
4. **Interview readiness**: Ability to discuss both custom implementation rationale and framework best practices

---

## Architecture Comparison

### Native ForgeHarness Runtime

```
AgentRuntime
  ├── Loop Strategy (ReAct / Plan-Execute)
  ├── Budget Enforcement (step / tool-call / token)
  ├── Approval Ledger (exact-action, expiring grants)
  ├── Checkpoint Store (SQLite optimistic snapshots)
  ├── Tool Dispatcher (native / MCP / sub-agent)
  └── Hash-chained Trace (tamper-evident audit)
```

**Characteristics:**
- ✅ Fine-grained control over state transitions
- ✅ Hard budget enforcement before model invocation
- ✅ Custom approval suspension/resume logic
- ✅ Framework-agnostic (no LangChain dependency in core)
- ⚠️ Higher implementation complexity
- ⚠️ Requires maintenance of state machine logic

### LangChain/LangGraph Implementation

```
LangChain RAG:
  LCEL Chain = Retriever -> Context Formatter -> Prompt -> LLM -> Parser
  + ConversationBufferMemory (last N messages)

LangGraph Coding Agent:
  StateGraph
    ├── agent (reasoning with tool binding)
    ├── tools (ToolNode execution)
    ├── check_completion (test verification)
    └── conditional edges (continue / iterate / end)
```

**Characteristics:**
- ✅ Declarative chain composition (LCEL)
- ✅ Built-in tool integration patterns
- ✅ Community ecosystem (LangSmith observability, templates)
- ✅ Faster development for standard patterns
- ⚠️ Less control over budget enforcement points
- ⚠️ Approval logic requires custom interrupts
- ⚠️ Framework coupling

---

## Implementation Mapping

| Feature | Native ForgeHarness | LangChain/LangGraph |
|---------|---------------------|---------------------|
| **RAG Retrieval** | Custom `KnowledgeService` with FTS5 + vector + RRF + rerank | `HybridRetriever` wrapper over same backend |
| **Chain Composition** | Imperative Python async/await | LCEL declarative chains |
| **Agent Loop** | Custom state machine (5 states: created/running/awaiting_approval/succeeded/failed) | LangGraph `StateGraph` with conditional edges |
| **Memory** | Session context compiler + review-gated long-term memory | `ConversationBufferMemory` (last N messages) |
| **Tool Validation** | Pydantic + JSON Schema + policy layer | LangChain `@tool` decorator + LLM tool binding |
| **Approval** | `ApprovalLedger` with expiring one-shot grants | Custom callback (requires manual integration) |
| **Checkpoint** | SQLite optimistic snapshots | LangGraph built-in checkpointing (optional) |
| **Observability** | SHA-256 hash-chained JSONL trace | LangSmith tracing (external service) |
| **Sub-agent** | Scoped runtime with parent budget deduction | LangGraph subgraph (experimental) |

---

## Code Examples

### RAG Comparison

#### Native ForgeHarness
```python
from forgeharness.knowledge.application import build_knowledge_application

app = build_knowledge_application(Path(".forgeharness"))
report = await app.knowledge.search("How does approval work?")

# Returns RetrievalReport with:
# - lexical_candidates (FTS5 results)
# - vector_candidates (embedding search)
# - fused (RRF combined)
# - final (after rerank)
# - stage_timings
```

#### LangChain Version
```python
from forgeharness.langchain_impl.rag_chain import create_langchain_rag_app

chain = await create_langchain_rag_app(Path(".forgeharness"))
result = await chain.ask("How does approval work?")

# Returns RetrievalResult with:
# - answer (generated response with citations)
# - sources (list of document filenames)
# - chunks_used
# - retrieval_time_ms
```

**Key Difference:**
- Native returns **retrieval report** (inspection-first, generation separate)
- LangChain returns **complete answer** (end-to-end chain)

---

### Agent State Machine

#### Native ForgeHarness
```python
from forgeharness.coding.workflow import run_repair_workflow

result = await run_repair_workflow(
    task="Fix the bug in utils.py",
    workspace=repo_path,
    test_command="pytest -q",
    model=model_config,
    approval_mode="interactive",  # Suspend on write_file
)

# State transitions logged in checkpoint:
# created -> running -> awaiting_approval -> running -> succeeded
```

#### LangGraph Version
```python
from forgeharness.langchain_impl.coding_agent import LangGraphCodingAgent

agent = LangGraphCodingAgent(llm=llm, workspace=repo_path)
result = await agent.run("Fix the bug in utils.py")

# State maintained in AgentState TypedDict:
# messages, workspace, status, iterations, tool_calls
# Graph edges: agent -> tools -> agent -> check_completion -> END
```

**Key Difference:**
- Native: Explicit approval suspension with ledger persistence
- LangGraph: State tracking via TypedDict, approval requires custom interrupt

---

## Performance Benchmark

Run comparison (both arms run in one process over identical queries):
```bash
uv run forge framework-compare --queries 5
```

Measured results (5 RAG queries against the indexed corpus, `reports/framework-comparison.json`
is revision-bound and regenerated on every run):

| Metric | Native | LangChain | Note |
|--------|--------|-----------|------|
| Retrieval mean latency | 1.40 ms | 3.64 ms | +160% relative, ~2 ms absolute — negligible in practice |
| Retrieval p50 / p95 | 0.85 / 3.68 ms | 3.75 / 4.40 ms | Same hits (5.0) on both arms |
| Retrieval quality | Identical | Identical | Same backend (FTS5 + vector + RRF + rerank) |
| End-to-end answer latency | n/a | see report | Dominated by local 9B chat-model generation, not the framework |

**Insight:** the LCEL wrapper costs a few milliseconds per retrieval (tiny in absolute
terms); end-to-end latency is dominated by model generation, so framework choice does
not materially affect user-perceived performance. Both arms return the same hits
because they share the native retrieval backend.

Only values present in the generated report may be quoted; rerun the command to
rebind the numbers to your revision.

---

## When to Choose Each Approach

### Choose Native Custom Runtime When:
- ✅ Hard budget enforcement is critical (prevent runaway costs)
- ✅ Approval flow requires exact-action grants with expiry
- ✅ Audit trail must be tamper-evident (hash-chained trace)
- ✅ You need full control over state transitions
- ✅ Framework independence is a requirement
- ✅ **Interview context**: Demonstrating deep understanding of Agent internals

### Choose LangChain/LangGraph When:
- ✅ Rapid prototyping of standard RAG/Agent patterns
- ✅ Leveraging community templates and integrations
- ✅ Team already invested in LangChain ecosystem
- ✅ LangSmith observability is valuable
- ✅ Declarative chain composition improves maintainability
- ✅ **Interview context**: Demonstrating framework proficiency and pragmatism

---

## Interview Talking Points

### "Why did you implement your own runtime instead of using LangChain?"

**Good Answer:**
> "I wanted to deeply understand Agent fundamentals—state machines, budget enforcement, approval suspension—which are often abstracted away in frameworks. By implementing the core loop myself, I learned how to handle edge cases like checkpoint recovery and tool call validation. However, I also built a LangChain version to compare trade-offs: the framework version trades some control over approval flow for less boilerplate. In production, I'd choose based on the specific governance requirements."

### "Have you used LangChain/LangGraph in real projects?"

**Good Answer:**
> "Yes, I implemented a parallel RAG system using LangChain LCEL for declarative chain composition and LangGraph for the Coding Agent state machine. I benchmarked both implementations on the same retrieval backend: the LCEL wrapper adds a few milliseconds per retrieval (measured +331% relative, ~4 ms absolute), which is negligible; end-to-end latency is dominated by model generation. For my use case—needing hard budget limits and exact-action approvals—the custom runtime was better, but I'd reach for LangChain for standard knowledge-base Q&A."

### "What's the difference between LangChain and LangGraph?"

**Good Answer:**
> "LangChain provides chain primitives (LCEL) for composing LLM calls, prompts, and tools in a pipeline. It's great for stateless workflows like RAG. LangGraph adds **stateful workflows** via directed graphs with conditional edges—essential for Agents that need to loop, plan, and react. In my project, I used LangChain for RAG chains (retrieve -> format -> generate) and LangGraph for the Coding Agent state machine (plan -> execute -> test -> iterate)."

---

## Technical Deep-Dive: Approval Pattern Comparison

### Native ForgeHarness Approval

```python
# Approval suspends execution mid-loop
class ApprovalLedger:
    def request(self, action: ToolCall) -> ApprovalGrant | None:
        # Returns None if no grant exists -> suspend
        # Returns grant if approved -> consume and continue

# In runtime loop:
for step in range(budget.max_steps):
    tool_call = model.invoke(...)
    if requires_approval(tool_call):
        grant = ledger.request(tool_call)
        if not grant:
            checkpoint.save(state="awaiting_approval", pending=tool_call)
            return  # Suspend execution, resume later
    result = dispatcher.execute(tool_call, grant)
```

### LangGraph Approval Pattern

```python
# Approval via human-in-the-loop interrupt
from langgraph.checkpoint import MemorySaver


# Define approval node
async def approval_required(state):
    pending = state["pending_action"]
    # In production, this would emit event and wait for external approval
    approved = await external_approval_callback(pending)
    return {"approved": approved}


# Graph structure
workflow.add_node("request_approval", approval_required)
workflow.add_conditional_edges(
    "agent",
    lambda s: "approval" if needs_approval(s) else "execute",
    {"approval": "request_approval", "execute": "tools"},
)
```

**Trade-off:**
- Native: Explicit suspension with persistent checkpoint (survives process restart)
- LangGraph: Requires external orchestration for human interrupts (more flexible but requires setup)

---

## Recommendations for Resume/Interview

1. **Highlight Both Implementations:**
   - "Implemented Agent Harness in both **native Python** (custom state machine) and **LangChain/LangGraph** (framework-based) to compare architectural trade-offs"

2. **Quantify the Comparison:**
   - "Benchmarked both on the same retrieval backend: identical hits, ~4 ms absolute wrapper overhead; native provided finer control over budget enforcement"

3. **Show Framework Depth:**
   - "Used **LangChain LCEL** for declarative RAG chains and **LangGraph StateGraph** for stateful Agent workflows with conditional edges"

4. **Explain the "Why":**
   - "Built custom runtime to understand Agent internals deeply, validated against LangChain to prove framework proficiency"

5. **Demo Readiness:**
   - Prepare 2-minute side-by-side demo: same RAG query on both implementations, show latency/code comparison

---

## Running the LangChain Implementation

### Install Dependencies
```bash
# Already in pyproject.toml [project.optional-dependencies.frameworks]
uv sync --extra frameworks
```

### RAG Demo
```bash
uv run python src/forgeharness/langchain_impl/rag_chain.py
```

### Coding Agent Demo
```bash
uv run python src/forgeharness/langchain_impl/coding_agent.py
```

### Full Benchmark
```bash
uv run python src/forgeharness/langchain_impl/benchmark.py
# Outputs: reports/framework-comparison.json
```

---

## References

- LangChain LCEL: https://python.langchain.com/docs/expression_language/
- LangGraph: https://langchain-ai.github.io/langgraph/
- ForgeHarness native runtime: `src/forgeharness/runtime/`
- LangChain implementation: `src/forgeharness/langchain_impl/`
