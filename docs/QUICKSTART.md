# Quickstart

Ten lines to a governed run, no API key, no Docker, no config file. This page
describes ForgeHarness 0.14.0 (SDK API v1) on Python 3.12.

## 1. Install

The package is not published to PyPI yet. Install from source with
[uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/l252916991-cyber/forgeharness.git
cd forgeharness
uv sync --extra dev --extra cli --frozen
```

To use it from another project, build a wheel and install that file:

```bash
uv build                                       # writes dist/forgeharness-0.14.0-*.whl
pip install "dist/forgeharness-0.14.0-py3-none-any.whl[cli]"
```

Verify:

```bash
uv run --no-sync forge --help
uv run --no-sync python -c "import forgeharness; print(forgeharness.__version__)"  # 0.14.0
```

## 2. A run in ten lines

```python
import asyncio, pathlib
from forgeharness.sdk import AgentRuntime, FinalAction, ModelResult, ScriptedModel
from forgeharness.sdk import InMemoryTrace, RiskBasedPolicy, ToolDispatcher, ToolRegistry

model = ScriptedModel([ModelResult(action=FinalAction(content="hello"), model_name="scripted")])
runtime = AgentRuntime(
    model=model,
    registry=ToolRegistry(),
    dispatcher=ToolDispatcher(ToolRegistry()),
    policy=RiskBasedPolicy(),
    trace=InMemoryTrace("demo"),
)
result = asyncio.run(runtime.run(task_id="demo", task="say hello", workspace=pathlib.Path(".")))
print(result.status, result.final_output)  # RunStatus.SUCCEEDED hello
```

`ScriptedModel` replays a fixed list of actions, so the snippet proves the runtime
wiring — loop, policy, trace, budget — without a provider. Swap in a real model for
an actual task; nothing else changes.

## 3. Add a tool the model can call

A tool is a class with an `input_model`, a `ToolSpec`, and an `execute` method. The
spec's JSON Schema is what the model sees, and the runtime validates arguments
against the same Pydantic model before `execute` runs.

```python
from pydantic import BaseModel
from forgeharness.sdk import RiskLevel, ToolContext, ToolEffectClass, ToolOutput, ToolSpec


class WordCountInput(BaseModel):
    text: str


class WordCount:
    input_model = WordCountInput
    spec = ToolSpec(
        name="word_count",
        description="Count words in a string.",
        input_schema=WordCountInput.model_json_schema(),
        risk=RiskLevel.READ,
        effect_class=ToolEffectClass.READ_ONLY,
    )

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolOutput:
        return ToolOutput(ok=True, content=str(len(arguments.text.split())))
```

Register it with `registry = ToolRegistry(); registry.register(WordCount())` and include a
`ToolAction(call=ToolCall(id="c1", name="word_count", arguments={"text": "a b c"}))`
in the scripted actions.

## 4. Writes stop for approval

`RiskBasedPolicy` allows reads, suspends writes, and denies everything else. A
suspended run comes back as `RunStatus.AWAITING_APPROVAL` with a
`result.pending_approval` describing the exact call. Approve that exact action and
resume from the stored checkpoint — a write to a different path will not be accepted:

```python
if result.pending_approval is not None:
    call = result.pending_approval.call
    print(call.name, call.arguments, result.pending_approval.reason)
```

For argument-level rules — deny `*.env` and `id_rsa*`, allow `src/**`, cap `run_tests`
at 20 calls per minute, block `rm -rf` — pass a `ParameterAwarePolicy` instead:

```python
from forgeharness.sdk import DEFAULT_SECRET_PATH_RULES, ParameterAwarePolicy, PathRule
from forgeharness.sdk import PolicyDecisionType, RateLimit, RiskBasedPolicy

policy = ParameterAwarePolicy(
    base=RiskBasedPolicy(),
    paths=(
        *DEFAULT_SECRET_PATH_RULES,
        PathRule("src/**", PolicyDecisionType.ALLOW, "generated tree"),
    ),
    rates=(RateLimit(max_calls=20, window_seconds=60, tools=("run_tests",)),),
)
```

## 5. The Coding vertical

`CodingAgent` adds seven tools (`list_files`, `read_file`, `search_code`, `write_file`,
`run_tests`, `git_diff`, `read_observation`), a repository map, a fixed test command,
and a verifier. Two moments matter:

```python
import sys
from pathlib import Path
from forgeharness.sdk import InMemoryTrace
from forgeharness.coding import CodingAgent

agent = CodingAgent(
    model=model, trace=InMemoryTrace("fix-1"), test_command=(sys.executable, "-m", "pytest", "-q")
)
first = await agent.start(task_id="fix-1", issue="add a module", workspace=Path("repo"))

# Writes suspend. The grant is bound to this exact call, expires in 10 minutes,
# and is consumed on use — it cannot be replayed against a different path.
second = await agent.resume(
    suspended=first, grant=agent.approve(first, granted_by="you"), workspace=Path("repo")
)
```

Overwriting an existing file additionally requires its current SHA-256, which
`read_file` returns — that is what stops a stale read from clobbering someone else's
edit. The verifier accepts a run only when a `write_file` succeeded *and* a later
`run_tests` passed. A final answer with no test evidence comes back `RunStatus.FAILED` with the
reason "no test evidence", not a green checkmark.

## 6. Long tool output does not vanish

Observations over the compressor's ceiling are projected — matched lines plus
head and tail — and the original is written to
`<workspace>/.forgeharness/observations/`. The projected text carries a reference:

```
[compressed observation: 41235 chars, 812 lines, full: obs://3f9a…]
```

The model can page the original back with `read_observation(reference="obs://3f9a…")`
instead of re-running the tool that produced it. When a workspace must stay clean,
construct `AgentRuntime(observation_spill=False)`; the projection still happens, the
file is simply not written.

## 7. Which extra do I need?

| Extra | Unlocks |
| --- | --- |
| *(none)* | Runtime, policy, tracing, SQLite checkpoints, coding vertical, spill |
| `cli` | The full `forge` CLI (recommended; the one to install) |
| `web` | FastAPI host for the reference applications |
| `models` | `httpx`-backed model adapters |
| `knowledge` | RAG pipeline (FTS5 + vector + rerank) |
| `platform` | PostgreSQL + Redis + Qdrant hosted execution |
| `frameworks` | LangChain / LangGraph adapters |
| `all` | Everything above except benchmark tooling |

## 8. Where state lands

- Trace: `InMemoryTrace` by default; swap for the JSONL writer for a hash-chained file.
- Checkpoints: `SQLiteCheckpointStore(path)` — one row per revision, resumable.
- Spilled observations: `<workspace>/.forgeharness/observations/`, content-addressed.
- Nothing is written outside the workspace you pass to `run()`.
