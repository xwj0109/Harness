# Context Management Plan

## Summary

Evolve Harness context management from static prompt packing into a provenance-preserving context pipeline. The goal is better relevance, lower token waste, repeatable packing, and safer auditability without weakening Harness's local-first authority model.

Current chat context is reliable but coarse:

- `src/harness/chat.py` calls `pack_chat_context(project_root)` before the model turn.
- `src/harness/context_pack.py` builds a static manifest with Harness vocabulary, built-in registry data, safety summaries, repo tree, selected files, git state, recent artifact metadata, and operator context.
- `_model_messages()` serializes selected blocks into one system message.
- The read-only chat tool loop can fetch more information with `repo_tree`, `read_file`, `search_repo`, `show_diff`, and JSON context tools.
- Token estimates are character heuristics, not model-token counts.
- There is no persistent context chunk cache, query-aware retrieval layer, semantic index, or context compression pipeline.

Target architecture:

```text
user turn
  -> context request
  -> pinned safety and state blocks
  -> query-aware retrieval over repo/docs/state
  -> provenance and trust filtering
  -> tokenizer-aware budget packing
  -> optional compression
  -> backend-specific serialization
  -> model turn plus existing read-only tool loop
```

The plan keeps the current action-contract, approval, provenance, and read-only tool boundaries intact. Context selection remains passive; it must not execute providers, run Docker, mutate state, grant permissions, or dispatch adapters.

## Product Principles

- Preserve the distinction between passive context and explicit action.
- Keep safety, approvals, pending actions, leases, current objective/task/run state, and memory warnings pinned and easy to inspect.
- Every retrieved or compressed block must carry source, trust level, hash or stable source id, and warning metadata.
- Retrieval and compression may reduce text, but they must not erase provenance or authority boundaries.
- Hosted embedding, hosted reranking, remote vector search, or hosted compression require an explicit context transmission policy and approval path.
- Read-only context display must stay cheap and deterministic. It must not preflight model backends or run providers.
- Existing JSON contracts should be extended with versioned fields rather than broken.

## Non-Goals

- Do not replace Harness action contracts.
- Do not make chat or TUI context an execution authority.
- Do not send repository, memory, artifact, or generated context to hosted services by default.
- Do not index secret-like paths, `.harness` private runtime state, `.git`, dependencies, build outputs, or context-excluded paths.
- Do not abstractive-summarize approvals, policy boundaries, diffs, evidence ids, or secret/redaction decisions.
- Do not require Postgres, Qdrant, Weaviate, Milvus, or another external service for the first release.

## Current Seams

Use these existing seams first:

- [src/harness/context_pack.py](/Users/oscarxue/Documents/harness/src/harness/context_pack.py): current `ContextBlock`, `ContextManifest`, static block selection, path filtering, secret filtering, and budget fitting.
- [src/harness/chat.py](/Users/oscarxue/Documents/harness/src/harness/chat.py): `_model_chat_response()`, `_model_messages()`, progress lines, read-only tool loop, and context manifest response metadata.
- [src/harness/chat_model.py](/Users/oscarxue/Documents/harness/src/harness/chat_model.py): `ChatContext` and backend-specific serialization behavior.
- [src/harness/chat_tools.py](/Users/oscarxue/Documents/harness/src/harness/chat_tools.py): read-only context tools and current fallback inspection path.
- [src/harness/memory/sqlite_store.py](/Users/oscarxue/Documents/harness/src/harness/memory/sqlite_store.py): SQLite persistence, memory records, artifact metadata, run manifests, and `build_context_provenance()`.
- [src/harness/models.py](/Users/oscarxue/Documents/harness/src/harness/models.py): `ContextProvenanceRecord`, `ContextTrustLevel`, `ContextSourceKind`, memory models, and manifest models.
- [src/harness/local_server.py](/Users/oscarxue/Documents/harness/src/harness/local_server.py): existing session context estimate route.
- [docs/operator_guide.md](/Users/oscarxue/Documents/harness/docs/operator_guide.md) and [docs/command_catalog.md](/Users/oscarxue/Documents/harness/docs/command_catalog.md): operator-facing contracts.

