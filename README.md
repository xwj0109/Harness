# Agent Harness

Local-first custom agent harness.

The v1.5 release includes local infrastructure, declarative agent structure, manual queue control, evidence inspection, local daemon control-plane readiness, registered execution dispatch, a unified chat/TUI operator app, and bounded registered adapters:

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
- Unified `harness` app with passive dashboard context, deterministic chat actions, in-memory transcript/progress, and `--plain` fallback.

Paid API execution, hosted fallback, generic shell execution, autonomous workflows, MCP/A2A, browser/email/calendar integrations, broker actions, live trading, order placement, and active repo write automation remain outside the v1 MVP scope.

Spec and agent lifecycle surfaces are inspection/control-plane commands. They do not execute agents, preflight backends, create runs, schedule work, or authorize tools. Bounded execution happens only through active leases and registered adapters. Chat is an operator surface over those same control-plane operations; it does not call Codex, Docker, shell, providers, or model backends directly.

## Repository Layout

```text
.
├── README.md
├── AGENTS.md
├── SECURITY.md
├── docs/
│   ├── operator_guide.md
│   ├── smoke_checklist.md
│   └── plans/
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

Repo-local planning references are tracked in:

- [docs/plans/agent_harness_master_plan.md](docs/plans/agent_harness_master_plan.md)
- [docs/plans/next_steps.md](docs/plans/next_steps.md)

Completed milestone plans were removed after the first MVP cleanup. Current operator behavior is documented in the operator docs above.
