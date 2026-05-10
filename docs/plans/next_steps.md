# Agent Harness Next Steps

## Current Phase

The v1.5 release-readiness pass is current. The repository now has local infrastructure, declarative agent structure, manual queue control, evidence inspection, local daemon control-plane readiness, registered execution dispatch, bounded read-only and Codex isolated adapters, and the unified chat/TUI operator app.

Historical milestone plans were removed after MVP cleanup. Keep this file as the current planning snapshot; use [agent_harness_master_plan.md](agent_harness_master_plan.md) only as a retained roadmap reference, not as blanket implementation approval.

## Completed v0.2 Foundations

The first v0.2 schema and registry foundations are in place:

- `ModelProfile`.
- `ToolPolicy`.
- `MemoryScope`.
- `AgentSpec`.
- `WorkbenchSpec`.
- `SpecRegistry`.
- `builtin_spec_registry()`.
- Built-in profiles: `local_reasoning`, `codex_supervised`.
- Built-in agents: `repo_inspector`, `code_editor`, `test_runner`, `quant_researcher`, `job_researcher`.
- Built-in workbenches: `coding`, `quant`, `personal`.
- Read-only custom bundle validation with required `schema_version: harness.spec_bundle/v1`.
- Centralized explicit-path custom bundle guard.
- Registry mapping key and contained id consistency checks.
- Memory-scope hard-forbidden path invariants.
- Model-profile backend compatibility invariants.
- Tool-policy safety invariants.
- Workbench forbidden-action invariants.
- Normalized spec export for built-in and custom registries.
- Registry diff for built-in versus custom registries.
- Effective policy preview for built-in and custom registries.
- Operator documentation and smoke checklist updates.
- Full regression suite pass after documentation updates.
- Version bump to `0.2.0`.
- Final restricted-path worktree review.
- Manual v0.3 task queue persistence and CLI.
- Durable objectives, task dependencies, transition evidence, cancel/retry, graph output, attempts, leases, and select-and-lease `run-next`.
- Runtime EffectivePolicy, manifest v1.1 evidence, artifact checksums, tool capability descriptors, compare/baseline, safety-smoke evals, and trace export.
- Version bump to `0.3.5`.
- v0.4 daemon control-plane persistence, scheduler tick, heartbeat/status/stop, lease renewal, expired lease recovery, approval/policy pause evidence, and daemon status pause reasons.
- Version bump to `0.4.0`.
- v0.4.5 dry-run lease-to-run contract, read-only lease inspection, dry-run recovery reconciliation, and dry-run smoke documentation.
- Version bump to `0.4.5`.
- v0.5 Slice 1 read-only summary lease adapter for `read_only_summary/read_only_repo_summary`.
- Version bump to `0.5.0`.
- v0.5.1 read-only adapter hardening tests for failure, recovery, inspection, and unsafe metadata/backend gates.
- v0.6 Quant Workbench declarative foundation is complete.
- v0.6 packaged hierarchical built-in specs are complete.
- v0.6 Slice 2 agent-group inheritance is complete.
- v0.6.3 Agent Structure MVP is complete.
- v0.7 Agent Authoring MVP is complete.
- v0.7.1 Agent Authoring Hardening is complete.
- v0.8 Project-Local Agent Registry is complete.
- v0.9 Agent Lifecycle and Preview is complete.
- v1.0 MVP Closure is complete.
- Version bump to `1.0.0`.
- v1.1 CLI/TUI UX release is complete, including post-MVP polish, copy-only TUI command discovery, and TUI usability polish.
- Version bump to `1.1.0`.
- v1.5 release-readiness stabilization reconciles package/docs versioning, removes direct model-backed chat calls, adds deterministic next-step guidance, improves chat recovery guidance, and adds no-preflight `doctor --release` diagnostics.
- Version bump to `1.5.0`.

The v0.2 components are declarative and read-only. The v0.3 queue components write only initialized harness persistence through the runtime. None of these components execute agents, preflight backends, run Docker from task commands, start schedulers, or schedule background work.

## Completed v0.3.5 Stabilization

The control-plane evidence that future daemon work will rely on is now implemented and verified:

1. Runtime `EffectivePolicy` and `harness policy explain`.
2. `harness.manifest/v1.1` run evidence.
3. Artifact checksum/size evidence and drift inspection.
4. Tool capability descriptors.
5. Compare/baseline evidence.
6. Safety-smoke evals and OTEL-shaped trace export.

## Completed v0.4 Scheduler Readiness

Daemon scheduler readiness is implemented: daemon control-plane persistence, `daemon run-once/status/stop`, heartbeat/event evidence, non-executing scheduler decisions, lease renewal, expired lease recovery, `daemon recover`, and daemon eligibility pause reasons.

The v0.4 scheduler-readiness checkpoint is complete. Daemon commands are local control-plane operations only: they may acquire/renew/recover leases and record daemon evidence, but they do not execute tasks, call backends, run Docker, create run artifacts, start unmanaged background work, add hosted fallback, or add paid fallback.

## Completed v0.4.5 Dry-Run Adapter Milestone

The dry-run adapter milestone is complete as dry-run evidence only: `daemon execute-dry-run` may create local `phase_1a_test` run evidence from an active lease, but it does not call backends, models, Docker, shell tools, network, hosted providers, paid providers, MCP/A2A, browser/email/calendar tools, or mutate active repo files.

## Completed v0.5 Read-Only Adapter Milestone

The read-only adapter milestone is implemented: `daemon execute-read-only` can bind an existing active daemon lease to the existing `read_only_repo_summary` runner when the task metadata is exactly `execution_adapter=read_only_summary` and `task_type=read_only_repo_summary`.

The adapter was originally intentionally narrow around the configured local-only, no-cost `local_openai_compatible` backend and existing read-only tools. The current implementation target has moved model-backed app work to supervised `codex_cli` subscription execution with hosted-boundary approval while preserving the same daemon lease and registered-adapter evidence model. It still does not authorize Docker, shell access, paid fallback, OpenAI API usage, active repo writes, MCP/A2A, browser/email/calendar tools, autonomous planning, generic task execution, or unmanaged daemon loops.

## Completed v0.5.1 Read-Only Adapter Hardening

Read-only adapter hardening added focused coverage for preflight failure, runner failure, duplicate execution, read-only recovery states, inspect-lease evidence, unresolved approvals, forbidden metadata, and unsafe backend descriptors. It does not add another execution path.

## Completed v1.1 CLI/TUI UX Release

The CLI/TUI UX release is complete and packaged as version `1.1.0`. The release kept the core execution model unchanged while improving operator usability. The ranked post-MVP UX/product order was:

1. Packaging/distribution polish first. Complete.
2. Small Typer/Rich-style CLI refinements second. Complete.
3. True interactive TUI/command palette later, behind a separate decision gate. Complete as a read-only, copy-only command discovery surface in the `1.1.0` release.

Implemented CLI-only refinements include sectioned text output for high-traffic inspect/explain commands and a grouped command catalog in [../command_catalog.md](../command_catalog.md). JSON contracts remain unchanged.

Implemented TUI refinements include Textual app startup, a chat-style slash-command discovery surface, dashboard context sections, local in-memory search over loaded dashboard and command metadata, keyboard/navigation hints, no-match status, static generated terminal pixel art, and explicit `tui-home set-image` static-art import. These changes remain read-only except for the explicit tracked static-art import command and do not add command execution, providers, hosted fallback, paid fallback, OpenAI API usage, Docker execution, shell access, or persisted TUI preferences.

## Completed v1.2 Read-Only TUI Refinements

The v1.2 read-only TUI refinement implementation is complete. It adds session-local section collapse and palette-only focus for command discovery while preserving the existing read-only TUI boundary: no command execution, subprocess spawning, clipboard writes, provider calls, backend preflight, Docker use, `.harness/` mutation, or persisted preferences.

## Completed v1.2-v1.5 Execution Layer Milestones

The staged execution layer is complete:

- v1.2 Registered Execution Dispatcher: static adapter descriptors, generic `daemon execute` dispatch, `daemon adapters`, generic inspect-lease eligibility, and fail-closed pre-run rejection events.
- v1.3 Execution Lifecycle Hardening: shared active-lease validation, duplicate-run rejection, adapter-owned run binding, terminal task/attempt/lease finish evidence, and sanitized adapter rejection events.
- v1.4 Codex Runner Binding: `CodexCodeEditRunner.run_existing(...)` lets the execution service own lease/attempt/run binding while the runner owns isolation, Codex execution, diff inspection, and apply-back mechanics.
- v1.5 Codex Isolated Adapter: `codex_isolated_edit/codex_code_edit` dispatch validates hosted-boundary approval before run creation, uses configured `codex_cli`, preserves deny-by-default apply-back, and records security-relevant applied/denied/no-change/failure decisions.

## Immediate Next Planning Target

The current implementation target is release readiness for the unified Harness app described in [chat_cli_experience_plan.md](chat_cli_experience_plan.md). The chat layer sits above the existing control plane and registered execution dispatcher. It is an operator surface, not a separate execution authority.

Current chat implementation status:

- Milestone 1 Chat Shell Foundation: implemented through root `harness`, `harness --output json`, `harness --plain`, slash commands, and no backend/Docker preflight.
- Milestone 2 Read-Only Chat Context: implemented with deterministic local state inspection, selected task/run/artifact details, registered adapter listing, and rule-based read-only aliases.
- Milestone 3 Task Drafting And Preview: implemented for `dry_run/phase_1a_test`, `read_only_summary/read_only_repo_summary`, and `codex_isolated_edit/codex_code_edit` with explicit confirmation.
- Milestone 4 Lease And Dispatch Flow: implemented through the existing daemon run-once lease path and registered `daemon execute` dispatcher only.
- Milestone 5 Read-Only Summary Chat Flow: implemented through the `read_only_summary` adapter only; chat does not call Codex directly, and the adapter now uses supervised Codex CLI subscription execution with hosted-boundary approval.
- Milestone 6 Codex Isolated Edit Chat Flow: implemented as queued `codex_isolated_edit` dispatch through the registered adapter only, preserving hosted-boundary approval and apply-back separation.
- Milestone 7 Conversational Result Memory: implemented as session-local references only, cleared by `/reset`, with no history file.
- Milestone 8 Streaming And Progress Rendering: implemented as local progress summaries from chat/session and harness execution evidence, with JSON mode kept stable.
- Milestone 9 Apply-Back Review UX: implemented as inspected-artifact review and explicit safe deny/keep handling. Chat does not parse patches from text or perform apply-back approval outside the existing approval provider path.
- Milestone 10 Polished OpenCode-Style UX: implemented as compact line-oriented prompts, status blocks, deterministic next-step recommendations, refusal/recovery messages, equivalent command previews, and in-memory transcript/progress state.
- Chat-first multi-agent orchestration: implemented as selectable built-in orchestrators, visible Codex-backed objective/task graph drafts, and a bounded foreground one-run approval loop over daemon run-once plus registered `codex_isolated_edit` dispatch.
- Unified application surface: implemented as bare `harness`, combining passive dashboard/TUI context with the real chat/orchestration engine. Legacy chat and dashboard entrypoints remain hidden compatibility aliases only.

Recommended decision options after release verification:

- Package and publish the coherent `1.5.0` local release after regression and wheel smokes pass.
- Next bounded execution adapter planning, only if policy, approval, sandbox, artifact, trace, idempotency, and recovery contracts are decision-complete.
- Additional read-only TUI refinements, still with no command execution or persisted preferences.

Do not add another execution adapter until a separate decision-complete plan authorizes it. `repo_planning`, `simple_code_edit`, Docker execution, shell access, hosted fallback, paid fallback, OpenAI API usage, MCP/A2A, browser/email/calendar tools, broker actions, live trading, order placement, and active repo writes outside the explicit Codex apply-back path remain unauthorized.

## Later Work

After the v0.5 read-only adapter checkpoint, plan any additional execution adapter as a separate milestone with explicit policy, approval, sandbox, artifact, trace, idempotency, and recovery contracts. Defer autonomous background behavior, broker actions, external-message automation, MCP/A2A adapters, hosted fallback, paid fallback, and generic shell access until a later milestone explicitly authorizes them.

## Working Defaults

- Keep changes small, typed, and tested.
- Preserve local/private data-boundary safeguards.
- Keep Codex modeled as a supervised external agent backend, not a raw model provider.
- Do not add paid API fallback, hosted fallback, OpenAI API usage, or automatic execution behavior.
