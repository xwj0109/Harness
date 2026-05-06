# Agent Harness Next Steps

## Immediate Phase

The next phase is v0.1 hardening before v0.2 abstractions. The repository is already a safety kernel with local initialization, persistence, approvals, Codex supervision, isolated edits, Docker-sandboxed tests, path protection, and regression coverage. The next work should stabilize those foundations before introducing generic agent and workbench specifications.

## v0.1 Priorities

Implement the remaining v0.1 items in this order:

1. Add a `RunMode` enum so run intent is explicit instead of inferred only from task type.
2. Add a `BackendDescriptor` contract that formalizes backend metadata, capabilities, and routing-relevant behavior.
3. Add a run-level `manifest.json` for every run so artifacts and key execution metadata have a stable per-run index.
4. Add stable JSON/JSONL CLI output for machine consumers while preserving human-readable defaults.
5. Add `SECURITY.md` and a formal threat model covering local/private boundaries, approvals, isolation, and forbidden paths.
6. Add `harness doctor` to inspect local readiness without mutating project state.
7. Add golden end-to-end tests for the core safety and artifact flows.

## Deferred Until v0.1 Is Stable

Do not start v0.2 abstractions until the v0.1 hardening work is complete and tested. Deferred v0.2 items include:

- `AgentSpec`.
- `WorkbenchSpec`.
- `ModelProfile`.
- `ToolPolicy`.
- `MemoryScope`.
- Agent registry.
- Built-in starter agents such as `repo_inspector`, `code_editor`, `test_runner`, `quant_researcher`, and `job_researcher`.

## Working Defaults

- Keep changes small, typed, and tested.
- Preserve the local/private data boundary safeguards.
- Keep Codex modeled as a supervised external agent backend, not a raw model provider.
- Do not add paid API fallback, hosted fallback, or OpenAI API usage.
