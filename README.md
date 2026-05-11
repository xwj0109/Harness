# Agent Harness

Local-first custom agent harness.

The v1.8 release is **Local Agent App Readiness**: local infrastructure, declarative agent structure, manual queue control, evidence inspection, local daemon control-plane readiness, registered execution dispatch, a unified chat/TUI operator app, bounded registered adapters, read-only operator TUI polish, and OpenClaw-style local app surfaces without broadening execution authority:

- CLI scaffolding.
- `.harness/` project state.
- Config loading.
- SQLite persistence.
- Run artifact generation.
- Backend metadata/capability schemas.
- Local read-only tools.
- Path traversal protection.
- Secret-path blocking and secret scanner primitives.
- Declarative model profiles, tool policies, memory scopes, agents, and workbenches.
- Explicit JSON/YAML custom spec bundle validation.
- Normalized spec export, registry diff, and effective policy preview.
- Custom agent bundle scaffold, validation, and preview.
- Project-local imported agent registry with inspect, preview, drift, and remove.
- Manual durable objectives and task queue.
- Runtime policy, manifest, artifact, compare/baseline, eval, and trace evidence.
- Local daemon control-plane commands.
- Registered execution adapter dispatcher and `daemon adapters`.
- Explicit `dry_run/phase_1a_test` evidence adapter.
- Explicit `read_only_summary/read_only_repo_summary` lease adapter.
- Explicit `codex_isolated_edit/codex_code_edit` adapter with hosted-boundary approval and isolated apply-back review.
- Explicit `repo_planning/repo_planning` adapter with hosted-boundary approval and Codex read-only sandbox planning.
- Unified `harness` app with passive dashboard context, deterministic chat actions, first-class summary/planning/coding-fix templates, in-memory transcript/progress, and `--plain` fallback.
- TUI command-palette and right-panel guidance that reflects the registered adapter set without executing commands.
- Read-only Capability Catalog over registered adapters through `harness capabilities list|inspect`.
- Explicit local memory notes through `harness memory save-note|list|inspect|forget`, with scoped records and redaction state.
- Read-only orchestration progress through `harness progress --objective <id>` and matching chat/TUI progress renderers.

OpenAI API usage, paid API fallback, hosted fallback, generic shell execution, autonomous workflows, MCP/A2A, browser/email/calendar integrations, external channels, third-party marketplace execution, broker actions, live trading, order placement, and active repo write automation remain outside the v1 MVP scope.

Spec and agent lifecycle surfaces are inspection/control-plane commands. They do not execute agents, preflight backends, create runs, schedule work, or authorize tools. Bounded execution happens only through active leases and registered adapters. Chat is an operator surface over those same control-plane operations; it does not call Codex, Docker, shell, providers, or model backends directly.

The primary operator loop is:

```bash
harness --project .
harness --project . --plain --codex-like
```

Inside the prompt, requests such as `summarize this repo`, `plan how to improve the CLI`, `fix the failing test with codex`, `show progress`, `show capabilities`, `show recent runs`, `review the last result`, `continue`, and `stop` route to explicit Harness actions. Chat first shows the interpreted intent, proposed action, equivalent CLI commands, safety boundary, required approvals, and confirmation prompt. Confirmed work still goes through objective/task records, daemon run-once leases, registered adapter dispatch, artifacts/events/manifests/progress, and an evidence summary with next inspection commands.

## Repository Layout

```text
.
├── README.md
├── SECURITY.md
├── docs/
│   ├── operator_guide.md
│   ├── command_catalog.md
│   └── smoke_checklist.md
├── src/
│   └── harness/
├── tests/
├── pyproject.toml
└── .gitignore
```

## Operator Docs

Current operator-facing flows are documented in:

- [docs/operator_guide.md](docs/operator_guide.md)
- [docs/command_catalog.md](docs/command_catalog.md)
- [docs/smoke_checklist.md](docs/smoke_checklist.md)

Security boundaries and threat-model notes are documented in:

- [SECURITY.md](SECURITY.md)

## Install and Verify

For local development, install the package in editable mode:

```bash
python3 -m pip install -e ".[dev]"
harness --help
```

For a clean local wheel smoke, build and install from a temporary wheelhouse:

```bash
rm -rf /tmp/harness-wheel /tmp/harness-install
python3 -m pip wheel --no-deps --no-build-isolation -w /tmp/harness-wheel .
python3 -m venv --system-site-packages /tmp/harness-install
/tmp/harness-install/bin/python -m pip install --no-deps /tmp/harness-wheel/agent_harness-*.whl
/tmp/harness-install/bin/harness --help
/tmp/harness-install/bin/harness --project /tmp/harness-project --output json
/tmp/harness-install/bin/harness specs --output json
/tmp/harness-install/bin/harness home --project /tmp/harness-project --output json
/tmp/harness-install/bin/harness quickstart agent --project /tmp/harness-project --output json
/tmp/harness-install/bin/harness doctor --release --project /tmp/harness-project --output json
```

The wheel must include packaged built-in YAML specs under `harness/builtin_specs/`. The install smoke is local-only and does not preflight backends, call providers, run Docker, create tasks, acquire leases, or execute adapters.

## Planning Docs

The repo-local planning files have been retired. Current operator behavior is documented in the operator docs above.
