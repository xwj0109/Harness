# v1.8 TUI Usability Polish Plan

Status: Complete.

## Summary

v1.8 should make the optional read-only TUI easier to navigate after the v1.7 command palette milestone. The goal is usability polish only: clearer pane organization, keyboard-oriented navigation hints, and a better local mental model for the dashboard and palette.

This is not an execution milestone. The TUI remains read-only and local in-memory. It must not execute commands, spawn subprocesses, mutate harness state, persist UI preferences, or invoke providers/tools.

## Product Goal

The v1.4-v1.7 TUI now has:

- read-only project dashboard panes;
- task/agent/lease/run detail panes;
- local pane search;
- a copy-only command palette.

v1.8 should reduce operator friction without changing the safety model. The polish should help users quickly understand where they are, what is selected, which panes are visible, and how to move around.

## Key Changes

- Add a Textual-free TUI view model that groups panes into stable sections:
  - project overview;
  - queue and daemon state;
  - agents and specs;
  - runtime evidence;
  - command palette;
  - safety.
- Add deterministic pane ordering metadata so tests can validate the visible layout without importing Textual.
- Add compact navigation/help metadata for the optional TUI:
  - search;
  - clear search;
  - next/previous pane;
  - quit;
  - command palette is copy-only.
- Improve empty-state and no-match text for dashboard and palette searches.
- Keep `harness tui --output json` as a non-interactive availability probe with unchanged schema.
- Update operator docs and smoke checklist after implementation.

## Required Behavior

- The TUI remains optional; importing the main CLI must not require Textual.
- The TUI loads only existing dashboard and palette metadata already supported by `harness.tui`.
- Search remains local and in-memory over already-loaded pane and palette metadata.
- All new view-model helpers must be deterministic and testable without Textual.
- The TUI must not persist UI preferences in `.harness/`, SQLite, config files, or user-home state in this milestone.
- The TUI must not execute commands directly or indirectly.
- The TUI must not call subprocess, shell, Docker, Codex, local model backends, hosted providers, paid providers, MCP/A2A, browser/email/calendar tools, broker APIs, or networked services.
- The TUI must not create tasks, objectives, agents, runs, artifacts, leases, daemon records, approvals, baselines, traces, specs, or repo files.
- The TUI must not acquire leases, recover daemons, stop daemons, execute adapters, or mutate task status.
- The TUI must not inspect backend settings, environment variables, secrets, `.env*`, `*.pem`, `*.key`, external SQLite files, or `secrets/`.
- The TUI must not read artifact file contents.

## Explicit Non-Goals

- No command execution.
- No subprocess calls.
- No shell invocation.
- No clipboard integration.
- No saved preferences or persisted layouts.
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

### Slice 1: View Model And Section Ordering

- Add a Textual-free helper that combines dashboard panes and command palette panes into sectioned, ordered view metadata.
- Keep pane ids stable and deterministic.
- Add tests for section membership, pane ordering, no-match behavior, and sanitized output.

Implementation note: Slice 1 adds `build_tui_view_model()` as a Textual-free helper that combines filtered dashboard panes and filtered command palette panes into deterministic sections, pane order metadata, navigation hints, search counts, and explicit no-match state. The helper is read-only and in-memory only.

### Slice 2: Textual Presentation Polish

- Update the optional Textual app to render section labels or compact headers using the new view model.
- Improve search status text so pane matches and palette matches are easy to distinguish.
- Add clearer keyboard help text through existing Textual footer/bindings only.
- Keep keyboard navigation only; no mouse-only flows and no command execution.

Implementation note: Slice 2 updates the optional Textual app to render from `build_tui_view_model()`, including compact section headers, unified search status, navigation hints, and the model-level no-match state. The app remains read-only and does not add command execution, clipboard bindings, persistence, or daemon controls.

### Slice 3: Docs And Hygiene

- Update [operator_guide.md](../operator_guide.md), [command_catalog.md](../command_catalog.md), and [smoke_checklist.md](../smoke_checklist.md).
- Mark v1.8 complete only after focused and full tests pass.
- Keep persisted layouts, clipboard integration, command execution, or any TUI actions behind later decision-complete plans.

Completion note: v1.8 is complete as read-only TUI usability polish. The Textual-free view model, deterministic section ordering, navigation hints, unified search status, model-level no-match state, Textual section rendering, operator docs, command catalog, and smoke checklist are implemented and verified. No command execution, clipboard integration, persisted layout, state mutation, backend preflight, Docker, provider calls, or daemon controls were added.

## Test Plan

- Model tests:
  - section ids are deterministic;
  - pane ids remain stable;
  - palette panes appear under a command-palette section;
  - safety panes remain present;
  - no-match states are explicit and sanitized;
  - output does not include `api_key`, `OPENAI_API_KEY`, `base_url`, environment variables, backend settings, artifact contents, or secret-like data.

- CLI/TUI tests:
  - `harness tui --output json` remains non-interactive and schema-compatible;
  - importing `harness.cli.main` does not require Textual;
  - TUI view-model tests pass without Textual installed;
  - if Textual test utilities are available, the app mounts and `q` exits cleanly.

- Regression:

```bash
pytest -q tests/test_cli_smoke.py tests/test_packaging_v1_2.py
pytest -q
git diff --check
git diff --name-only | rg '(^|/)\.harness(/|$)|(^|/)\.git(/|$)|(^|/)\.env|\.pem$|\.key$|\.sqlite$|(^|/)secrets(/|$)' || true
```

## Assumptions

- v1.7 TUI Copy-Only Command Palette is complete.
- Textual remains optional and excluded from the base dependency set.
- v1.8 is usability polish for the existing read-only TUI, not a new product surface.
- View preferences remain in-memory only for this milestone.
- Saved layouts, clipboard integration, command execution, task mutation, daemon controls, adapter execution, and new execution adapters require separate decision-complete plans.
