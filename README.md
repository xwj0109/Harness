# Agent Harness

Local-first custom agent harness.

## Project Foundations And Development Principles

Harness is a local-first, agentic coding and operator harness for real development work. Its product direction is inspired by the fast operator flow of tools such as Codex CLI, OpenCode, and Pi, but Harness should not clone any single system. Its differentiator is a local-first control plane with explicit policy, durable evidence, bounded execution, and inspectable recovery paths.

The primary product experience should be session-first and task-completing: understand the user goal, gather relevant context, plan when useful, execute through governed tools and adapters, verify the result, persist evidence, and return a concise final outcome.

The future development direction is autonomous within pre-authorized policy scopes by product default. Harness should not rely on repeated runtime confirmation prompts as the normal safety mechanism. Human control should move into configuration, static policy, scoped credentials, capability grants, budgets, leases, runtime controls, and audit review. During normal operation, Harness should either proceed automatically within policy or fail closed with durable evidence.

This does not mean models, agents, memories, sessions, prompts, artifacts, or generated text can grant themselves new authority. Authority comes only from static policy, explicit local configuration, scoped runtime capability records, and trusted Harness code paths.

Harness should preserve and extend its four security planes:

- Policy and authority boundaries.
- Runtime controls, leases, budgets, and breakers.
- Sandbox, workspace, and evidence boundaries.
- Context, provenance, integrity, and detection.

Development should prioritize working loops that are inspectable and resumable: session timelines, append-only events, immutable artifacts, linked tasks and runs, test evidence, summaries, policy decisions, and recovery paths.

Current implemented behavior remains documented in [SECURITY.md](SECURITY.md), [docs/operator_guide.md](docs/operator_guide.md), and [docs/command_catalog.md](docs/command_catalog.md). This section is the north-star product principle for future development, not a claim that all autonomous behavior already exists.

### Development Defaults

- Default to local-first execution and local evidence.
- Default to autonomous completion for policy-allowed work.
- Default to fail-closed for unknown, ambiguous, destructive, secret-touching, networked, hosted, authority-expanding, or irreversible behavior.
- Prefer sandboxing, leases, budgets, manifests, typed capabilities, and deterministic policy over ad hoc prompts.
- Preserve no hidden provider fallback, no paid fallback, no secret exposure, and no ambient shell, network, or tool authority.
- Make every user-visible action traceable to persisted session, run, task, artifact, or policy evidence.
- Build UX around fast agentic flow, not command-catalog ceremony, while keeping the control plane auditable.

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
- Foreground Codex-style prompt execution through `harness "prompt"`, with direct workspace edits, Codex CLI workspace-write sandboxing, run artifacts, and final CLI reports.
- Unified `harness` app with passive dashboard context, deterministic chat actions, first-class summary/planning/coding-fix templates, in-memory transcript/progress, and `--plain` fallback when no prompt is supplied.
- TUI command-palette and right-panel guidance that reflects the registered adapter set without executing commands.
- Read-only Capability Catalog over registered adapters through `harness capabilities list|inspect`.
- Explicit local memory notes through `harness memory save-note|list|inspect|forget`, with scoped records and redaction state.
- Read-only orchestration progress through `harness progress --objective <id>` and matching chat/TUI progress renderers.

OpenAI API usage, paid API fallback, hosted fallback, generic shell execution, autonomous workflows, MCP/A2A, browser/email/calendar integrations, external channels, third-party marketplace execution, broker actions, live trading, order placement, and active repo write automation remain outside the v1 MVP scope.

Spec and agent lifecycle surfaces are inspection/control-plane commands. They do not execute agents, preflight backends, create runs, schedule work, or authorize tools. Bounded execution happens only through active leases and registered adapters. Chat is an operator surface over those same control-plane operations; it does not call Codex, Docker, shell, providers, or model backends directly.

The primary foreground coding loop is:

```bash
harness "fix the failing tests" --project .
harness "add a CLI flag and update tests" --project . --model gpt-5.5 --reasoning-effort medium
```

This runs Codex CLI in the active project workspace with `workspace-write` sandboxing and writes a Harness run report under `.harness/runs/<run_id>/`. The direct foreground mode does not use Harness apply-back approval because edits happen in the active workspace; use `harness run "prompt" --task-type codex_code_edit` when you want the safer isolated-workspace review and apply-back flow.

The app and chat surfaces are still available when no prompt is supplied:

```bash
harness --project .
harness --project . --plain --codex-like
```

Inside the chat prompt, requests such as `summarize this repo`, `plan how to improve the CLI`, `fix the failing test with codex`, `show progress`, `show capabilities`, `show recent runs`, `review the last result`, `continue`, and `stop` route to explicit Harness actions. Chat first shows the interpreted intent, proposed action, equivalent CLI commands, safety boundary, required approvals, and confirmation prompt. Confirmed work still goes through objective/task records, daemon run-once leases, registered adapter dispatch, artifacts/events/manifests/progress, and an evidence summary with next inspection commands.

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
