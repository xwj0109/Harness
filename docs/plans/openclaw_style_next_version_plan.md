# Harness v1.8 OpenClaw-Style Local App Plan

Harness v1.8 is a local-first supervised agent app, not an OpenClaw clone and not a generic automation platform.

The release target is **Local Agent App Readiness**: make the existing Harness operator app feel like a complete local agent workspace while preserving the current control-plane authority, approval gates, evidence model, and registered adapter boundary.

This plan uses the useful OpenClaw-shaped product concepts of agents, memory, capabilities, sandboxing, and an always-available operator surface. It explicitly does not adopt OpenClaw-style channel breadth, marketplace execution, shell/browser access, or unmanaged autonomy for this release.

## Reference Framing

OpenClaw-style concepts considered for this plan:

- OpenClaw presents an always-available self-hosted agent platform with many channels, many skills, model flexibility, privacy positioning, and sandboxed permissions: <https://openclawdoc.com/>.
- OpenClaw agents are described as stateful entities with model, memory, tool, and channel layers: <https://openclawdoc.com:8444/docs/agents/overview/>.
- OpenClaw skills are higher-level capabilities with manifests, inputs, outputs, configuration, lifecycle, and discoverability: <https://openclawdoc.com:8444/docs/skills/overview/>.
- OpenClaw security emphasizes least privilege, defense in depth, secure defaults, sandboxing, permissions, audit logging, network controls, rate limits, and secret masking: <https://openclawdoc.com:8444/docs/security/overview/>.

Harness v1.8 should translate those ideas into Harness-native terms:

- Channels become the local `harness` operator app only.
- Skills become registered Harness capabilities backed by existing registered adapters.
- Memory becomes explicit local artifact-backed records with scopes and redaction state.
- Autonomy becomes bounded foreground orchestration through objectives, tasks, leases, and registered dispatch.
- Sandbox and permissions remain Harness policy, approval, isolation, artifact, and evidence contracts.

## Current Baseline

The v1.7 baseline already includes the safety and execution substrate for this release:

- Unified `harness` Textual app with passive dashboard context and real chat/orchestrator prompt.
- Plain line-oriented fallback via `harness --plain`.
- Stable read-only context probe via `harness --output json`.
- Declarative agents, workbenches, model profiles, tool policies, and memory scopes.
- Manual durable objectives, task queue, dependencies, attempts, leases, cancellation, retry, graph output, and transition evidence.
- Local daemon control-plane readiness with heartbeat, status, stop, lease renewal, expired lease recovery, and pause reasons.
- Registered execution dispatcher with fail-closed adapter lookup and generic `daemon execute`.
- Bounded adapters:
  - `dry_run/phase_1a_test`.
  - `read_only_summary/read_only_repo_summary`.
  - `repo_planning/repo_planning`.
  - `codex_isolated_edit/codex_code_edit`.
- Supervised Codex CLI subscription usage only through explicit registered adapters and hosted-boundary approval.
- Codex isolated edit path with isolated workspace, diff inspection, and deny-by-default apply-back.
- Run manifests, artifacts, checksums, traces, evals, compare/baseline evidence, and policy snapshots.
- TUI command palette, right-panel guidance, slash commands, next-step suggestions, and session-local UI state.

The current app is already more than a CLI wrapper. v1.8 should make it feel coherent as a local agent application without adding new execution power.

## Release Goal

v1.8.0 is **Local Agent App Readiness**.

The release succeeds when an operator can open `harness`, understand the current local agent workspace, see available Harness capabilities, manage explicit local memory, run or inspect a bounded foreground objective flow, and understand every safety gate without needing to know the lower-level command sequence first.

The release must remain compatible with existing control-plane JSON contracts and must not grant broad execution authority through app polish.

## Non-Goals

v1.8 must not add or authorize:

