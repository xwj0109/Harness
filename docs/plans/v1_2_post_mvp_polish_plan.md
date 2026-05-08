# v1.2 Post-MVP Polish Plan

Status: in progress.

## Summary

v1.2 should make the completed MVP easier to install, try, and operate before adding more execution capability or a full-screen interface. The priority order is:

1. Packaging/distribution polish.
2. Small Typer/Rich-style CLI refinements.
3. True interactive TUI or command palette, behind a separate decision gate.

This plan is intentionally not an execution-adapter plan. It must not add task-generation automation, new daemon behavior, backend/model calls, Docker-from-queue, shell access, hosted fallback, paid fallback, OpenAI API usage, MCP/A2A, browser/email/calendar tools, broker integrations, live trading, order placement, external messaging, application submission, or active repo write automation.

## Track 1: Packaging and Distribution Polish

Status: complete.

Goal: make the local-first MVP installable and verifiable in a clean operator environment.

Recommended scope:

- Verify package metadata:
  - project name, version, description, classifiers, Python requirement, license metadata, and console script.
  - package-data inclusion for `src/harness/builtin_specs/**/*.yaml`.
- Add a clean install smoke path:
  - build wheel and source distribution locally;
  - install into a temporary virtual environment;
  - run `harness --help`, `harness home --project <tmp> --output json`, `harness specs --output json`, and `harness quickstart agent --output json`.
- Add packaging tests or scripts only if they remain local and deterministic.
- Update README with a short install/verify section.
- Update smoke checklist with packaging verification commands.

Non-goals:

- No publishing to PyPI or any remote registry in this slice.
- No dependency expansion unless required for packaging correctness.
- No background service installers, launch agents, shell completions, or TUI dependency decisions yet.
- No reads or writes of `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, or `secrets/` except normal runtime behavior in explicit temporary test projects.

Exit criteria:

- Local build succeeds.
- Clean virtualenv install succeeds.
- Packaged built-in specs are available after install.
- CLI entrypoint works from the installed wheel.
- Existing full regression suite passes.

Completion note:

- Package metadata now includes license expression, authors, keywords, classifiers, and dev packaging dependency metadata.
- Package-data patterns explicitly cover packaged built-in YAML specs.
- Packaging tests build a local wheel from a temporary source copy, inspect wheel metadata/spec contents, install the wheel into a temporary virtual environment, and verify installed `harness` CLI commands.
- README and smoke checklist include local wheel install verification.
- No publishing, remote registry upload, new execution behavior, backend preflight, Docker-from-queue, shell access, hosted fallback, paid fallback, OpenAI API usage, or TUI dependency was added.

## Track 2: Small Typer/Rich-Style CLI Refinements

Status: complete.

Goal: improve day-to-day terminal ergonomics without changing behavior or adding a full-screen TUI.

Candidate refinements:

- Add clearer section headings to selected text output while leaving JSON unchanged.
- Add compact `--help` examples in command docstrings/help text for high-value paths.
- Improve `harness home` text output with better grouping and next-action clarity.
- Add optional text output affordances only where Typer/Rich support is already available through existing dependencies.
- Keep machine-readable JSON schemas stable.

Non-goals:

- No interactive prompts that mutate state.
- No command palette, full-screen panes, live session UI, keybindings, or terminal multiplexer integration.
- No new runtime dependencies unless a separate dependency decision approves them.

Exit criteria:

- Focused CLI smoke tests cover representative text output.
- JSON output remains unchanged for existing schemas.
- Full regression suite passes.

Completion note:

- `harness home` text output now uses section headings for project state, summary, task states, recent runs, next actions, and safety.
- `harness quickstart agent` text output now uses section headings for project state, steps, and safety.
- Compact command docstrings include examples for both commands.
- JSON output schemas and payloads are unchanged.
- No interactive prompts, TUI dependency, command palette, execution behavior, backend preflight, Docker-from-queue, shell access, hosted fallback, paid fallback, or OpenAI API usage was added.

## Track 3: Interactive TUI or Command Palette Decision Gate

Status: gated.

Goal: decide whether a true interactive UX is worth adding after packaging and lightweight CLI polish.

Decision questions:

- Should the product stay Typer/Rich-first or add a TUI dependency such as Textual?
- What is the first interactive surface: dashboard, command palette, task queue browser, lease/run monitor, or guided agent authoring?
- How should interactive commands preserve explicit operator confirmation before mutating state?
- How should the TUI behave in non-TTY, CI, and remote terminal environments?
- How will JSON/headless CLI surfaces remain first-class?

Required decision artifacts before implementation:

- Dependency decision and fallback behavior.
- Safety model for interactive actions.
- Test strategy for terminal UI behavior.
- Explicit non-goals for execution, daemon scheduling, backend preflight, Docker, shell, hosted/paid fallback, and provider usage.

Non-goals:

- No TUI implementation in the packaging/distribution track.
- No command palette implementation before a decision-complete plan.
- No hidden execution or automatic task creation from interactive flows.

## Test Plan

For Track 1 implementation:

- `python -m build` or the repository-approved equivalent.
- Install the generated wheel into a temporary virtual environment.
- Run installed CLI smoke commands:
  - `harness --help`;
  - `harness home --project <tmp> --output json`;
  - `harness specs --output json`;
  - `harness quickstart agent --project <tmp> --output json`.
- `pytest -q tests/test_cli_smoke.py tests/test_spec_loader_v0_2.py`.
- `pytest -q`.
- `git diff --check`.
- Forbidden target check for `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, and `secrets/`.

## Assumptions

- v1.0 MVP and v1.1 CLI UX are complete.
- Packaging/distribution polish is the next implementation target.
- Small CLI refinements come after packaging.
- Interactive TUI work requires a separate decision gate.
- No new execution capability is authorized by this plan.
