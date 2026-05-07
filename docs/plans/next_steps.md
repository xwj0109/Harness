# Agent Harness Next Steps

## Current Phase

v0.1 hardening is complete. The repository now has explicit run modes, backend descriptors, run manifests, stable JSON inspection output, `SECURITY.md`, non-mutating `harness doctor`, and golden v0.1 evidence tests.

The project has entered v0.2 schema and registry work. The current slice is read-only spec inspection for the built-in registry.

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

These components are declarative and read-only. They do not load user files, write `.harness/`, create tasks, execute agents, preflight backends, or schedule work.

## Immediate v0.2 Slice

Add operator-facing inspection commands for the built-in registry:

```bash
harness specs
harness specs --output json
harness specs agent <agent_id>
harness specs agent <agent_id> --output json
harness specs workbench <workbench_id>
harness specs workbench <workbench_id> --output json
```

The commands should preserve the same safety boundary: inspect built-in specs only, do not load custom files, do not read or write `.harness/`, and do not execute or preflight anything.

## Later v0.2+ Work

After read-only built-in spec inspection is stable, the next target should be a read-only custom spec-file validator/loader with no execution or persistence. Defer runtime routing, effective permission inheritance, task queues, schedulers, daemons, and autonomous behavior until the schema and validation surfaces are stable.

## Working Defaults

- Keep changes small, typed, and tested.
- Preserve local/private data-boundary safeguards.
- Keep Codex modeled as a supervised external agent backend, not a raw model provider.
- Do not add paid API fallback, hosted fallback, OpenAI API usage, or automatic execution behavior.
