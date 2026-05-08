# v1.5 TUI Detail Panes Plan

Status: Slice 1 in progress.

## Summary

v1.5 should make the existing read-only TUI useful for day-to-day inspection by adding detail panes and selectable read-only sections for the core operator objects.

This is a TUI inspection milestone, not an action or execution milestone. It must not add command execution, mutation, backend preflight, daemon actions, adapter execution, or a general command palette.

## Product Goal

The v1.4 TUI proves an optional read-only dashboard. v1.5 should turn it into a practical cockpit by letting operators inspect:

- imported agents;
- tasks and task statuses;
- active leases;
- daemon state;
- recent runs;
- safety boundaries;
- command suggestions.

The target experience is faster local inspection, not autonomous operation.

## Key Changes

- Extend the Textual dashboard layout with read-only detail panes or selectable sections.
- Keep a single optional command:

```bash
harness tui --project .
```

- Keep `harness tui --output json` as a non-interactive availability probe.
- Build detail data from existing store/runtime APIs, reusing the v1.4 dashboard model where practical.
- Add structured dashboard/detail models that can be tested without importing Textual.
- Render concise lists and details for:
  - project agents: id, workbench, source hash, profile count;
  - tasks: id, title, status, priority, objective, agent, workbench;
  - leases: id, task id, attempt id, status, owner, expiry;
  - daemon status: active daemon count, paused task count, latest event summary;
  - runs: id, status, task type, goal, created time;
  - safety: local-first and no-hidden-execution boundaries;
  - commands: copyable command suggestions only.
- Add keyboard navigation only for selection and quitting.
- Keep command suggestions visible but non-executing.

## Required Behavior

- The TUI may read initialized harness state through existing runtime/store APIs only.
- The TUI must remain useful on an uninitialized project by showing guidance without creating `.harness/`.
- Detail panes must not mutate SQLite, project-agent records, tasks, leases, daemon records, runs, artifacts, approvals, baselines, traces, specs, or repo files.
- Detail panes must not read artifact file contents.
- Detail panes must not inspect backend settings, environment variables, secrets, `.env*`, `*.pem`, `*.key`, external SQLite files, or `secrets/`.
- Detail panes must not start refresh loops that perform scheduler-like behavior.
- JSON CLI outputs remain unchanged.

## Explicit Non-Goals

- No command palette execution.
- No copy-to-clipboard dependency unless separately justified.
- No interactive prompts that create or mutate state.
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

### Slice 1: Detail Model Foundation

- Extend the TUI dashboard builder with structured detail lists for agents, tasks, leases, daemon events, and recent runs.
- Keep the model serializable and sanitized.
- Add tests for initialized and uninitialized projects.
- Verify secret-like fields and artifact contents are absent.

Implementation note: Slice 1 adds sanitized detail lists to the TUI dashboard model for agents, tasks, active leases, daemon events, and recent runs. The text renderer now includes read-only sections for those details, and tests cover initialized/uninitialized projects without requiring Textual.

### Slice 2: Textual Layout And Navigation

- Add a read-only Textual layout with sections or panes for overview, agents, tasks, leases, daemon, runs, safety, and commands.
- Add keyboard navigation for moving between sections/items.
- Keep `q` as the exit path.
- Do not add action bindings.

### Slice 3: Docs And Hygiene

- Update [operator_guide.md](../operator_guide.md), [command_catalog.md](../command_catalog.md), and [smoke_checklist.md](../smoke_checklist.md).
- Mark v1.5 complete only after focused and full tests pass.
- Keep command-palette behavior behind a later decision plan.

## Test Plan

- Model tests:
  - uninitialized projects render guidance without creating `.harness/`;
  - initialized projects include agent/task/lease/daemon/run details;
  - task and lease details match persisted queue state;
  - recent runs include metadata only, not artifact contents;
  - output does not include `api_key`, `OPENAI_API_KEY`, `base_url`, environment variables, backend settings, artifact contents, or secret-like data.

- CLI/TUI tests:
  - `harness tui --output json` remains non-interactive;
  - importing `harness.cli.main` does not require Textual;
  - TUI model tests pass without Textual installed;
  - if Textual test utilities are available, the app mounts and `q` exits cleanly.

- Regression:

```bash
pytest -q tests/test_cli_smoke.py tests/test_packaging_v1_2.py
pytest -q
git diff --check
git diff --name-only | rg '(^|/)\.harness(/|$)|(^|/)\.git(/|$)|(^|/)\.env|\.pem$|\.key$|\.sqlite$|(^|/)secrets(/|$)' || true
```

## Assumptions

- v1.4 Read-Only TUI is complete.
- Textual remains optional and excluded from the base dependency set.
- The first v1.5 release remains read-only.
- Command execution, command palettes, task mutation, daemon controls, adapter execution, and new execution adapters require separate decision-complete plans.
