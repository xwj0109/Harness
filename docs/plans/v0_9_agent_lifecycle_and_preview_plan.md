# v0.9 Agent Lifecycle and Preview Plan

Status: complete.

## Summary

Make v0.9 the project-agent usability and lifecycle hardening milestone. Operators should be able to understand imported project agents from persisted state, detect source-bundle drift, preview effective imported-agent policy/metadata, and make an explicit decision about remove/refresh behavior.

This milestone remains declarative/control-plane only. It must not execute agents, add execution adapters, create tasks automatically, schedule daemon work, preflight model backends, call providers, run Docker, invoke shell tools, mutate active repo files, use hosted fallback, use paid fallback, use OpenAI API, add MCP/A2A, add browser/email/calendar tools, connect to brokers, place orders, send messages, submit applications, or add workflow automation.

Completion note: v0.9 is implemented and verified. Project-local imported agents can be previewed from persisted state with `harness agents preview-imported`, source drift is reported as `verified`, `changed`, `missing`, or `unavailable`, and `harness agents remove` deletes only unused project-local imports. Refresh/replace remains explicitly deferred so imported-agent lifecycle changes stay deliberate.

## Key Changes

- Add project-agent effective preview:
  - `harness agents preview-imported <agent_id> --project . --output json`;
  - output should include imported agent spec, profiles, parent chain, effective agent view, workbench, source path, import hash, and drift status;
  - JSON schema: `harness.project_agent_preview/v1`.
- Add source drift inspection:
  - recompute the current source bundle hash without rewriting persisted records;
  - report `verified`, `changed`, `missing`, or `unavailable` as drift status;
  - keep output metadata-only and never include secret-like data or artifact contents.
- Decide and implement the smallest lifecycle commands:
  - `harness agents remove <agent_id> --project . --output json` may remove only unused imported agents;
  - `harness agents refresh <agent_id> --project . --output json` should be implemented only if a clean immutable replace contract is defined; otherwise explicitly defer refresh and document import immutability.
- Preserve task safety:
  - removing an imported agent referenced by any task must be rejected with a stable JSON error;
  - refresh, if implemented, must not mutate existing task records or broaden policy boundaries.
- Update docs and smoke checklist for imported-agent preview, drift inspection, and lifecycle decisions.

## Test Plan

- Store tests:
  - imported-agent preview resolves parent chain and effective agent metadata from persisted project state;
  - drift inspection reports `verified`, `changed`, `missing`, and `unavailable`;
  - remove succeeds only for unused imported agents;
  - remove rejects built-in ids, unknown ids, and task-referenced imported agents.
- CLI tests:
  - `agents preview-imported --output json` returns `harness.project_agent_preview/v1`;
  - lifecycle errors are stable and schema-versioned;
  - commands do not create tasks, runs, artifacts, leases, daemon events, or backend preflight.
- Regression:
  - `pytest -q tests/test_agent_authoring_v0_7.py tests/test_sqlite_store.py tests/test_cli_smoke.py tests/test_spec_effective_preview_v0_2.py`;
  - `pytest -q`;
  - `git diff --check`;
  - forbidden target check for `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, and `secrets/`.

## Assumptions

- v0.8 project-local agent import/list/inspect is complete and committed.
- Project-local agents remain declarative until a separate execution plan authorizes otherwise.
- Built-ins remain immutable and cannot be removed or refreshed by project-agent lifecycle commands.
- Source drift is reported, not repaired, unless refresh semantics are made decision-complete during this milestone.
