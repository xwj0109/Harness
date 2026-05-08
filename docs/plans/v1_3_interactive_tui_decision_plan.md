# v1.3 Interactive TUI Decision Plan

Status: decided.

## Summary

v1.3 decides whether and how to add a true interactive terminal experience after the v1.2 packaging and lightweight CLI polish tracks.

Decision: choose the Typer/Rich-first path for now. Do not add a TUI dependency or implementation yet. Keep improving the existing CLI and preserve a later TUI/command-palette gate.

This is a decision plan, not implementation. It must not add dependencies, a TUI runtime, command palette code, execution adapters, task-generation automation, daemon-loop changes, backend/model calls, Docker-from-queue, shell access, hosted fallback, paid fallback, OpenAI API usage, MCP/A2A, browser/email/calendar tools, broker integrations, live trading, order placement, external messaging, application submission, or active repo write automation.

## Decision

Selected option: **Option A: Stay Typer/Rich-First**.

Rationale:

- The MVP is still early and benefits more from a stable, scriptable, installable CLI than from a new interactive dependency.
- The existing `harness home`, `harness quickstart agent`, text sections, tabular list output, and JSON contracts already cover the main operator path.
- Keeping the base install small matters more than a persistent dashboard right now.
- Non-TTY, CI, SSH, and plain-terminal behavior remains predictable.
- A future TUI remains available once the CLI flows and operator vocabulary stabilize further.

Implementation consequence:

- No `textual`, prompt toolkit, TUI extra, or `harness tui` command should be added as part of v1.3.
- The next implementation work should be Typer/Rich-first CLI polish only.
- A future TUI requires a new decision checkpoint.

## Product Direction

The interactive UX should make the existing MVP easier to operate without weakening the explicit-command safety model.

If a future TUI is reconsidered, the preferred first surface remains:

- A read-only interactive operator dashboard that mirrors `harness home`.
- A command palette that shows copyable commands from existing public CLI surfaces.
- Optional detail panes for agents, tasks, leases, daemon status, and recent runs.
- No inline execution in the first TUI slice.

Defer mutating actions until the read-only TUI proves useful and has a clear confirmation model.

## Options

### Option A: Stay Typer/Rich-First

Use only current Typer/Rich-style command output and add no TUI dependency.

Pros:

- Lowest maintenance.
- Keeps install footprint small.
- Works predictably in non-TTY, CI, SSH, and simple terminals.
- Preserves current test strategy.

Cons:

- No persistent dashboard.
- No keyboard navigation.
- Less competitive with terminal-first agent tools.

### Option B: Add Textual for a Full TUI

Use Textual for an interactive app, likely exposed as `harness tui`.

Pros:

- Mature Python terminal UI framework.
- Supports panes, tables, keyboard navigation, and testable apps.
- Good fit for dashboard, queue browser, and command palette.

Cons:

- Adds a runtime dependency and packaging surface.
- Requires terminal capability fallback logic.
- Needs a new test strategy and accessibility/non-TTY rules.

### Option C: Add a Minimal Prompt/Palette Dependency

Use a narrower prompt/selection library for command palette flows only.

Pros:

- Smaller than a full TUI.
- Useful for guided command selection.
- Easier to implement than full panes.

Cons:

- Less useful for monitoring tasks, leases, and runs.
- Still introduces interactive edge cases.
- Can drift toward hidden action execution if not constrained.

## Deferred TUI Recommendation

Choose Option B only in a later milestone if the next slice is explicitly read-only and dependency-gated:

- Add a `tui` optional extra rather than a required dependency at first.
- Expose a single command such as `harness tui --project .`.
- If the optional dependency is missing, return a clear text error with install guidance.
- If stdout is not a TTY, exit cleanly and suggest JSON/headless commands.
- Keep `harness home`, `harness quickstart agent`, and all JSON outputs as the stable non-interactive path.

The first implementation slice should be read-only:

- show project initialization state;
- show task counts and selected task details;
- show daemon status and recent runs;
- show command palette entries as copyable strings only;
- do not execute palette commands inside the TUI.

## Safety Model

Interactive UX must preserve explicit operator control:

- The first TUI slice is read-only.
- Command palette entries are copyable commands, not hidden actions.
- Any future mutating TUI action requires a separate plan and explicit confirmation flow.
- JSON/headless commands remain first-class and unchanged.
- The TUI must not preflight Codex or local model backends.
- The TUI must not run Docker, shell tools, providers, network clients, browser/email/calendar tools, broker integrations, or schedulers.
- The TUI must not read or expose secrets, environment variables, backend settings, artifact contents, `.env*`, `*.pem`, `*.key`, `*.sqlite`, `.git/`, `.harness/` internals, or `secrets/`.
- It may read initialized harness persistence only through existing runtime/store APIs.

## Deferred Dependency Gate

Before any future TUI implementation, choose and document:

- dependency name and version range;
- whether it is required or optional;
- packaging impact on wheel install smoke;
- behavior when dependency is absent;
- behavior in non-TTY contexts;
- test approach for terminal rendering and keyboard events;
- whether screenshots/golden text snapshots are needed.

Deferred dependency preference:

- Prefer optional extra: `agent-harness[tui]`.
- Keep base install unchanged.
- Do not add the dependency to base `dependencies`.

## Deferred TUI Slices

### Slice 1: Optional TUI Dependency Probe

- Add optional dependency metadata only after dependency selection.
- Add `harness tui --project .` with graceful missing-dependency and non-TTY handling.
- No full dashboard yet.

### Slice 2: Read-Only Dashboard

- Render project state equivalent to `harness home`.
- Show task counts, active lease count, daemon count, and recent runs.
- No mutation.

### Slice 3: Copyable Command Palette

- List commands equivalent to `harness quickstart agent` and common inspect/list commands.
- Copy or display selected command.
- Do not execute selected commands.

### Slice 4: Decision Review

- Review whether mutating interactive actions are worth planning.
- If yes, create a separate explicit-confirmation plan.

## Typer/Rich-First Next Work

Recommended next CLI-only improvements:

- Extend `harness home` with a concise `--watch`-free refresh recommendation rather than a live dashboard.
- Add compact examples to high-traffic command help text where Typer already displays docstrings well.
- Add consistent text sections for `agents inspect`, `tasks inspect`, `daemon inspect-lease`, and policy/artifact inspection commands.
- Add a command catalog page in docs that groups common command paths by workflow.
- Keep JSON output unchanged.

Non-goals for this next work:

- No interactive prompts.
- No command palette.
- No full-screen panes.
- No new dependencies.
- No hidden execution or automatic task creation.

## Test Plan

For Typer/Rich-first follow-up:

- Focused CLI smoke tests for text output only.
- Existing JSON assertions remain unchanged.
- Packaging smoke still passes with no TUI dependency.
- Regression tests:
  - `pytest -q tests/test_cli_smoke.py tests/test_packaging_v1_2.py`;
  - `pytest -q`;
  - `git diff --check`;
  - forbidden target check for `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, and `secrets/`.

For any future TUI implementation:

- Unit tests for TUI payload builders independent of terminal rendering.
- CLI tests for missing dependency behavior.
- CLI tests for non-TTY behavior.
- Snapshot or app-level tests for the read-only dashboard if the selected framework supports them.
- Regression tests:
  - `pytest -q tests/test_cli_smoke.py tests/test_packaging_v1_2.py`;
  - `pytest -q`;
  - `git diff --check`;
  - forbidden target check for `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, and `secrets/`.

## Non-Goals

- No TUI implementation in this decision plan.
- No mutating command palette.
- No workflow engine.
- No task generation.
- No new execution adapter.
- No daemon scheduling changes.
- No backend preflight, model calls, Docker-from-queue, shell access, hosted fallback, paid fallback, OpenAI API usage, MCP/A2A, browser/email/calendar tools, broker actions, live trading, order placement, external messaging, application submission, or active repo write automation.

## Assumptions

- v1.2 Track 1 packaging/distribution polish is complete.
- v1.2 Track 2 small CLI refinements are complete.
- The base CLI remains the stable headless interface.
- Interactive UX must be optional or otherwise safe for non-TTY environments.
- Any mutating interactive workflow requires a later decision-complete plan.