## Target Architecture

Add a small context pipeline behind `pack_chat_context()` and the chat model path.

```text
ContextRequest
  - project_root
  - query
  - mode
  - active model profile
  - optional session id
  - optional objective/task/run ids
  - budget policy

ContextPipeline
  - pinned provider
  - retriever
  - provenance filter
  - token budgeter
  - optional compressor
  - serializer

ContextManifest
  - pinned blocks
  - retrieved blocks
  - derived/compressed blocks
  - selected chunk refs
  - blocked paths
  - warnings
  - budget report
```

Recommended module layout:

```text
src/harness/context_budget.py
src/harness/context_chunks.py
src/harness/context_cache.py
src/harness/context_retrieval.py
src/harness/context_pipeline.py
src/harness/context_compression.py
```

Keep `src/harness/context_pack.py` as the compatibility facade. Existing callers should be able to keep using `pack_chat_context(project_root)`, while newer callers can pass a query and budget policy.

## Public Types

Add Pydantic or dataclass models with schema versions before wiring behavior broadly.

```python
class ContextBlockRole(str, Enum):
    PINNED = "pinned"
    RETRIEVED = "retrieved"
    DERIVED = "derived"


class ContextRequest(BaseModel):
    schema_version: str = "harness.context_request/v1"
    project_root: Path
    query: str = ""
    mode: str = "normal"
    model_profile: str | None = None
    session_id: str | None = None
    objective_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    max_input_tokens: int | None = None
    include_dynamic_retrieval: bool = True


class ContextChunk(BaseModel):
    schema_version: str = "harness.context_chunk/v1"
    chunk_id: str
    source_kind: ContextSourceKind
    trust_level: ContextTrustLevel
    path: Path | None = None
    source_id: str | None = None
    artifact_id: str | None = None
    memory_id: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    sha256: str
    text: str
    token_count: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class RetrievedContextChunk(BaseModel):
    schema_version: str = "harness.retrieved_context_chunk/v1"
    chunk: ContextChunk
    score: float
    rank: int
    retriever: str
    matched_terms: list[str] = Field(default_factory=list)


class ContextBudgetReport(BaseModel):
    schema_version: str = "harness.context_budget_report/v1"
    tokenizer: str
    model_profile: str | None = None
    max_input_tokens: int | None = None
    reserved_output_tokens: int
    used_input_tokens: int
    approximate: bool = False
    warnings: list[str] = Field(default_factory=list)
```

Extend `ContextBlock` rather than replacing it:

```python
class ContextBlock(BaseModel):
    kind: str
    title: str
    content: str
    source: str | None = None
    token_estimate: int = 0
    truncated: bool = False
    role: ContextBlockRole = ContextBlockRole.PINNED
    chunk_ids: list[str] = Field(default_factory=list)
    provenance: list[ContextProvenanceRecord] = Field(default_factory=list)
    score: float | None = None
```

Compatibility rule: existing `to_payload()` output must keep current fields. Add new fields only where consumers tolerate them or behind a new manifest schema version.

## Phase 1: Tokenizer-Aware Budgeting

Goal: replace coarse character budgeting with a model-profile-aware budgeter while keeping the current static context behavior.

Implementation steps:

- [x] Add `src/harness/context_budget.py`.
- [x] Define `TokenBudgeter` with `count(text: str) -> int` and `fit(text: str, max_tokens: int) -> TokenFit`.
- [x] Add a default heuristic budgeter matching the current `len(text) // 4` behavior.
- [x] Add an optional `tiktoken` budgeter for Codex/OpenAI-like profiles when the dependency is available.
- [x] Select budgeter from `load_config(project_root).chat.default_model_profile`.
- [x] Update `_block()` and `_fit_block()` in `context_pack.py` to use token counts internally.
- [x] Keep `token_estimate` in payloads for compatibility, but make it tokenizer-backed when available.
- [x] Add manifest warning `approximate_token_budget_only` when falling back to the heuristic.
- [x] Add a `budget_report` field to the manifest payload.

Tests:

