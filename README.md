# Agent Harness

Phase 1A foundation for a local-first custom agent harness.

This phase includes only local infrastructure:

- CLI scaffolding.
- `.harness/` project state.
- Config loading.
- SQLite persistence.
- Run artifact generation.
- Backend metadata/capability schemas.
- Local read-only tools.
- Path traversal protection.
- Secret-path blocking and secret scanner primitives.

Later phases added supervised Codex editing and Docker-sandboxed test execution.
Paid API execution, generic shell execution, workflows, plugins, and MCP remain
outside the current implemented scope.

## Repository Layout

```text
.
├── README.md
├── AGENTS.md
├── SECURITY.md
├── docs/
│   ├── operator_guide.md
│   ├── smoke_checklist.md
│   └── plans/
├── src/
│   └── harness/
├── tests/
├── pyproject.toml
└── .gitignore
```

## Operator Docs

Current operator-facing flows are documented in:

- [docs/operator_guide.md](docs/operator_guide.md)
- [docs/smoke_checklist.md](docs/smoke_checklist.md)

Security boundaries and threat-model notes are documented in:

- [SECURITY.md](SECURITY.md)

## Planning Docs

Repo-local planning references are tracked in:

- [docs/plans/agent_harness_master_plan.md](docs/plans/agent_harness_master_plan.md)
- [docs/plans/next_steps.md](docs/plans/next_steps.md)
- [docs/plans/v0_1_hardening_plan.md](docs/plans/v0_1_hardening_plan.md)
