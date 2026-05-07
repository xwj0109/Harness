# Agent Harness Next Steps

## Current Phase

v0.1 hardening is complete. The repository now has explicit run modes, backend descriptors, run manifests, stable JSON inspection output, `SECURITY.md`, non-mutating `harness doctor`, and golden v0.1 evidence tests.

v0.2.0 release hygiene is complete. v0.3 manual task queue hardening is complete. v0.3.5 control-plane stabilization is complete. v0.4 local daemon scheduler-readiness is complete.

The complete v0.2.0 execution plan is tracked in [v0_2_0_plan.md](v0_2_0_plan.md).
The v0.3 queue-hardening plan is tracked in [v0_3_task_queue_hardening_plan.md](v0_3_task_queue_hardening_plan.md).
The v0.3.5 stabilization plan is tracked in [v0_3_5_control_plane_stabilization_plan.md](v0_3_5_control_plane_stabilization_plan.md).
The v0.4 local daemon plan is tracked in [v0_4_local_daemon_plan.md](v0_4_local_daemon_plan.md).
The v0.4.5 execution adapter decision gate is tracked in [v0_4_5_minimal_execution_adapter_decision_plan.md](v0_4_5_minimal_execution_adapter_decision_plan.md).

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

## Immediate Next Planning Target

The next planning target is the v0.4.5 minimal execution adapter decision gate in [v0_4_5_minimal_execution_adapter_decision_plan.md](v0_4_5_minimal_execution_adapter_decision_plan.md). The default remains no task execution unless a separate, decision-complete implementation plan explicitly authorizes a tiny adapter. Any future execution work must preserve the same safety boundary until explicitly changed: no backend calls, Docker, run artifact creation, unmanaged background work, hosted fallback, or paid fallback unless the approved adapter contract explicitly covers and tests that behavior.

## Later Work

After the v0.4 scheduler-readiness checkpoint, plan any execution adapter as a separate milestone with explicit policy, approval, sandbox, artifact, trace, idempotency, and recovery contracts. Defer autonomous background behavior, broker actions, external-message automation, MCP/A2A adapters, hosted fallback, paid fallback, and generic shell access until a later milestone explicitly authorizes them.

## Working Defaults

- Keep changes small, typed, and tested.
- Preserve local/private data-boundary safeguards.
- Keep Codex modeled as a supervised external agent backend, not a raw model provider.
- Do not add paid API fallback, hosted fallback, OpenAI API usage, or automatic execution behavior.
