# v1.0 MVP Closure Plan

## Summary

Close the v1 MVP around the current local-first harness capabilities: declarative built-in and project-local agents, manual durable task queue, runtime evidence, local daemon control-plane readiness, and one bounded read-only execution adapter.

This is a release-closure and integration-polish milestone, not a new capability milestone. It must not add new execution adapters, autonomous workflows, automatic task generation, backend fallback, hosted fallback, paid fallback, OpenAI API usage, Docker-from-queue, generic shell access, MCP/A2A, browser/email/calendar tools, broker integrations, live trading, order placement, external messaging, application submission, or active repo write automation.

## Key Changes

- Update release/version metadata after an explicit release decision:
  - choose whether to publish as `0.9.0` first or bump directly to `1.0.0`;
  - update `pyproject.toml`, `src/harness/__init__.py`, and version assertions if needed.
- Build a final operator workflow in docs:
  - scaffold a custom agent bundle;
  - validate and preview the bundle;
  - import into project-local registry;
  - inspect and preview imported agent drift/effective metadata;
  - create a manual task referencing the imported agent;
  - lease work through the queue/daemon control plane;
  - execute only the already-authorized `read_only_summary/read_only_repo_summary` adapter where metadata is exactly allowlisted.
- Audit CLI and schemas for consistency:
  - confirm JSON outputs have stable `schema_version` and `ok` where applicable;
  - confirm errors are machine-readable;
  - confirm docs match implemented command names and safety boundaries.
- Finalize smoke checklist:
  - include the agent-authoring/import lifecycle;
  - include v0.3 queue, v0.4 daemon control plane, v0.5 read-only adapter, v0.9 imported-agent preview/remove;
  - keep all unauthorized capabilities explicitly out of scope.
- Release safety audit:
  - verify no commands expose backend settings, `api_key`, `OPENAI_API_KEY`, `base_url`, environment variables, secret-like metadata, or artifact contents;
  - verify no tracked edits touch `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, or `secrets/`;
  - verify task/objective/agent lifecycle commands do not execute agents or call providers.

## Test Plan

- Focused MVP checks:
  - `pytest -q tests/test_agent_authoring_v0_7.py tests/test_sqlite_store.py tests/test_cli_smoke.py tests/test_spec_effective_preview_v0_2.py`;
  - `pytest -q tests/test_effective_policy_v0_3_5.py tests/test_tool_capabilities_v0_3_5.py tests/test_evals_traces_v0_3_5.py`;
  - `pytest -q tests/test_runner_phase_1b.py`.
- Full regression:
  - `pytest -q`;
  - `git diff --check`;
  - `git status --short`;
  - `git diff --name-only`;
  - forbidden target check for `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, and `secrets/`.

## Assumptions

- v0.9 is complete and committed before v1 closure starts.
- v1 MVP is local-first and conservative: one bounded real read-only adapter plus declarative/manual control-plane features.
- v1 closure should stabilize and document the existing surface rather than add new execution behavior.
- Additional adapters such as `repo_planning`, `simple_code_edit`, `codex_code_edit`, Docker-from-queue, shell access, hosted/paid fallback, browser/email/calendar tools, MCP/A2A, broker actions, and active repo write automation require separate post-MVP decision-complete plans.
