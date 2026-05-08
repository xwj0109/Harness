# Agent Harness Next Steps

## Current Phase

v0.1 hardening is complete. The repository now has explicit run modes, backend descriptors, run manifests, stable JSON inspection output, `SECURITY.md`, non-mutating `harness doctor`, and golden v0.1 evidence tests.

v0.2.0 release hygiene is complete. v0.3 manual task queue hardening is complete. v0.3.5 control-plane stabilization is complete. v0.4 local daemon scheduler-readiness is complete. v0.4.5 dry-run adapter milestone is complete. v0.5 read-only execution adapter milestone is complete. v0.5.1 read-only adapter hardening is complete. v1.0 MVP closure is complete.

The complete v0.2.0 execution plan is tracked in [v0_2_0_plan.md](v0_2_0_plan.md).
The v0.3 queue-hardening plan is tracked in [v0_3_task_queue_hardening_plan.md](v0_3_task_queue_hardening_plan.md).
The v0.3.5 stabilization plan is tracked in [v0_3_5_control_plane_stabilization_plan.md](v0_3_5_control_plane_stabilization_plan.md).
The v0.4 local daemon plan is tracked in [v0_4_local_daemon_plan.md](v0_4_local_daemon_plan.md).
The v0.4.5 execution adapter decision gate is tracked in [v0_4_5_minimal_execution_adapter_decision_plan.md](v0_4_5_minimal_execution_adapter_decision_plan.md).
The v0.5 read-only execution adapter plan is tracked in [v0_5_read_only_execution_adapter_plan.md](v0_5_read_only_execution_adapter_plan.md).
The v0.5.1 read-only hardening plan is tracked in [v0_5_1_read_only_adapter_hardening_plan.md](v0_5_1_read_only_adapter_hardening_plan.md).
The v0.6 Quant Workbench plan is tracked in [v0_6_quant_workbench_plan.md](v0_6_quant_workbench_plan.md).
The v0.6 Slice 2 agent-group inheritance plan is tracked in [v0_6_2_agent_group_inheritance_plan.md](v0_6_2_agent_group_inheritance_plan.md).
The v0.6.3 Agent Structure MVP plan is tracked in [v0_6_3_agent_structure_mvp_plan.md](v0_6_3_agent_structure_mvp_plan.md).
The v0.7 Agent Authoring MVP plan is tracked in [v0_7_agent_authoring_mvp_plan.md](v0_7_agent_authoring_mvp_plan.md).
The v0.7.1 Agent Authoring Hardening plan is tracked in [v0_7_1_agent_authoring_hardening_plan.md](v0_7_1_agent_authoring_hardening_plan.md).
The v0.8 Project-Local Agent Registry plan is tracked in [v0_8_project_agent_registry_plan.md](v0_8_project_agent_registry_plan.md).
The v0.9 Agent Lifecycle and Preview plan is tracked in [v0_9_agent_lifecycle_and_preview_plan.md](v0_9_agent_lifecycle_and_preview_plan.md).
The v1.0 MVP Closure plan is tracked in [v1_0_mvp_closure_plan.md](v1_0_mvp_closure_plan.md).
The v1.1 CLI UX plan is tracked in [v1_1_cli_ux_plan.md](v1_1_cli_ux_plan.md).

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

v0.4 local daemon planning is captured in [v0_4_local_daemon_plan.md](v0_4_local_daemon_plan.md). Slice 1 is implemented: daemon control-plane persistence, `daemon run-once/status/stop`, heartbeat/event evidence, and non-executing scheduler decisions. Slice 2 is implemented: lease renewal, expired lease recovery, and `daemon recover`. Slice 3 is implemented: daemon eligibility now pauses approval-required and daemon-forbidden tasks with inspectable pause reasons.

The v0.4 scheduler-readiness checkpoint is complete. Daemon commands are local control-plane operations only: they may acquire/renew/recover leases and record daemon evidence, but they do not execute tasks, call backends, run Docker, create run artifacts, start unmanaged background work, add hosted fallback, or add paid fallback.

## Completed v0.4.5 Dry-Run Adapter Milestone

The v0.4.5 dry-run adapter milestone is tracked in [v0_4_5_minimal_execution_adapter_decision_plan.md](v0_4_5_minimal_execution_adapter_decision_plan.md). Slice 1 proves a dry-run lease-to-run contract, and Slice 2 hardens dry-run lease inspection and recovery. The milestone is complete as dry-run evidence only: `daemon execute-dry-run` may create local `phase_1a_test` run evidence from an active lease, but it does not call backends, models, Docker, shell tools, network, hosted providers, paid providers, MCP/A2A, browser/email/calendar tools, or mutate active repo files.

## Completed v0.5 Read-Only Adapter Milestone

The v0.5 read-only adapter milestone is tracked in [v0_5_read_only_execution_adapter_plan.md](v0_5_read_only_execution_adapter_plan.md). Slice 1 is implemented: `daemon execute-read-only` can bind an existing active daemon lease to the existing `read_only_repo_summary` runner when the task metadata is exactly `execution_adapter=read_only_summary` and `task_type=read_only_repo_summary`.

The adapter is intentionally narrow. It uses only the configured local-only, no-cost `local_openai_compatible` backend and existing read-only tools. It does not authorize Codex execution, Docker, shell access, hosted fallback, paid fallback, OpenAI API usage, active repo writes, MCP/A2A, browser/email/calendar tools, autonomous planning, generic task execution, or unmanaged daemon loops.

## Completed v0.5.1 Read-Only Adapter Hardening

The v0.5.1 hardening plan is tracked in [v0_5_1_read_only_adapter_hardening_plan.md](v0_5_1_read_only_adapter_hardening_plan.md). It adds focused coverage for read-only adapter preflight failure, runner failure, duplicate execution, read-only recovery states, inspect-lease evidence, unresolved approvals, forbidden metadata, and unsafe backend descriptors. It does not add another execution path.

## Immediate Next Planning Target

The immediate next target is v1.1 CLI UX, tracked in [v1_1_cli_ux_plan.md](v1_1_cli_ux_plan.md). It should add a human-friendly dashboard and guided command composition for the existing MVP surface without changing execution behavior.

Do not add another execution adapter until a separate decision-complete plan authorizes it. `repo_planning`, `simple_code_edit`, `codex_code_edit`, Docker execution, shell access, hosted fallback, paid fallback, OpenAI API usage, MCP/A2A, browser/email/calendar tools, broker actions, live trading, order placement, and active repo writes remain unauthorized.

## Later Work

After the v0.5 read-only adapter checkpoint, plan any additional execution adapter as a separate milestone with explicit policy, approval, sandbox, artifact, trace, idempotency, and recovery contracts. Defer autonomous background behavior, broker actions, external-message automation, MCP/A2A adapters, hosted fallback, paid fallback, and generic shell access until a later milestone explicitly authorizes them.

## Working Defaults

- Keep changes small, typed, and tested.
- Preserve local/private data-boundary safeguards.
- Keep Codex modeled as a supervised external agent backend, not a raw model provider.
- Do not add paid API fallback, hosted fallback, OpenAI API usage, or automatic execution behavior.
