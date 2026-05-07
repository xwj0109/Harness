# v0.7.1 Agent Authoring Hardening Plan

## Summary

Harden the completed v0.7 agent authoring MVP before adding project-local custom agent import/persistence in v0.8.

This slice keeps agent authoring declarative and explicit-path only. It should improve path safety, diagnostics, deterministic validation behavior, and operator documentation for `harness agents scaffold/validate/preview`. It must not add custom agent import, registry persistence, execution adapters, task generation, scheduling, backend preflight, model calls, Docker, shell access, hosted fallback, paid fallback, OpenAI API usage, MCP/A2A, browser/email/calendar tools, active repo writes, or workflow behavior.

## Key Changes

- Tighten explicit-path safety for agent bundles:
  - reject symlinked bundle paths, profile paths, and scaffold destinations or symlinked existing parents;
  - continue rejecting `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, and `secrets/`;
  - keep validation/preview read-only and scaffold limited to the operator-specified destination.
- Improve bundle diagnostics:
  - reject unsupported files inside `profiles/` instead of silently ignoring them;
  - keep error output schema-versioned and stable for scaffold, validate, and preview;
  - preserve deterministic profile ordering and preview payload shape.
- Expand tests for:
  - symlink input/output rejection;
  - unsupported profile files;
  - invalid scaffold kind and existing file destination errors;
  - preview/validation not creating `.harness/` or touching backends.
- Update operator docs and smoke checklist only where stale.

## Test Plan

- Focused:
  - `pytest -q tests/test_agent_authoring_v0_7.py tests/test_cli_smoke.py tests/test_spec_effective_preview_v0_2.py`
- Full regression:
  - `pytest -q`
  - `git diff --check`
  - forbidden target check for `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, and `secrets/`.

## Assumptions

- v0.7 MVP is complete and committed.
- v0.7.1 is hardening only and does not require a package version bump.
- v0.8 should be a separate project-local agent import/persistence plan after this hardening checkpoint.