- [x] `tests/test_context_budget.py` covers heuristic counting, fit behavior, tokenizer fallback, and warning behavior.
- [x] Existing `tests/test_context_pack.py` still passes.
- [x] Add a budget exhaustion test that asserts the first oversized block is truncated by token budget, not character budget.

Acceptance criteria:

- [x] `pack_chat_context()` produces the same block kinds as before for default callers.
- [x] Manifests include budget metadata without breaking existing tests.
- [x] No backend preflight occurs during context budgeting.

## Phase 2: Pinned vs Dynamic Context Split

Goal: make the context manifest distinguish always-included safety/state from query-selected content.

Pinned blocks:

- Harness vocabulary and authority boundaries.
- Security and policy summary.
- Sandbox profiles summary.
- Current pending action, current mode, selected model profile, and safety boundaries.
- Current objective/task/run/session ids where known.
- Active leases and blocked-state summaries.
- Memory summary and `memory_not_authority` warnings.

Dynamic candidates:

- Repo tree excerpts.
- README, AGENTS, docs, and source chunks.
- Git status and diff metadata.
- Recent artifact metadata.
- Recent session messages and transcript summaries.
- Memory records and derived memory summaries.

Implementation steps:

- [x] Add `role` metadata to blocks: `pinned`, `retrieved`, or `derived`.
- [x] Add `pack_pinned_context(project_root, request)` for tiny, safety-critical context.
- [x] Add `pack_static_dynamic_context(project_root, request)` as a temporary compatibility bridge using current file/tree/git blocks.
- [x] Update `_context_manifest_progress_lines()` to show pinned vs retrieved counts.
- [x] Update `_model_chat_response()` to create a `ContextRequest` with the raw user turn and active mode.
- [x] Keep the existing read-only tool loop unchanged.

Tests:

- [x] Context pack tests assert safety blocks are pinned.
- [x] Chat response metadata includes block roles.
- [x] Memory records are still non-authoritative and carry warnings.

Acceptance criteria:

- [x] Static behavior remains unchanged in user-visible chat quality.
- [x] The manifest can be inspected to see why a block was pinned or retrieved.
- [x] Pinned blocks never lose safety/approval/memory warnings due to retrieval ranking.

Status note:

- Implemented the Phase 2 static bridge by adding additive block roles, `ContextRequest`, pinned and static dynamic packers, manifest role summaries, chat metadata/progress role counts, and focused tests.
- Later phases remain intentionally unimplemented: no chunk cache, lexical retrieval, embeddings, vector store, compression, provider preflight, Docker execution, adapter dispatch, or permission-granting behavior was added.

## Phase 3: SQLite Chunk Cache

Goal: avoid repeated preprocessing and create stable retrieval units with hashes and provenance.

Add SQLite tables through the existing store migration path:

```sql
CREATE TABLE IF NOT EXISTS context_chunks (
  id TEXT PRIMARY KEY,
  source_kind TEXT NOT NULL,
  trust_level TEXT NOT NULL,
  path TEXT,
  source_id TEXT,
  artifact_id TEXT,
  memory_id TEXT,
  start_line INTEGER,
  end_line INTEGER,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  token_count INTEGER,
  tokenizer TEXT,
  chunk_scheme TEXT NOT NULL,
  text_preview TEXT NOT NULL,
  redaction_state TEXT,
  warnings_json TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_context_chunks_source ON context_chunks(source_kind, path);
CREATE INDEX IF NOT EXISTS idx_context_chunks_hash ON context_chunks(sha256, chunk_scheme, tokenizer);
```

Rules:

- Chunk ids must be content-addressable or include a content hash.
- Repo-file chunks must preserve path, line range, hash, and trust class.
- Memory chunks must preserve memory id, redaction state, and `memory_not_authority`.
- Artifact chunks are metadata-only by default. Artifact bodies stay out of context unless explicitly enabled by policy.
- Forgetting memory must invalidate or tombstone derived context chunks.
- Secret-like paths and context-excluded paths must not be chunked.

Implementation steps:

- [x] Add chunk models and helpers in `context_chunks.py`.
- [x] Add store methods: `upsert_context_chunk`, `list_context_chunks`, `delete_context_chunks_for_memory`, `stale_context_chunks`.
- [x] Add deterministic line-aware chunking for text files.
- [x] Add hash-based invalidation for repo files.
- [x] Evaluate the optional in-process LRU for token counts and repo tree previews; defer because deterministic rebuild and budgeter costs are already small in this slice.
- [x] Add a CLI/admin inspection command only if useful, for example `harness context chunks --json`.

Tests:

- [x] Chunking skips `.env`, `.git`, `.harness`, dependencies, build outputs, and configured context excludes.
- [x] Unchanged file hashes do not create duplicate chunks.
- [x] Forgetting memory invalidates its chunks.
- [x] Artifact body text does not enter chunk cache by default.

Acceptance criteria:

- [x] Running chunk rebuild twice on unchanged files reuses cached chunk metadata.
- [x] Chunk records can be rebuilt from local files and SQLite state.
- [x] No external service is required.

Status note:

- Implemented the Phase 3 chunk-cache substrate with local `ContextChunk` helpers, SQLite schema/migration support, store upsert/list/delete/stale methods, deterministic line-aware repo-file chunks, memory chunks with `memory_not_authority`, memory-forget invalidation, and artifact metadata-only chunks.
- Deferred the optional in-process LRU and CLI/admin inspection command because they are not needed for the storage substrate and would broaden the surface area.
- Later phases remain intentionally unimplemented: no lexical retrieval, semantic retrieval, embeddings, vector store, compression, transcript summaries, hosted indexing, provider preflight, Docker execution, adapter dispatch, or permission-granting behavior was added.

## Phase 4: Query-Aware Lexical Retrieval

Goal: pick relevant chunks before serialization without introducing embeddings or external infrastructure.

Retrieval sources:

- Cached repo-file chunks.
- README, AGENTS, docs, and command docs.
- Current git status and diff summaries.
- Operator context summaries.
- Recent session and run metadata.
- Explicit memory summaries.

Ranking signals:

- Exact phrase match.
- Symbol/path/token match.
- Filename and extension match.
- Recent active task/objective/run/session boost.
- Safety-critical block pinning, not ranking.
- Penalty for generated or memory context unless directly relevant.

Implementation steps:

- [x] Add `src/harness/context_retrieval.py`.
- [x] Define `ContextRetriever.retrieve(request, k) -> list[RetrievedContextChunk]`.
- [x] Implement `LexicalContextRetriever` using cached chunks.
- [x] Evaluate `rg` fallback; defer because cached SQLite chunks plus existing explicit read-only `search_repo` tool preserve the passive context boundary without adding another implicit search path.
- [x] Add score normalization and de-duplication by `(source_kind, path, start_line, end_line, sha256)`.
- [x] Add retrieved chunk provenance records to the manifest.
- [x] Update `pack_chat_context(project_root, query=...)` to include top lexical chunks under a dynamic budget.
- [x] Keep explicit `search_repo` and `read_file` chat tools as follow-up inspection tools.

Tests:

- [x] Query mentioning a filename ranks that file above unrelated docs.
- [x] Query mentioning a symbol ranks chunks containing the symbol.
- [x] Retrieval respects excludes and secret path filters.
- [x] Retrieved memory chunks include `memory_not_authority`.
- [x] Chat still falls back to read-only tool requests when retrieved context is insufficient.

Acceptance criteria:

- [x] Chat context includes fewer low-value static blocks for targeted questions.
- [x] Every retrieved block has source and provenance metadata.
- [x] Retrieval remains local-only and deterministic.

Status note:

- Implemented the Phase 4 local lexical retriever over the SQLite `context_chunks` cache, with deterministic phrase/token/path/filename/symbol scoring, normalized scores, source-line/hash deduplication, memory/generation penalties, retrieval-time path and secret guards, and additive selected-chunk metadata in `pack_chat_context(project_root, query=...)`.
- The default `pack_chat_context(project_root)` static bridge remains available when no query is supplied or no cached chunks match. `rg` fallback is intentionally deferred to avoid broadening the passive context surface before its path and secret filtering can be audited.

