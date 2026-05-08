# v1.4 Read-Only TUI Plan

Status: Slice 2 in progress.

## Summary

v1.4 adds the first true interactive terminal surface: a read-only operator dashboard that mirrors the existing `harness home` and command-catalog flows.

This is a UX/control-plane milestone, not an execution milestone. The first TUI must help operators inspect project state and discover commands without adding hidden execution, mutation, scheduling, backend preflight, or provider/tool access.

## Product Goal

The TUI should make the MVP easier to operate from a terminal while preserving the explicit-command safety model.

The first useful surface is:

- project initialization state;
- imported agent summary;
- objective/task counts and task-state counts;
- active lease summary;
- daemon status summary;
- recent run summary;
- local-first safety reminders;
- copyable command suggestions that match existing CLI commands.

The TUI should feel like an operator cockpit, not an autonomous agent shell.

## Decision

Implement a **read-only Textual-based dashboard** behind an optional TUI extra.

Rationale:

- Textual is the best fit for real panes, tables, keyboard navigation, and future detail views.
- Keeping it optional preserves the small default install and scriptable CLI.
- A read-only first slice lets us validate the interface without designing confirmation flows for mutations.
- Existing `harness home` and store-summary helpers already provide most of the required data without new backend or daemon behavior.

## Key Changes

- Add optional dependency metadata for a TUI extra, for example:

```toml
[project.optional-dependencies]
tui = ["textual>=..."]
```

- Add a public command:

```bash
harness tui --project .
```

- If the optional TUI dependency is missing, return a clear install hint instead of a stack trace:

```text
Install the TUI extra with: python3 -m pip install "agent-harness[tui]"
```

- Implement a read-only dashboard using existing project/store/config summary code where possible.
- Keep `harness home --output json` as the stable non-interactive source of truth.
- Add keyboard-only exits, at minimum `q` and `Ctrl+C`.
- Add visible but non-executing command suggestions, such as:
  - `harness quickstart agent --project .`;
  - `harness agents list --project .`;
  - `harness tasks list --project .`;
  - `harness daemon status --project .`;
  - `harness runs --project .`.
- Update operator docs and smoke checklist with install and launch examples.

## Required Behavior

- `harness tui` may read initialized harness state through existing runtime/store APIs.
- On an uninitialized project, it may display the same kind of guidance as `harness home`, but must not initialize the project.
- The dashboard must not mutate SQLite, project agent records, tasks, leases, daemon records, runs, artifacts, approvals, baselines, traces, or specs.
- The dashboard must not start a background refresh loop that performs hidden scheduler work.
- Any refresh behavior, if added, must be manual or purely read-only.
- JSON CLI outputs remain unchanged.

## Explicit Non-Goals

- No command palette execution.
- No interactive prompts that create or mutate state.
- No task creation.
- No objective creation.
- No agent import/remove.
- No task status/cancel/retry.
- No `tasks run-next`.
- No `daemon run-once`, `daemon recover`, `daemon stop`, or adapter execution from the TUI.
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

## Safety Boundaries

- The TUI is read-only and local-first.
- It must not read or expose secrets, backend settings, `api_key`, `OPENAI_API_KEY`, `base_url`, environment variables, artifact file contents, `.env*`, `*.pem`, `*.key`, external SQLite files, or `secrets/`.
- It must not directly read or modify `.harness/` planning/edit targets; runtime project state may only be accessed through existing harness APIs.
- It must not modify `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, or `secrets/` as repo-tracked edit targets.

## Implementation Slices

### Slice 1: Optional Dependency And Launch Stub

- Add optional `tui` extra.
- Add `harness tui --project .`.
- Implement missing-dependency handling.
- Add tests proving default install paths still work without importing Textual at module import time.
- Add packaging smoke coverage that the default wheel remains usable without the TUI extra.

Implementation note: Slice 1 adds the optional `tui` extra, `harness tui`, a missing-Textual install hint, a minimal Textual launch module for environments that install the extra, and smoke coverage for the base install path.

### Slice 2: Read-Only Dashboard MVP

- Implement a Textual app that renders project summary from existing `home`/store data.
- Include panels for project state, agents, tasks, leases/daemon, recent runs, and safety.
- Add command-suggestion panel with copyable command text if straightforward; suggestions must not execute.
- Add tests for rendering/smoke behavior using Textual test utilities when available.

Implementation note: Slice 2 adds a Textual-free dashboard model and text renderer used by the optional Textual app. It reports project initialization, summary counts, task-state counts, active leases, daemon count, recent runs, safety boundaries, and command suggestions without executing commands or mutating project state.

### Slice 3: Docs And Release Hygiene

- Update [operator_guide.md](../operator_guide.md), [command_catalog.md](../command_catalog.md), and [smoke_checklist.md](../smoke_checklist.md).
- Mark v1.4 complete only after tests pass and safety boundaries are verified.
- Keep future command-palette or mutating TUI behavior behind a separate decision plan.

## Test Plan

- CLI tests:
  - `harness tui --project .` returns a stable install hint when the TUI extra is unavailable.
  - importing `harness.cli.main` does not require Textual.
  - `harness --help` remains available without the TUI extra.
  - `harness tui` does not create `.harness/` for uninitialized projects.
  - `harness tui` does not create tasks, runs, artifacts, leases, daemon events, approvals, baselines, traces, or backend preflight output.

- TUI tests:
  - initialized-project dashboard renders project state.
  - uninitialized-project dashboard renders guidance only.
  - task counts, active leases, daemon status, and recent runs are shown when present.
  - command suggestions are visible but do not execute.
  - `q` exits cleanly.

- Regression:

```bash
pytest -q tests/test_cli_smoke.py tests/test_packaging_v1_2.py
pytest -q
git diff --check
git diff --name-only | rg '(^|/)\.harness(/|$)|(^|/)\.git(/|$)|(^|/)\.env|\.pem$|\.key$|\.sqlite$|(^|/)secrets(/|$)' || true
```

## Assumptions

- v1.3 Typer/Rich-first polish is complete.
- The TUI dependency should be optional, not part of the base install.
- `harness home` remains the non-interactive source of truth.
- The first TUI is read-only and does not execute commands.
- A command palette, task actions, daemon actions, adapter execution, or any mutation from the TUI requires a later decision-complete plan.