- OpenAI API usage or `OPENAI_API_KEY`.
- Paid API fallback.
- Hosted fallback.
- Generic shell execution.
- Browser automation.
- Email, calendar, Slack, Discord, Telegram, Teams, WhatsApp, or other external channel integrations.
- MCP or A2A adapters.
- Third-party skill marketplace install/update execution.
- Broker actions, live trading, order placement, or capital allocation.
- Automatic external message sending or application submission.
- Unmanaged background autonomy or hidden daemon loops.
- Direct active repository mutation by chat, model text, TUI, or planning output.
- Active repository writes outside the existing explicit Codex isolated apply-back approval path.
- Reading or exposing secrets.
- Planning or edit targets under `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, `secrets/`, or other secret-like paths.

## Public Interfaces

### Capability Catalog

Add a read-only Harness capability surface over existing registered adapters.

Schema: `harness.capability_catalog/v1`.

Minimum fields:

- `schema_version`.
- `generated_at`.
- `project_root`.
- `capabilities`.
- Capability item fields:
  - `id`.
  - `title`.
  - `description`.
  - `execution_adapter`.
  - `supported_task_types`.
  - `required_approvals`.
  - `data_boundary`.
  - `mutation_boundary`.
  - `sandbox_boundary`.
  - `readiness`.
  - `readiness_reasons`.
  - `safety_notes`.
  - `equivalent_commands`.

CLI:

```bash
harness capabilities list --project . --output json
harness capabilities inspect <capability_id> --project . --output json
```

Rules:

- This is a read-only alias over registered adapter descriptors and local readiness checks.
- It must not preflight Codex, local model endpoints, Docker, network, shell, browser, email, calendar, or third-party services.
- It must not create tasks, acquire leases, create runs, write artifacts, request approvals, or execute adapters.
- Capability descriptors are documentation and validation metadata, not permission grants.

### Artifact-Backed Memory

Add explicit local memory records for operator-visible continuity.

Schema: `harness.memory_record/v1`.

Minimum fields:

- `schema_version`.
- `id`.
- `scope`.
- `scope_type`: one of `workbench`, `agent`, `objective`, or `project`.
- `scope_id`.
- `source_kind`: one of `artifact`, `run`, `task`, `objective`, or `operator_note`.
- `source_id`.
- `source_artifact_id`.
- `summary`.
- `redaction_state`: one of `not_required`, `redacted`, or `blocked`.
- `sha256`.
- `size_bytes`.
- `created_at`.
- `updated_at`.
- `lineage`.

CLI:

```bash
harness memory list --project . --output json
harness memory inspect <memory_id> --project . --output json
harness memory save-note --scope project --summary "..." --project . --output json
harness memory forget <memory_id> --project . --output json
```

Rules:

- Memory is local-only.
- Memory is explicit; the app must not silently persist transcript history.
- Memory records must be scoped and inspectable.
- Operator notes are allowed only through explicit `save-note`.
- Derived memory from runs, plans, task outcomes, and artifacts must preserve lineage.
- Secret-looking content must be redacted before registration or block the memory record.
- Memory commands must reject forbidden paths and must not read `.env*`, `*.pem`, `*.key`, `*.sqlite`, `secrets/`, `.git/`, or `.harness/` except through existing approved Harness runtime APIs.
- Memory cannot grant tools, approvals, execution authority, model access, hosted boundary permission, or apply-back permission.

### Orchestration Progress

Add a stable foreground progress surface for objective/task graph execution.

Schema: `harness.orchestration_progress/v1`.

Minimum fields:

- `schema_version`.
- `objective_id`.
- `objective_title`.
- `selected_orchestrator`.
- `mode`: `draft`, `confirmed`, `leased`, `dispatching`, `blocked`, `terminal`, or `idle`.
- `tasks`.
- Task item fields:
  - `task_id`.
  - `title`.
  - `status`.
  - `execution_adapter`.
  - `task_type`.
  - `attempt_id`.
  - `lease_id`.
  - `run_id`.
  - `terminal_decision`.
  - `blocked_reasons`.
  - `next_action`.
- `active_lease_id`.
- `active_run_id`.
- `blocked_reasons`.
- `next_action`.
- `equivalent_commands`.

CLI:

```bash
harness progress --objective <objective_id> --project . --output json
```

Rules:

- Progress inspection is read-only.
- It must report the current objective/task/lease/run state without selecting work or dispatching adapters.
- It must explain blocked states in operator terms: missing approval, unsafe metadata, missing adapter, unknown adapter, dependency not satisfied, active lease, terminal task, or failed policy check.
- The TUI and chat surfaces should consume this same structure rather than duplicate ad hoc progress rendering.

## Milestone 1: Agent Home And Capability Catalog Polish

Goal: make `harness` feel like a complete local agent home without adding execution authority.

Implementation:

- Add a capability catalog builder over `list_execution_adapter_descriptors()` and existing project context.
- Add `harness capabilities list` and `harness capabilities inspect`.
- Add capability rows to the unified app home and right panel:
  - capability name.
  - supported task types.
  - required approval status.
  - safety boundary.
  - equivalent command preview.
- Add deterministic chat answers for:
  - "what can Harness do here?"
  - "show capabilities"
  - "which actions need approval?"
  - "what is safe to run next?"
- Keep command palette entries copy-only unless the user is in an existing explicit confirmation flow.

Acceptance criteria:

- Capability JSON is stable and deterministic.
- Capability catalog output matches the registered adapter set.
- Capability rendering does not preflight backends or mutate project state.
- Unknown capability inspection fails closed with a clear error.
- Text output includes equivalent CLI commands and safety notes.
- TUI/chat display does not create tasks, acquire leases, write artifacts, call providers, run Docker, or execute adapters.

## Milestone 2: Artifact-Backed Memory v1

Goal: add explicit local memory that improves app continuity while preserving the private data boundary.

Implementation:

- Add a memory record model and storage path through existing Harness persistence patterns.
- Support explicit operator notes with `memory save-note`.
- Support listing and inspecting memory records by scope.
- Support forgetting memory records by marking them forgotten or deleting their local metadata according to existing evidence conventions.
- Allow future derivation from run/task/artifact summaries, but do not auto-ingest transcripts in v1.8.
- Add redaction and secret-looking content checks before memory registration.
- Add memory summary sections to the app home when records exist.

Acceptance criteria:

- Memory commands are explicit local control-plane operations.
- Memory records include schema version, scope, source, redaction state, checksum, timestamp, and lineage.
- Forbidden path inputs are rejected.
- Secret-looking content is redacted or blocked before registration.
- Memory does not alter effective policy, tool permissions, approvals, backend routing, or adapter eligibility.
- `/reset` continues to clear session transcript/progress state without deleting explicit memory records.

## Milestone 3: Foreground Orchestration Progress And Recovery UX

Goal: make bounded foreground orchestration inspectable and recoverable from the app.

Implementation:

- Add `harness progress --objective <id>`.
- Add a progress renderer in chat and TUI based on `harness.orchestration_progress/v1`.
- Show visible graph states for draft, confirmed, leased, dispatching, blocked, terminal, and idle.
- Make blocked states actionable with exact next commands or app actions.
- Show lease/run/artifact links after each dispatch.
- Preserve the current explicit confirmation model for creating objectives/tasks and for foreground run loops.
- Keep registered dispatcher as the only execution path.

Acceptance criteria:

- Progress inspection never selects work or executes an adapter.
- Foreground orchestration continues to use `daemon run-once` leases and `daemon execute` registered dispatch.
- Missing approval, unsafe backend metadata, policy rejection, duplicate execution, failed adapter, and denied apply-back are surfaced as clear blocked or terminal states.
- The app can resume display from existing objective/task/run evidence after restart.
- Active repo mutation remains denied unless the existing explicit apply-back approval path approves the inspected diff.

## Milestone 4: Release Verification, Docs, And Packaging

Goal: ship v1.8 as a coherent local app release.

Implementation:

- Update `README.md`, `docs/operator_guide.md`, `docs/command_catalog.md`, `docs/smoke_checklist.md`, and `docs/plans/next_steps.md` only after reviewing existing local modifications.
- Document capability catalog, explicit memory, progress inspection, and the unchanged safety boundaries.
- Add smoke checklist coverage for:
  - `harness capabilities list --output json`.
  - `harness memory save-note/list/inspect/forget --output json`.
  - `harness progress --objective <id> --output json`.
  - TUI capability and progress display.
  - no-preflight app startup.
- Bump package version to `1.8.0` only after tests and documentation are complete.

Acceptance criteria:

- Existing JSON contracts remain backward compatible.
- New JSON schemas are stable and covered by tests.
- Full regression suite passes.
- Wheel smoke from `README.md` passes.
- Safety-smoke evals pass.
- Documentation clearly states v1.8 does not add hosted fallback, paid fallback, OpenAI API usage, generic shell, browser/email/calendar, external channels, MCP/A2A, third-party marketplace execution, unmanaged background autonomy, or broad active repo writes.

## Tests And Verification

Documentation checks:

- Confirm this plan is tracked under `docs/plans/`.
- Confirm no planning doc uses `.harness/`, `.git/`, `.env*`, secret-like files, SQLite files, or `secrets/` as edit targets.
- Confirm prohibited capabilities are documented as non-goals, not allowed behavior.

Capability tests:

- `capabilities list` JSON schema test.
- `capabilities inspect` known capability test.
- `capabilities inspect` unknown capability fail-closed test.
- Test that capability commands do not call Codex, local model backend preflight, Docker, shell, network, or adapter execution.

Memory tests:

- Save/list/inspect/forget happy path.
- Scope validation for workbench, agent, objective, and project scopes.
- Forbidden path rejection.
- Secret-looking content redaction or blocking.
- Memory does not affect policy resolution or adapter eligibility.
- Session reset does not delete explicit memory.

Progress tests:

- Objective with ready task.
- Objective with active lease.
- Objective with completed run.
- Objective blocked by missing approval.
- Objective blocked by dependency.
- Objective failed by adapter rejection.
- Progress command is read-only and does not create leases or runs.

TUI/chat tests:

- App startup reads capability and progress context without backend preflight.
- Chat aliases route to read-only capability/progress renderers.
- TUI displays capability rows and progress rows without command execution.
- Existing `--plain`, `--output json`, and hidden compatibility aliases remain stable.

Release checks:

```bash
python3 -m pytest
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

## Future After v1.8

Each item below requires a separate decision-complete plan before implementation:

- External channel adapters such as Slack, Discord, Telegram, email, calendar, Matrix, Signal, Teams, or web chat.
- Third-party skill/package installation, update, signing, trust, and sandbox lifecycle.
- MCP or A2A adapter support.
- Browser automation.
- Generic shell or command execution.
- Local daemon autonomy beyond bounded foreground loops.
- New execution adapters beyond the current registered set.
- Broader workbench automation for quant, personal, research, writing, or operations workflows.
- Live trading, broker integrations, external messaging, or application submission workflows.

## Working Defaults

- Keep changes small, typed, and tested.
- Preserve local/private data-boundary safeguards.
- Treat Codex as a supervised external agent backend, not a raw model provider.
- Use registered adapters and existing control-plane APIs for execution.
- Prefer explicit operator confirmation over inference.
- Prefer local evidence and stable JSON over chat-only state.
- Do not read or expose secrets.
- Do not modify `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, `secrets/`, or other secret-like files.