## Phase 5: Provenance-Complete Manifests

Goal: make context selection auditable across chat, runs, sessions, and reports.

Extend manifests with:

```json
{
  "selected_chunks": [
    {
      "chunk_id": "ctx_...",
      "source_kind": "repo_file",
      "trust_level": "untrusted_repo",
      "path": "src/harness/chat.py",
      "start_line": 989,
      "end_line": 1088,
      "sha256": "...",
      "retriever": "lexical",
      "score": 0.83,
      "compressed": false
    }
  ],
  "budget_report": {
    "schema_version": "harness.context_budget_report/v1"
  },
  "untrusted_context_warnings": [
    "memory_not_authority"
  ]
}
```

Implementation steps:

- [x] Extend `ContextManifest` with `selected_chunks`, `context_provenance`, `untrusted_context_warnings`, and `budget_report`.
- [x] Reuse `ContextProvenanceRecord` where possible.
- [x] Add repo-file provenance records, not just run/task/artifact/memory records.
- [x] Record retrieval score, retriever name, chunk scheme, tokenizer, and compression lineage (`compressed=false` until Phase 6).
- [x] Update chat response metadata where context manifests are surfaced.
- [x] Evaluate trace span attributes for context chunk ids only, not raw content; defer direct trace wiring because chat context packing does not currently create run/session trace spans.

Tests:

- [x] Chat manifests include context provenance for selected chunks.
- [x] Confirm trace-style chat metadata includes provenance ids without raw chunk text; defer run trace export integration until chat turns have a narrow trace hook.
- [x] Memory, generated, artifact, and repo-file chunks receive distinct trust levels.
- [x] Untrusted warnings survive serialization.

Acceptance criteria:

- [x] An operator can inspect which source records were used for a model turn.
- [x] Compressed or summarized blocks preserve links to original chunk ids.
- [x] Context provenance cannot grant permissions, approvals, or policy authority.

Status note:

- Implemented Phase 5 for chat context manifests with additive `context_provenance`, `untrusted_context_warnings`, richer `selected_chunks`, `compressed=false`, and audit-only `ContextProvenanceRecord` entries for retrieved chunks and static context blocks. Chat response metadata now surfaces provenance and selected chunk references without adding raw chunk text to trace-style metadata.
- Trace span wiring and compression lineage beyond `compressed=false` are deferred to later phases because the current Phase 5 slice does not create run/session trace records for chat turns.

## Phase 6: Optional Compression and Transcript Summaries

Goal: reduce token spend after retrieval quality is acceptable.

Compression rules:

- Compress only retrieved code/docs/session text.
- Do not compress pinned safety, approvals, policy, diffs, evidence ids, redaction state, or warning text.
- Prefer deterministic extractive trimming first: top spans, line-window clipping, duplicate removal, boilerplate removal.
- Add semantic compression only behind local-first configuration.
- Abstractive rolling summaries must be marked `derived` and non-authoritative.

Implementation steps:

- [x] Add `src/harness/context_compression.py`.
- [x] Define `ContextCompressor` and deterministic extractive compression helpers.
- [x] Implement deterministic extractive compression first.
- [x] Evaluate rolling transcript summaries for stale turns beyond the last N transcript items; defer until a narrow stale-transcript packing seam exists.
- [x] Evaluate summary provenance with source message ids or event ids; defer with transcript summaries.
- [x] Gate optional semantic compression as unsupported/fail-closed by context policy rather than adding a live config path.
- [x] Add manifest lineage from compressed block to original chunk ids.

Tests:

- [x] Pinned blocks are never compressed.
- [x] Diffs and policy text are never compressed.
- [x] Compressed blocks include original chunk ids.
- [x] Confirm derived transcript-summary design cannot authorize permissions or approvals; no live summary path is added in this release.

Acceptance criteria:

- [x] Large retrieved contexts fit budget with stable provenance.
- [x] Compression can be disabled without breaking retrieval.
- [x] No hidden model/provider call is made for compression unless explicitly approved and configured.

