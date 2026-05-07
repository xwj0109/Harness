# Agent Harness Next Steps

## Current Phase

v0.1 hardening is complete. The repository now has explicit run modes, backend descriptors, run manifests, stable JSON inspection output, `SECURITY.md`, non-mutating `harness doctor`, and golden v0.1 evidence tests.

v0.2.0 release hygiene is complete. The current slice is v0.3 manual task queue hardening and release hygiene.

The complete v0.2.0 execution plan is tracked in [v0_2_0_plan.md](v0_2_0_plan.md).

## Completed v0.2 Foundations

The first v0.2 schema and registry foundations are in place:

- `ModelProfile`.
- `ToolPolicy`.
- `MemoryScope`.
- `AgentSpec`.
- `WorkbenchSpec`.
- `SpecRegistry`.
- `builtin_spec_registry()`.
- Built-in profiles: `local_reasoning`, `codex_supervised`.
- Built-in agents: `repo_inspector`, `code_editor`, `test_runner`, `quant_researcher`, `job_researcher`.
- Built-in workbenches: `coding`, `quant`, `personal`.
- Read-only custom bundle validation with required `schema_version: harness.spec_bundle/v1`.
- Centralized explicit-path custom bundle guard.
- Registry mapping key and contained id consistency checks.
- Memory-scope hard-forbidden path invariants.
- Model-profile backend compatibility invariants.
- Tool-policy safety invariants.
- Workbench forbidden-action invariants.
- Normalized spec export for built-in and custom registries.
- Registry diff for built-in versus custom registries.
- Effective policy preview for built-in and custom registries.
- Operator documentation and smoke checklist updates.
- Full regression suite pass after documentation updates.
- Version bump to `0.2.0`.
- Final restricted-path worktree review.
- Manual task queue persistence and CLI.

These components are declarative and read-only. They do not load user files, write `.harness/`, create tasks, execute agents, preflight backends, or schedule work.

## Immediate v0.3 Slice

Harden and document the manual task queue:

Implementation queue:

1. Keep task records persistent and manually controlled.
2. Preserve `run-next` as selection plus status transition only.
3. Add no daemon, scheduler, autonomous background work, or backend execution.
4. Run full regression tests after any queue changes.
5. Prepare v0.3 release docs and version checklist after the queue is stable.

## Later v0.2+ Work

After v0.3 planning is stable, implement the manual queue in small read/write slices. Defer daemon scheduling, autonomous background behavior, broker actions, and external-message automation until a later milestone.

## Working Defaults

- Keep changes small, typed, and tested.
- Preserve local/private data-boundary safeguards.
- Keep Codex modeled as a supervised external agent backend, not a raw model provider.
- Do not add paid API fallback, hosted fallback, OpenAI API usage, or automatic execution behavior.
