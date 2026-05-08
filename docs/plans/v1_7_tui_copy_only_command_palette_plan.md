# v1.7 TUI Copy-Only Command Palette Plan

Status: Slice 1 in progress.

## Summary

v1.7 should add a copy-only command palette to the optional TUI. The palette should help operators discover and manually copy existing CLI commands grouped by workflow, while preserving the explicit-command safety model.

This is a command discovery milestone, not an execution milestone. The TUI must not run commands, spawn subprocesses, mutate harness state, or invoke providers/tools.

## Product Goal

The v1.4-v1.6 TUI provides a read-only dashboard, detail panes, and local search. v1.7 should make the existing command catalog easier to use from the TUI by surfacing workflow-grouped command templates.

The first palette is display-only:

- searchable command entries;
- grouped by workflow;
- command text shown in a detail pane;
- no command execution;
- no clipboard dependency unless separately justified.

## Command Groups

Initial palette groups should mirror [command_catalog.md](../command_catalog.md):

- orientation;
- agent authoring;
- project agents;
- built-in specs;
- objectives and tasks;
- daemon control plane;
- authorized read-only adapter;
- runtime evidence;
- packaging smoke.

Entries should include:

- stable id;
- group id;
- title;
- command template;
- description;
- mutation flag describing what would happen if the operator manually ran the command outside the TUI;
- safety note when relevant.

## Key Changes

- Add a Textual-free command palette model that can be tested without importing Textual.
- Add search/filter support over command ids, group ids, titles, descriptions, and command text.
- Add a read-only palette view in the optional Textual app.
- Display selected command text for manual copy.
- Keep existing TUI pane search/filter behavior.
- Keep `harness tui --output json` as a non-interactive availability probe.
- Update operator docs and smoke checklist after implementation.

## Required Behavior

- Palette entries are static command templates, not executable actions.
- The TUI must not execute commands directly or indirectly.
- The TUI must not call subprocess, shell, Docker, Codex, local model backends, hosted providers, paid providers, MCP/A2A, browser/email/calendar tools, broker APIs, or networked services.
- The TUI must not create tasks, objectives, agents, runs, artifacts, leases, daemon records, approvals, baselines, traces, specs, or repo files.
- The TUI must not acquire leases, recover daemons, stop daemons, execute adapters, or mutate task status.
- The TUI must not inspect backend settings, environment variables, secrets, `.env*`, `*.pem`, `*.key`, external SQLite files, or `secrets/`.
- The TUI must not read artifact file contents.
- JSON CLI outputs remain unchanged.

## Explicit Non-Goals

- No command execution from the palette.
- No subprocess calls.
- No shell invocation.
- No copy-to-clipboard dependency in the first slice.
- No confirmation prompts for mutating commands.
- No task creation forms.
- No daemon controls.
- No adapter execution.
- No Codex invocation.
- No local model backend preflight.
- No Docker.
- No hosted fallback.
- No paid fallback.
- No OpenAI API usage.
- No MCP/A2A.
- No browser/email/calendar tools.
- No broker actions, live trading, capital allocation, or order placement.
- No active repo writes.
- No unmanaged daemon loop.

## Implementation Slices

### Slice 1: Palette Model Foundation

- Add a command palette model with deterministic grouped entries.
- Keep it Textual-free and serializable.
- Add filtering over command ids, group ids, titles, descriptions, and command templates.
- Add tests proving entries are stable, grouped, searchable, and non-executing metadata only.
- Verify no palette entry exposes `api_key`, `OPENAI_API_KEY`, `base_url`, environment variables, backend settings, artifact contents, or secret-like data.

Implementation note: Slice 1 adds `build_command_palette()` and `filter_command_palette()` as Textual-free, static metadata helpers. Palette entries are workflow-grouped command templates with mutation/safety metadata; filtering is local and read-only over ids, groups, titles, descriptions, safety notes, and command text.

### Slice 2: Textual Palette View

- Add a read-only command palette pane or mode to the optional Textual app.
- Show grouped entries and selected command text.
- Reuse existing search/filter patterns where practical.
- Add keyboard navigation only.
- Do not add execution or clipboard bindings.

### Slice 3: Docs And Hygiene

- Update [operator_guide.md](../operator_guide.md), [command_catalog.md](../command_catalog.md), and [smoke_checklist.md](../smoke_checklist.md).
- Mark v1.7 complete only after focused and full tests pass.
- Keep command execution or confirmed TUI actions behind a later decision plan.

## Test Plan

- Model tests:
  - palette contains all required groups;
  - command ids are unique and deterministic;
  - entries include command text and safety/mutation metadata;
  - filtering works over group, title, description, and command text;
  - output does not include `api_key`, `OPENAI_API_KEY`, `base_url`, environment variables, backend settings, artifact contents, or secret-like data.

- CLI/TUI tests:
  - `harness tui --output json` remains non-interactive;
  - importing `harness.cli.main` does not require Textual;
  - palette model tests pass without Textual installed;
  - if Textual test utilities are available, the app mounts and `q` exits cleanly.

- Regression:

```bash
pytest -q tests/test_cli_smoke.py tests/test_packaging_v1_2.py
pytest -q
git diff --check
git diff --name-only | rg '(^|/)\.harness(/|$)|(^|/)\.git(/|$)|(^|/)\.env|\.pem$|\.key$|\.sqlite$|(^|/)secrets(/|$)' || true
```

## Assumptions

- v1.6 TUI Filter/Search is complete.
- Textual remains optional and excluded from the base dependency set.
- The first v1.7 release remains copy-only and non-executing.
- Palette entries are static templates for manual operator use.
- Executing palette commands, clipboard integration, task mutation, daemon controls, adapter execution, and new execution adapters require separate decision-complete plans.
