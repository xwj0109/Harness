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

It does not implement model calls, Codex execution, paid API execution, editing,
Docker execution, workflows, plugins, MCP, or any agent loop.

