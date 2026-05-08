# v1.6 TUI Filter/Search Plan

Status: Slice 2 in progress.

## Summary

v1.6 should add read-only filter/search support to the optional TUI detail panes. The goal is to help operators find agents, tasks, leases, daemon events, runs, and command suggestions as projects grow, without adding command execution or mutation.

This is a TUI inspection milestone, not an action milestone. Search must operate over already-loaded dashboard data only.

## Product Goal

The v1.5 TUI exposes read-only detail panes. v1.6 should make those panes navigable at scale by adding a small local search/filter layer.

Search targets:

- agent id, workbench id, source path, and profile count;
- task id, title, status, priority, objective id, agent id, workbench id, execution adapter, and task type;
- lease id, task id, attempt id, status, owner, and expiry;
- run id, status, task type, goal, and created time;
- daemon event type and message;
- command suggestions;
- safety boundary labels.

## Key Changes

- Add a Textual-free filter model that can be tested without importing Textual.
- Add a read-only TUI search/filter input or mode in the optional Textual app.
- Keep `harness tui --output json` as a non-interactive availability probe.
- Filter already-loaded pane lines from the v1.5 pane model.
- Show the current query and match counts per pane.
- Add a clear-filter keyboard binding if straightforward.
- Keep command suggestions visible but non-executing.
- Update operator docs and smoke checklist after implementation.

## Required Behavior

- Search is local and in-memory over the dashboard/pane model.
- Search must not perform filesystem crawling.
- Search must not perform database-wide ad hoc queries outside existing harness store APIs.
- Search must not read artifact file contents.
- Search must not inspect backend settings, environment variables, secrets, `.env*`, `*.pem`, `*.key`, external SQLite files, or `secrets/`.
- Search must not mutate SQLite, project-agent records, tasks, leases, daemon records, runs, artifacts, approvals, baselines, traces, specs, or repo files.
- Search must not execute commands, acquire leases, run daemon actions, execute adapters, preflight backends, run Docker, invoke shell tools, call providers, or start background work.
- JSON CLI outputs remain unchanged.

## Explicit Non-Goals

- No command palette execution.
- No command execution from search results.
- No copy-to-clipboard dependency unless separately justified.
- No task creation.
- No objective creation.
- No agent import/remove.
- No task status/cancel/retry.
- No `tasks run-next`.
- No daemon actions from the TUI.
- No adapter execution from the TUI.
- No Codex invocation.
- No local model backend preflight.
- No Docker.
- No shell access.
- No hosted fallback.
- No paid fallback.
- No OpenAI API usage.
- No MCP/A2A.
- No browser/email/calendar tools.
- No broker actions, live trading, capital allocation, or order placement.
- No active repo writes.
- No unmanaged daemon loop.

## Implementation Slices

### Slice 1: Search Model Foundation

- Add a function such as `filter_tui_panes(panes, query)` that returns filtered panes and match metadata.
- Match case-insensitively over pane titles and line text.
- Preserve pane order and section ids.
- Return all panes unchanged for an empty query.
- Add tests for agent/task/lease/run/daemon/command matches.
- Add tests proving secret-like fields and artifact contents are absent from searchable data.

Implementation note: Slice 1 adds `filter_tui_panes(panes, query)` as a Textual-free, local in-memory filter over the existing sanitized pane model. It preserves pane order, reports match counts, returns all panes for empty queries, and is covered by search tests for agents, tasks, leases, runs, daemon events, and commands.

### Slice 2: Textual Search UI

- Add a read-only search input or mode to the optional Textual app.
- Update pane rendering when the query changes.
- Show match counts and empty-state messages.
- Add keyboard bindings for focusing search, clearing search, tab navigation, and quitting.
- Do not add action bindings for selected results.

Implementation note: Slice 2 wires the existing filter model into the optional Textual app with a search input, match-status line, filtered pane rendering, empty-state text, and keyboard bindings for search focus, clear search, pane navigation, and quit only. Search remains local and read-only over already-loaded pane data.

### Slice 3: Docs And Hygiene

- Update [operator_guide.md](../operator_guide.md), [command_catalog.md](../command_catalog.md), and [smoke_checklist.md](../smoke_checklist.md).
- Mark v1.6 complete only after focused and full tests pass.
- Keep command-palette behavior behind a later decision plan.

## Test Plan

- Model tests:
  - empty query returns original panes;
  - agent id queries match the agents pane;
  - task title/status/agent/workbench queries match the tasks pane;
  - lease id/task id/owner queries match the leases pane;
  - run id/task type/goal queries match the runs pane;
  - daemon event queries match the daemon pane;
  - command text queries match the commands pane;
  - output does not include `api_key`, `OPENAI_API_KEY`, `base_url`, environment variables, backend settings, artifact contents, or secret-like data.

- CLI/TUI tests:
  - `harness tui --output json` remains non-interactive;
  - importing `harness.cli.main` does not require Textual;
  - search model tests pass without Textual installed;
  - if Textual test utilities are available, the app mounts and `q` exits cleanly.

- Regression:

```bash
pytest -q tests/test_cli_smoke.py tests/test_packaging_v1_2.py
pytest -q
git diff --check
git diff --name-only | rg '(^|/)\.harness(/|$)|(^|/)\.git(/|$)|(^|/)\.env|\.pem$|\.key$|\.sqlite$|(^|/)secrets(/|$)' || true
```

## Assumptions

- v1.5 TUI Detail Panes is complete.
- Textual remains optional and excluded from the base dependency set.
- The first v1.6 release remains read-only.
- Search operates over sanitized dashboard data only.
- Command execution, command palettes, task mutation, daemon controls, adapter execution, and new execution adapters require separate decision-complete plans.