Status note:

- Implemented Phase 6 deterministic extractive compression as an explicit opt-in context-packing path (`enable_compression=True`) with line-window clipping, duplicate/boilerplate removal, existing token-budget fitting, and additive compression lineage. Compression is limited to retrieved repo-file chunks; pinned safety/state, policy, approvals, diffs, artifacts, memory warnings, redaction decisions, and warning text remain uncompressed.
- Transcript-summary scaffolding is deferred. The repo has in-memory chat transcript state and separate persisted session/message records, but there is not yet a narrow, safe packing seam for stale transcript turns in this context path without broadening storage/session behavior.

## Phase 7: Dense and Hybrid Retrieval

Goal: add embeddings after local lexical retrieval is stable.

Default first implementation:

- Local SQLite sidecar remains the source of truth.
- Dense vectors are derived indexes that can be rebuilt.
- Lexical retrieval remains a fallback and fusion partner.
- Local embedding is default. Hosted embedding is approval-gated.

Recommended implementation order:

- [x] Add embedding model configuration and a local-only default.
- [x] Store embedding metadata in SQLite: model name, dimension, quantization, chunk id, and vector reference.
- [x] Add an in-process vector index or SQLite-compatible storage for small repos.
- [x] Add reciprocal-rank fusion between lexical and dense results.
- [x] Add result calibration coverage with local fixture repos in context retrieval/vector tests.
- [x] Consider pgvector, Qdrant, Weaviate, or Milvus adapters and defer them behind fail-closed remote vector policy.

External store adapter contract:

```python
class VectorIndex(Protocol):
    def upsert(self, chunks: list[ContextChunk]) -> None: ...
    def delete(self, chunk_ids: list[str]) -> None: ...
    def search(self, query: str, *, filters: ContextFilters, k: int) -> list[RetrievedContextChunk]: ...
    def health(self) -> ContextIndexHealth: ...
```

Tests:

- [x] Dense retrieval can be disabled and lexical still works.
- [x] Rebuilding the vector index from SQLite chunk metadata produces equivalent chunk ids.
- [x] Remote vector store configuration fails closed without approval.
- [x] Fusion never returns secret-like or context-excluded chunks.

Acceptance criteria:

- [x] Hybrid retrieval improves semantic queries without degrading exact-symbol queries.
- [x] Index drift is observable through health checks.
- [x] External stores are optional, unsupported in this release, auditable through fail-closed policy decisions, and cannot silently run.

Status note:

- Implemented Phase 7A as a local-only dense/hybrid substrate: deterministic hashed bag-of-words embeddings, SQLite-derived `context_vectors`, vector index rebuild/health helpers, dense search over cached chunks, and an opt-in `HybridContextRetriever` with lexical fallback. No hosted embeddings, model downloads, external vector DBs, provider preflight, network calls, Docker execution, adapter dispatch, shell execution, or permission grants were added.
- Hybrid retrieval is not the default chat path. `LexicalContextRetriever` and `pack_chat_context(project_root, query=...)` remain compatible, and dense/vector state is derived and rebuildable from `context_chunks`.

## Governance and Policy

Add a context transmission policy before any hosted or remote indexing path.

Policy dimensions:

- Source kind: repo file, memory, artifact, generated plan, tool output, task metadata, run goal.
- Trust level: trusted operator, untrusted repo, untrusted tool output, generated, artifact, memory.
- Destination: local process, local sidecar database, local vector service, hosted embedding, hosted reranker, hosted model.
- Redaction state: not required, redacted, restricted, forgotten.
- Budget: bytes or tokens allowed by approval scope.

Default policy:

```text
repo files: local chunking allowed, hosted indexing approval required
docs and README: local chunking allowed, hosted indexing approval required
memory records: local summary indexing allowed, hosted indexing denied by default
artifact metadata: local indexing allowed
artifact bodies: denied by default
generated summaries: local indexing allowed, non-authoritative
forgotten memory: denied and derived chunks invalidated
secret-like/context-excluded paths: denied
```

Implementation steps:

- [x] Add `context_policy.py` or extend existing policy modules.
- [x] Add stable denied/approval codes for context transmission.
- [x] Surface context-policy decisions in manifest warnings.
- [x] Evaluate approval-store integration for hosted indexing/reranking/compression; defer because hosted transmission remains denied rather than approval-gated in this local release.

Acceptance criteria:

- [x] Local context indexing does not change execution authority.
- [x] Hosted context transmission is explicit, scoped, and auditable.
- [x] Forget and redaction decisions propagate to derived chunks and indexes.

## Operator Surfaces

Keep UI and CLI context surfaces passive and inspectable.

Potential commands:

```bash
harness context inspect --json
harness context estimate "user prompt" --json
harness context chunks --json
harness context rebuild-index
harness context search "query" --json
```

Potential TUI additions:

- Compact context budget row: used tokens, budget, pinned count, retrieved count, warnings.
- Read-only context inspector showing selected chunk labels and source paths.
- Warning row for approximate token budgeting, memory non-authority, context exclusions, and blocked paths.
- No click or palette action should index, embed remotely, call providers, or grant approval without an explicit action path.

Tests:

- [x] CLI context commands are read-only unless the command name explicitly says rebuild or index.
- [x] Confirm current chat/TUI context metadata display remains passive; broader TUI context rows are deferred.
- [x] JSON outputs include `permission_granting=false`, `process_started=false`, and `filesystem_modified=false` where applicable.

Acceptance criteria:

- [x] Operators can see what context was selected and why.
- [x] Context inspection does not call providers or execute tools.

## Evaluation Plan

Create a small local eval suite before dense retrieval lands.

Fixture categories:

- Small repo with obvious README answers.
- Repo with repeated symbols in multiple files.
- Repo with secret-like paths that must be blocked.
- Repo with memory records that are relevant but non-authoritative.
- Repo with artifacts where metadata is relevant but body content must not enter context.
- Long transcript where stale turns must be summarized or omitted.

Metrics:

- Selected chunk recall for expected files.
- Token budget accuracy.
- Prompt token reduction versus static packing.
- Retrieval latency.
- Secret/context-exclude violations.
- Provenance completeness.
- Warning preservation.

Commands:

```bash
pytest tests/test_context_pack.py
pytest tests/test_context_budget.py
pytest tests/test_context_chunks.py
pytest tests/test_context_retrieval.py
pytest tests/test_cli_smoke.py -k context
```

Acceptance criteria:

- [x] No context-excluded path appears in selected chunks.
- [x] No secret-like path appears in selected chunks.
- [x] Every selected chunk has provenance.
- [x] Pinned safety blocks are always present.
- [x] Token budget tests pass for heuristic and tokenizer-backed modes.

## Rollout Plan

### Milestone 1: Budget Foundation

- [x] Add token budgeter.
- [x] Preserve current static context behavior.
- [x] Add budget report and warnings.
- [x] Update tests and docs.

Exit criteria:

- [x] Existing context tests pass.
- [x] Budget metadata appears in chat response extras.
- [x] No user-visible behavior regression.

### Milestone 2: Context Roles

- [x] Split pinned and dynamic block roles.
- [x] Add query-aware request object.
- [x] Keep static dynamic selection as bridge behavior.
- [x] Surface role counts in progress and metadata.

Exit criteria:

- [x] Pinned safety/state blocks are protected from ranking.
- [x] Dynamic blocks remain inspectable.

### Milestone 3: Chunk Cache

- [x] Add SQLite chunk tables and store methods.
- [x] Add deterministic file chunking.
- [x] Add memory/artifact metadata chunking rules.
- [x] Add invalidation for file hash changes and memory forget.

Exit criteria:

- [x] Context chunks can be rebuilt locally.
- [x] Repeated packing avoids duplicate chunk records.
- [x] Exclude and secret tests pass.

### Milestone 4: Lexical Retrieval

- [x] Add local lexical retriever.
- [x] Rank chunks by query relevance.
- [x] Pack retrieved blocks under budget.
- [x] Preserve read-only tool-loop fallback.

Exit criteria:

- [x] Query-specific context improves targeted prompts.
- [x] Retrieved blocks include score and provenance.

### Milestone 5: Manifest and Trace Auditability

- [x] Add selected chunk ids and provenance to manifests.
- [x] Add context warning propagation.
- [x] Add trace-style chat metadata for provenance ids and defer run trace attributes until a narrow trace hook exists.

Exit criteria:

- [x] Operators can audit selected context without raw hidden prompt dumps.
- [x] Warnings survive through manifests and reports.

### Milestone 6: Compression

- [x] Add deterministic extractive compression.
- [x] Evaluate rolling transcript summaries and defer live storage/packing until a narrow stale-transcript seam exists.
- [x] Gate semantic compression by fail-closed context policy; no live semantic compression config is added.

Exit criteria:

- [x] Context size drops without losing pinned safety and provenance.
- [x] Compression lineage is visible.

### Milestone 7: Hybrid Retrieval

- [x] Add local embedding path.
- [x] Add vector index abstraction.
- [x] Add lexical+dense fusion.
- [x] Evaluate optional external vector-store adapters and defer them behind fail-closed remote vector policy.

Exit criteria:

- [x] Dense retrieval is optional.
- [x] Local lexical retrieval remains a complete fallback.
- [x] Hosted/remote indexing fails closed without approval.

## Initial PR Slice

The first PR should be intentionally small:

- [x] Add `context_budget.py` with heuristic and optional tokenizer-backed budgeters.
- [x] Update `context_pack.py` to budget by tokens while preserving existing `ContextBlock` payload fields.
- [x] Add `budget_report` and `approximate_token_budget_only` warning.
- [x] Add tests for budgeting and existing pack compatibility.
- [x] Update this plan with status notes after implementation.

Status note:

- Implemented the first PR slice with a local-only `TokenBudgeter` abstraction, defensive optional `tiktoken` support, heuristic fallback, token-based block fitting, backward-compatible block payloads, manifest-level budget metadata, and focused tests.
- Later phases remain intentionally unimplemented: no dynamic retrieval, chunk cache, compression, vector search, hosted embeddings, provider preflight, Docker execution, or adapter dispatch was added.

Files likely touched:

```text
src/harness/context_budget.py
src/harness/context_pack.py
tests/test_context_budget.py
tests/test_context_pack.py
docs/plans/context_management_plan.md
```

Definition of done:

- [x] `pytest tests/test_context_pack.py tests/test_context_budget.py` passes.
- [x] `pytest tests/test_cli_smoke.py -k context` passes or any unrelated failures are documented.
- [x] Context JSON remains backward-compatible.
- [x] No provider, Docker, adapter, shell execution, or state mutation is introduced by context inspection.

## Completion Status

The local-first context management roadmap is complete through the safe local release boundary:

- Token budgeting, role-aware packing, SQLite chunk caching, local lexical retrieval, provenance manifests, opt-in deterministic extractive compression, and the local dense/hybrid substrate are implemented and tested.
- Passive CLI context surfaces are implemented with read-only defaults: `harness context inspect`, `estimate`, `chunks`, `search`, and `policy`.
- Explicit local mutation commands are named accordingly: `harness context rebuild-chunks` and `rebuild-index`.
- Context transmission policy is fail-closed for hosted embeddings, hosted reranking, hosted compression, hosted model transmission, and remote vector stores.
- External vector-store adapters, hosted semantic compression, hosted embeddings/reranking, and broad TUI context inspector work are considered and intentionally deferred outside this local-first completion boundary. They must not silently run without a future explicit approval and policy design.

## Open Questions

- Which model profiles need exact tokenizer support first: `codex_cli`, `local_openai_compatible`, or both?
- Should context chunk cache live in the main Harness SQLite database or a separate sidecar database under `.harness/context.sqlite`?
- What is the default maximum input-token budget per chat mode?
- Should file attachments and mention-based session context share the same chunk cache immediately or later?
- Should dense retrieval use a bundled local model, an optional configured model, or only an adapter interface at first?
- What is the approval UX for hosted embedding or reranking, if that is ever allowed?
