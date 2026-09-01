# References and attribution

ForgeHarness implements its runtime directly. The following projects and specifications informed terminology, product comparison, or protocol behavior; their source code is not vendored or copied into this repository.

- [Model Context Protocol — current specification](https://modelcontextprotocol.io/specification/2026-07-28)
- [MCP 2026-07-28 release announcement](https://blog.modelcontextprotocol.io/posts/2026-07-28/)
- [SWE-agent](https://github.com/SWE-agent/SWE-agent) — research/product baseline for software-engineering agents.
- [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) — minimal coding-agent baseline and comparison point.
- [SWE-bench](https://github.com/SWE-bench/SWE-bench) — planned external evaluation dataset; no result is currently claimed.
- [OpenAI chat-completions API format](https://platform.openai.com/docs/api-reference/chat) — compatibility shape used by the provider adapter.

Python dependencies and exact resolved versions are recorded in `uv.lock`. Each dependency retains its own license.
