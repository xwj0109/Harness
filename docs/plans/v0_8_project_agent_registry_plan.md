# v0.8 Project-Local Agent Registry Plan

Status: complete.

## Summary

Make v0.8 the project-local import and persistence milestone for user-authored agents. Operators should be able to import a validated v0.7 agent bundle into initialized harness persistence, list and inspect imported agents, and reference imported agents from task metadata without changing packaged built-ins.

This milestone remains declarative/control-plane only. It must not execute imported agents, create tasks automatically, start workflows, schedule daemon work, preflight model backends, call providers, run Docker, invoke shell tools, mutate active repo files, connect to brokers, place orders, send messages, submit applications, use hosted fallback, use paid fallback, use OpenAI API, add MCP/A2A, or add browser/email/calendar tools.

Completion note: v0.8 Slice 1 is implemented and verified. Project-local agent imports now persist validated agent/profile metadata, source path, import timestamp, and deterministic content hash; `harness agents import/list/inspect` are available; and task creation can reference imported project agents while preserving `spec_source_kind: project` and the imported bundle source path. Imported agents remain non-executing metadata.

## Key Changes

- Add additive project persistence for imported agent bundles:
  - store imported agent specs, attached profiles, source path metadata, import timestamp, and deterministic content hash;
  - keep built-ins immutable and separate from project-local imports;
  - reject imports whose ids shadow built-ins or existing project-local agents unless an explicit future update/replace command is planned separately.
- Add CLI:
  - `harness agents import <bundle_path> --project . --output json`;
  - `harness agents list --project . --output json`;
  - `harness agents inspect <agent_id> --project . --output json`;
  - `harness agents preview <bundle_path> --output json` remains read-only and does not persist.
- Define JSON schemas:
  - `harness.project_agent/v1` for import and inspect;
  - `harness.project_agents/v1` for list.
- Extend task reference validation:
  - task `--agent` may resolve against built-ins or imported project-local agents;
  - task records should retain enough source metadata to distinguish built-in versus project-local agent references;
  - imported agent references do not authorize execution or new tools.
- Preserve explicit-path safety:
  - imports read only the supplied bundle path;
  - imports reject symlinks, forbidden paths, malformed YAML, id shadowing, missing refs, parent cycles, and policy broadening using the v0.7.1 authoring loader.

## Test Plan

- Store tests:
  - fresh initialization creates additive project-agent persistence;
  - importing a valid bundle persists agent/profile/source/hash metadata;
  - imported agents list and inspect deterministically;
  - duplicate project-local agent ids are rejected;
  - built-in id shadowing remains rejected;
  - existing initialized projects migrate safely.
- CLI tests:
  - `agents import/list/inspect --output json` return stable schema-versioned payloads;
  - unknown imported agent ids return stable JSON errors;
  - task creation accepts imported agent ids while preserving source metadata;
  - imports do not create tasks, runs, artifacts, leases, daemon events, or backend preflight.
- Regression:
  - `pytest -q tests/test_agent_authoring_v0_7.py tests/test_sqlite_store.py tests/test_cli_smoke.py tests/test_spec_effective_preview_v0_2.py`;
  - `pytest -q`;
  - `git diff --check`;
  - forbidden target check for `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, and `secrets/`.

## Assumptions

- v0.7 and v0.7.1 are complete and committed.
- v0.8 persists validated custom agent declarations only; it does not execute them.
- Built-in packaged specs remain immutable repo assets loaded through `builtin_spec_registry()`.
- Imported project-local agents should be stored through existing harness runtime persistence, not through direct `.harness/` file edits.
- Agent update/replace/delete semantics can be deferred unless needed for MVP usability.
