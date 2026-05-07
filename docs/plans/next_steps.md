# Agent Harness Next Steps

## Current Phase

v0.1 hardening is complete. The repository now has explicit run modes, backend descriptors, run manifests, stable JSON inspection output, `SECURITY.md`, non-mutating `harness doctor`, and golden v0.1 evidence tests.

v0.2.0 release hygiene is complete. v0.3 manual task queue hardening is complete. The current slice is v0.3.5 control-plane stabilization.

The complete v0.2.0 execution plan is tracked in [v0_2_0_plan.md](v0_2_0_plan.md).
The v0.3 queue-hardening plan is tracked in [v0_3_task_queue_hardening_plan.md](v0_3_task_queue_hardening_plan.md).
The v0.3.5 stabilization plan is tracked in [v0_3_5_control_plane_stabilization_plan.md](v0_3_5_control_plane_stabilization_plan.md).

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

The v0.2 components are declarative and read-only. The v0.3 queue components write only initialized harness persistence through the runtime. None of these components execute agents, preflight backends, run Docker from task commands, start schedulers, or schedule background work.

## Immediate v0.3.5 Slice

Stabilize the control-plane evidence that future daemon work will rely on:

Implementation queue:

1. Add typed `PolicyLevel` and `EffectivePolicy`.
2. Upgrade run manifests compatibly toward `harness.manifest/v1.1`.
3. Add `policy explain` JSON output without backend preflight or secret inspection.
4. Add artifact evidence, tool descriptors, compare/baseline, safety-smoke evals, and trace export in later v0.3.5 slices.
5. Run full regression tests after each stabilization slice.

## Later Work

After v0.3.5 exit criteria pass, plan v0.4 local daemon work as a scheduler over durable task state. Defer autonomous background behavior, broker actions, external-message automation, MCP/A2A adapters, hosted fallback, paid fallback, and generic shell access until a later milestone explicitly authorizes them.

## Working Defaults

- Keep changes small, typed, and tested.
- Preserve local/private data-boundary safeguards.
- Keep Codex modeled as a supervised external agent backend, not a raw model provider.
- Do not add paid API fallback, hosted fallback, OpenAI API usage, or automatic execution behavior.
