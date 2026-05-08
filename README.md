# Agent Harness

Local-first custom agent harness MVP.

The v1.0 MVP includes local infrastructure, declarative agent structure, manual queue control, evidence inspection, local daemon control-plane readiness, and one bounded read-only execution adapter:

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
- Explicit `read_only_summary/read_only_repo_summary` lease adapter.

Paid API execution, hosted fallback, generic shell execution, autonomous workflows, MCP/A2A, browser/email/calendar integrations, broker actions, live trading, order placement, and active repo write automation remain outside the v1 MVP scope.

Spec and agent lifecycle surfaces are inspection/control-plane commands. They do not execute agents, preflight backends, create runs, schedule work, or authorize tools. The only bounded real execution path in the MVP is the explicit read-only daemon lease adapter.

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
/tmp/harness-install/bin/harness specs --output json
/tmp/harness-install/bin/harness home --project /tmp/harness-project --output json
/tmp/harness-install/bin/harness quickstart agent --project /tmp/harness-project --output json
```

The wheel must include packaged built-in YAML specs under `harness/builtin_specs/`. The install smoke is local-only and does not preflight backends, call providers, run Docker, create tasks, acquire leases, or execute adapters.

## Planning Docs

Repo-local planning references are tracked in:

- [docs/plans/agent_harness_master_plan.md](docs/plans/agent_harness_master_plan.md)
- [docs/plans/next_steps.md](docs/plans/next_steps.md)
- [docs/plans/v0_1_hardening_plan.md](docs/plans/v0_1_hardening_plan.md)
- [docs/plans/v0_2_0_plan.md](docs/plans/v0_2_0_plan.md)
