# ADR 003: Hybrid retrieval and isolated vector spaces

Status: accepted.

ForgeHarness runs FTS5 lexical search and embedding-vector search concurrently, fuses ranks with RRF, then asks a cross-encoder to rerank. A reranker outage falls back to fused results. Retrieval reports retain every candidate, rank, score and stage timing.

Knowledge and approved long-term memory use different lexical indexes and vector collections. Qdrant collection names include a hash of the embedding model plus its dimension; a changed model therefore creates a new space instead of silently mixing vectors.

Documents are untrusted evidence. Generation prompts delimit them as evidence, citations are reconstructed from stored chunks, and the Reviewer independently checks chunk/source/location/quote bindings.
