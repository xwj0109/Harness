# v1.1 CLI UX Plan

Status: complete.

## Summary

Make v1.1 the first operator UX milestone after the v1 MVP. The goal is to make the harness feel like a cohesive terminal product, similar in spirit to modern terminal agent tools, while preserving the harness safety model and stable machine-readable command surface.

This milestone should add a human-friendly cockpit and guided flows for the existing MVP surface. It must not add new execution adapters, autonomous workflows, automatic task generation, hosted fallback, paid fallback, OpenAI API usage, Docker-from-queue, generic shell access, MCP/A2A, browser/email/calendar tools, broker integrations, live trading, order placement, external messaging, application submission, or active repo write automation.

## Key Changes

### Slice 1: Operator Cockpit

- Add a non-mutating operator dashboard:
  - `harness home --project .`;
  - summarize initialization state, imported agents, ready/blocked/leased tasks, daemon status, recent runs, and safety boundary reminders;
  - support `--output json` with schema `harness.home/v1`.
- Keep dashboard behavior read-only:
  - do not initialize projects;
  - do not create tasks, runs, leases, artifacts, daemon events, imported agents, or lifecycle records;
  - do not preflight backends or inspect backend settings.

### Slice 2: Guided Command Composition

- Add guided command composition without hidden execution:
  - `harness quickstart agent --project .`;
  - print the exact scaffold, validate, import, task, daemon, and read-only execution commands for the operator to run;
  - do not execute the generated commands in this slice.
- Keep generated commands explicit:
  - every command should be copyable and use existing public CLI surfaces;
  - generated examples must avoid `.harness/`, secret-like paths, hosted providers, paid providers, Docker-from-queue, shell access, and active repo writes.

### Slice 3: Text Output Polish

- Improve text output consistency:
  - concise tables for agents/tasks/runs where possible;
  - clearer success/error phrasing for common agent/task/daemon paths;
  - keep all existing JSON outputs unchanged.
- Keep dependencies conservative:
  - prefer Typer/Rich capabilities already available through Typer before adding a new TUI framework;
  - defer full-screen TUI/session panes until a later decision after `home` and `quickstart` prove the workflows.
- Update README, operator guide, and smoke checklist with the UX entrypoints.

## Non-Goals

- No new execution adapters.
- No autonomous workflow engine or automatic task generation.
- No full-screen TUI framework in the first slice.
- No daemon loop changes, lease behavior changes, or recovery changes.
- No backend preflight, model calls, Docker, shell access, hosted fallback, paid fallback, OpenAI API usage, MCP/A2A, browser/email/calendar tools, broker integrations, live trading, order placement, external messaging, application submission, or active repo write automation.
- No direct reads or writes of `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, or `secrets/`.

## Test Plan

- CLI tests:
  - `harness home --project . --output json` returns `harness.home/v1`;
  - uninitialized projects show a useful non-mutating status;
  - initialized projects summarize imported agents, tasks, daemon state, and recent runs without exposing secrets;
  - `harness quickstart agent --project .` prints commands but does not create files, tasks, runs, leases, or artifacts.
- Safety tests:
  - dashboard and quickstart output do not include `api_key`, `OPENAI_API_KEY`, `base_url`, environment variables, backend settings, artifact contents, or secret-like metadata;
  - commands do not instantiate Codex, local backend clients, Docker runners, shell tools, network clients, or schedulers.
- Regression:
  - `pytest -q tests/test_cli_smoke.py tests/test_sqlite_store.py tests/test_agent_authoring_v0_7.py`;
  - `pytest -q`;
  - `git diff --check`;
  - forbidden target check for `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, and `secrets/`.

## Assumptions

- v1.0 MVP is complete and committed.
- The first UX slice should improve discoverability and operator confidence without changing execution behavior.
- Full-screen TUI, multi-session panes, live logs, keybindings, and command palettes are later slices, not the first post-MVP UX step.
- If an interactive UX dependency becomes necessary, it should be introduced by a separate dependency decision after the first dashboard and quickstart commands are implemented.

## Completion Note

v1.1 is complete as a CLI UX foundation:

- Slice 1 implemented `harness home` with schema `harness.home/v1`.
- Slice 2 implemented `harness quickstart agent` with schema `harness.quickstart_agent/v1`.
- Slice 3 added compact tab-separated headers for common text list/status commands while preserving JSON schemas.
- Operator guide and smoke checklist coverage were updated.
- Focused and full regression tests passed.

The milestone remains UX-only. It does not add execution adapters, task generation, autonomous workflows, backend preflight, Docker-from-queue, shell access, hosted fallback, paid fallback, OpenAI API usage, MCP/A2A, browser/email/calendar tools, broker actions, live trading, order placement, external messaging, application submission, or active repo write automation.
