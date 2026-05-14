# Autonomous Chat Runtime Implementation Plan

This document is the complete implementation roadmap for moving Harness from a chat-driven operator surface to a bounded autonomous agent runtime.

The core principle is:

```text
The LLM becomes autonomous in deciding and sequencing work, not autonomous in granting itself authority.
```

Harness should remove live human confirmation only where policy already says the action is safe, local, idempotent, bounded, and observable. Sensitive boundaries remain paused, denied, or governed by explicit pre-granted approval profiles.

## Current Assessment

The repository is already well-positioned for this roadmap.

Current repo capabilities:

- `pyproject.toml` reports `agent-harness` version `1.8.0`.
- The app already has Typer/Textual, Pydantic, YAML specs, packaged built-in specs, and the `harness` CLI entrypoint.
- The README describes a unified chat/TUI operator surface.
- Adapter dispatch is registered and policy-aware.
- Local memory notes already exist.
- Progress views, capability discovery, and explicit adapters already exist.
- Existing adapters include dry-run, read-only summary, repo planning, and Codex isolated edit.

Relevant current architecture:

- `src/harness/chat.py` models chat session state with pending drafts, pending orchestration, pending action contracts, latest task, latest lease, latest run, latest artifact, selected orchestrator, and Codex-like foreground mode.
- `src/harness/chat_tools.py` already separates read tools from gated side-effect tools.
- `src/harness/chat_model.py` supports structured `harness.tool_request/v1` model output.
- `src/harness/execution.py` is the correct execution backbone for registered adapters, task validation, leases, policy hashes, manifests, rejection evidence, and sandboxed runs.

Strategic gap:

```text
Harness currently has chat as an operator surface.
The target is chat as the autonomous agent runtime.
```

This does not mean letting the model execute arbitrary commands. It means letting the model request work, while Harness validates, authorizes, executes, records evidence, and stops at boundaries.

## Product Target

Target definition:

```text
An LLM chat runtime that autonomously plans, inspects, creates objectives/tasks,
dispatches registered adapters, reviews artifacts, continues task graphs, and
produces evidence, subject only to Harness policy, sandbox, approval profiles,
leases, idempotency, budgets, and runtime kill switches.
```

Non-target:

```text
An LLM that can bypass confirmation, write files directly, execute shell, call
arbitrary tools, self-grant approvals, mutate the active repo, or cross hosted,
paid, networked, personal-message, trading, broker, job-submission, or external
side-effect boundaries without explicit approval.
```

## Target Architecture

Add an autonomy layer between chat and action contracts:

```text
LLM chat runtime
  -> typed tool request
  -> Harness action contract
  -> autonomy policy evaluator
  -> auto-approve / pause / deny
  -> task/objective/lease/run/adapter execution
  -> evidence returned to chat
  -> LLM decides next step
```

The LLM never executes tools directly. It requests tools. Harness validates and executes.

## Safety Model

Autonomy can only narrow or consume existing authority.

It cannot:

- Broaden `EffectivePolicy`.
- Broaden sandbox permissions.
- Invent approvals.
- Extend approval expiry.
- Increase budget.
- Add adapter capability.
- Mutate forbidden paths.
- Bypass kill switches.
- Bypass adapter breakers.
- Apply isolated edits back to the active repo unless a separate policy explicitly permits it.

Every autonomous side effect must leave evidence.

## Phase Overview

Recommended PR sequence:

1. `AutonomyPolicy` and `AutonomyDecision`.
2. Action contract auto-evaluator.
3. Autonomous read-only chat loop.
4. Autonomous control-plane writes.
5. Autonomous registered adapter dispatch.
6. Autonomous objective runner.
7. Supervised-Codex autonomy profile.
8. Evidence and recovery hardening.
9. Reviewer agents and bounded multi-agent workflows.

Each phase should land independently and preserve a working manual mode.

## Phase 1: Autonomy Policy Foundation

### Goal

Add the autonomy domain model and a pure evaluator. No runtime behavior changes yet.

Target command:

```bash
harness autonomy policy inspect --project . --profile safe-local --output json
```

### In Scope

- `src/harness/autonomy.py`
- `AutonomyPolicy`
- `AutonomyDecision`
- `AutonomousApprovalRecord`
- `AutonomyBudget`
- `AutonomyScope`
- `AutonomyViolation`
- Built-in autonomy profiles
- Pure evaluator
- CLI profile inspection
- Unit tests

### Out Of Scope

- Changing chat confirmation behavior.
- Auto-executing action contracts.
- Adapter dispatch changes.
- Objective runner changes.
- Daemon changes.

### Data Model

Use existing Harness policy, risk, boundary, approval, and adapter types where they already exist. Add new types only for autonomy-specific concepts.

Target shape:

```python
class AutonomyPolicy(BaseModel):
    schema_version: Literal["harness.autonomy_policy/v1"] = "harness.autonomy_policy/v1"
    id: str
    scope: Literal["project", "workbench", "agent", "objective", "task"]
    allowed_tools: list[str]
    allowed_adapters: list[str]
    allowed_task_types: list[str]
    max_tool_calls_per_turn: int
    max_steps_per_objective: int
    max_runtime_minutes: int
    max_cost_usd: Decimal | None = None
    allowed_boundaries: list[str]
    auto_confirm_risks: list[str]
    pause_on_risks: list[str]
    forbidden_risks: list[str]
    require_evidence: bool = True
    require_idempotency: bool = True
    require_sandbox: bool = True
```

Recommended explicit budget object:

```python
class AutonomyBudget(BaseModel):
    max_model_turns: int = 20
    max_tool_calls: int = 60
    max_side_effect_actions: int = 10
    max_runtime_seconds: int = 900
    max_adapter_dispatches: int = 3
    max_new_tasks: int = 10
    max_consecutive_failures: int = 2
    max_cost_usd: Decimal | None = None
```

Decision statuses:

```text
auto_allowed
approval_required
denied
budget_exceeded
policy_mismatch
requires_human_boundary
```

### Built-In Profiles

Add profiles:

- `manual`
- `safe-local`
- `supervised-codex`
- `daemon-safe`

`manual`:

- Preserve current behavior.
- Read-only evaluator decisions may report `auto_allowed`.
- Side-effect action contracts require approval.
- Adapter dispatch requires approval.
- Active repo mutation is not auto-allowed.

`safe-local`:

- Auto read tools.
- Auto local control-plane writes.
- Auto dry-run.
- Auto progress/review/summary local records.
- Pause Codex hosted boundary.
- Deny active repo apply-back.

`supervised-codex`:

- Auto read tools.
- Auto local control-plane writes.
- Auto repo planning if scoped hosted approval exists.
- Auto Codex isolated edit if scoped hosted approval exists and isolated workspace is enforced.
- Deny or pause active repo apply-back.

`daemon-safe`:

- Same risk posture as `safe-local`.
- Stricter budgets.
- Intended for later daemon run loop.

### Evaluator

Implement:

```python
def evaluate_autonomy(
    policy: AutonomyPolicy,
    request: AutonomyEvaluationInput,
) -> AutonomyDecision:
    ...
```

Evaluation order:

1. Deny or pause if a kill switch is active.
2. Deny active repo mutation unless explicitly permitted.
3. Deny unknown tools or adapters unless policy says approval is allowed.
4. Deny forbidden risks.
5. Pause human-boundary risks.
6. Require scoped approval for hosted, paid, networked, or external side-effect boundaries.
7. Require adapter allowlist for adapter dispatch.
8. Require task-type allowlist for task creation.
9. Require idempotency key for side effects if policy requires idempotency.
10. Require evidence contract if policy requires evidence.
11. Stop on budget exhaustion.
12. Return `auto_allowed` only when every check passes.

### Acceptance Criteria

- An action contract can be evaluated without user input.
- All auto-allowed actions can produce an `AutonomousApprovalRecord`.
- Denied actions produce an `AutonomyDecision` or `SecurityDecision` artifact.
- Autonomy cannot broaden `EffectivePolicy`.
- Autonomy respects runtime kill switches.
- Autonomy respects adapter breakers.
- Every autonomous step can link to objective, task, lease, run, and artifacts where applicable.
- Manual behavior remains unchanged.

### Tests

Add tests:

- `test_builtin_autonomy_profiles_exist`
- `test_manual_profile_requires_approval_for_side_effect`
- `test_read_tool_auto_allowed_under_safe_local`
- `test_control_plane_task_creation_auto_allowed_under_safe_local`
- `test_codex_adapter_pauses_without_hosted_approval`
- `test_codex_adapter_auto_allowed_with_scoped_hosted_approval`
- `test_active_repo_apply_back_not_auto_allowed`
- `test_autonomy_cannot_broaden_effective_policy`
- `test_autonomy_denies_unknown_adapter`
- `test_autonomy_respects_kill_switch`
- `test_autonomy_respects_adapter_breaker`
- `test_autonomy_budget_exhaustion_stops_loop`

### Verification

Run:

```bash
pytest tests/test_autonomy.py
pytest tests/test_cli*.py -k autonomy
pytest tests -k "policy or approval or chat_tools or execution or autonomy"
```

If practical:

```bash
pytest
```

## Phase 2: Action Contract Auto-Evaluator

### Goal

Wire existing pending chat action contracts through autonomy evaluation while preserving manual mode.

Current behavior:

```text
chat tool request
  -> action_contract_required
  -> pending_action_contract
  -> user confirms
  -> Harness executes
```

Target behavior:

```text
chat tool request
  -> action_contract_required
  -> pending_action_contract
  -> evaluate_autonomy_policy()
  -> auto_allowed: execute now and record autonomous approval evidence
  -> approval_required: show existing confirmation / pause
  -> denied: explain and stop
```

### Implementation Steps

1. Find the pending action contract path in `src/harness/chat.py`.
2. Find side-effect tool contract creation in `src/harness/chat_tools.py`.
3. Add autonomy profile selection to chat session state or CLI options.
4. Default to `manual`.
5. When a side-effect contract is produced, call `evaluate_autonomy(...)`.
6. If `manual`, preserve current behavior.
7. If `auto_allowed`, execute the same handler the confirmation path would execute.
8. Record `AutonomousApprovalRecord`.
9. Return execution evidence to chat.
10. If `approval_required`, preserve current confirmation path.
11. If `denied`, record denial evidence and return a blocked-state explanation.

### CLI/Config

Add:

```bash
harness --project . --autonomous
harness --project . --plain --autonomous
harness autonomy policy set --profile safe-local --project .
```

If persistent config is too much for this PR, implement only CLI-level selection and defer persistence.

### Acceptance Criteria

- Manual profile produces current behavior.
- `safe-local` can auto-allow predeclared local control-plane action contracts.
- Denied actions do not execute.
- Approval-required actions still show the existing confirmation flow.
- Every auto-allowed side effect records an autonomous approval artifact.

### Tests

- `test_manual_profile_preserves_pending_contract_confirmation`
- `test_safe_local_auto_executes_allowed_control_plane_contract`
- `test_denied_contract_is_not_executed`
- `test_approval_required_contract_remains_pending`
- `test_autonomous_approval_record_is_written`

## Phase 3: Autonomous Read-Only Chat Loop

### Goal

Turn the current bounded model tool-call support into a real read-only autonomous loop.

Existing primitive:

```text
MAX_CHAT_TOOL_CALLS = 3
```

This is useful but too turn-local. Add session-level and objective-level budgets.

Target loop:

```text
while objective not terminal:
  pack context
  ask LLM for next step
  parse tool request or answer
  execute read tool / evaluate action contract
  append observation
  update state
  stop on budget, terminal state, blocked state, denial, or no useful next action
```

### Limits

Add explicit loop limits:

- `max_model_turns`
- `max_tool_calls`
- `max_side_effect_actions`
- `max_runtime_seconds`
- `max_adapter_dispatches`
- `max_new_tasks`
- `max_consecutive_failures`

### Command

Add:

```bash
harness act "summarize this repo" --project . --autonomy safe-local --output json
```

For this phase, keep it read-only by default.

### Behavior

1. LLM inspects repo with read tools.
2. Harness executes only read-only tools.
3. Observations are appended to the loop.
4. LLM produces grounded final answer.
5. Loop stops on answer, budget exhaustion, blocked state, invalid tool request, or repeated failure.

### Acceptance Criteria

- No side-effect tools execute in this phase.
- Read-only tools can be called repeatedly within budget.
- JSONL events are written for model turns and tool observations.
- Final answer cites evidence from tool observations.
- Loop stops deterministically on budget exhaustion.

### Tests

- `test_autonomous_read_loop_uses_read_tools_until_answer`
- `test_autonomous_read_loop_stops_on_tool_budget`
- `test_autonomous_read_loop_rejects_side_effect_tool`
- `test_autonomous_read_loop_records_jsonl_evidence`
- `test_autonomous_read_loop_handles_invalid_tool_request`

## Phase 4: Autonomous Control-Plane Writes

### Goal

Allow the chat runtime to create local Harness records without live confirmation under `safe-local`.

Allowed local control-plane writes:

- Create objective.
- Create task.
- Create task graph.
- Write local memory note.
- Update progress metadata.
- Write review or summary artifacts.

Still forbidden or paused:

- Active repo mutation.
- External side effects.
- Hosted provider boundary without approval.
- Networked actions unless explicitly allowed.
- Paid actions.
- Personal messages.
- Job submissions.
- Broker/trading actions.

### Implementation Steps

1. Classify each side-effect chat tool by risk and boundary.
2. Add idempotency keys to local control-plane writes if missing.
3. Add evidence contracts to local control-plane writes if missing.
4. Wire each action contract through `evaluate_autonomy(...)`.
5. Allow `safe-local` to auto-execute only local control-plane writes.
6. Record `AutonomousApprovalRecord`.
7. Record linked objective/task/artifact ids.

### Acceptance Criteria

- `safe-local` can create objectives and tasks autonomously.
- Duplicate task creation is prevented through idempotency.
- Memory writes include scope, redaction state, source id, and hash.
- Active repo mutation remains blocked.
- Unknown local write tools remain approval-required or denied.

### Tests

- `test_safe_local_auto_creates_objective`
- `test_safe_local_auto_creates_task`
- `test_safe_local_auto_creates_task_graph`
- `test_idempotent_task_creation_not_duplicated`
- `test_memory_write_requires_scope_and_hash`
- `test_active_repo_mutation_still_blocked_under_safe_local`

## Phase 5: Autonomous Registered Adapter Dispatch

### Goal

Make registered adapters autonomy-ready and allow low-risk adapter dispatch under policy.

### Adapter Descriptor Metadata

Extend adapter descriptors with autonomy metadata:

```python
class ExecutionAdapterDescriptor(BaseModel):
    ...
    autonomy_default: Literal["auto_allowed", "approval_required", "forbidden"]
    max_autonomous_retries: int
    required_autonomy_scopes: list[str]
    output_contracts: list[str]
    terminal_evidence_required: list[str]
```

### Initial Adapter Settings

`dry_run`:

- `auto_allowed`

`read_only_summary`:

- `approval_required` by default.
- `auto_allowed` under `supervised-codex` when scoped hosted approval exists, if it crosses hosted boundary.

`repo_planning`:

- `approval_required` by default.
- `auto_allowed` under `supervised-codex` when scoped hosted approval exists.

`codex_isolated_edit`:

- `approval_required` by default.
- `auto_allowed` only under explicit `supervised-codex` and existing hosted approval.
- Requires isolated workspace enforcement.
- Apply-back remains forbidden or paused.

`docker_run_tests`:

- `approval_required` initially.
- Later auto-allowed only for strict/default sandbox, no network, and known command templates.

### Implementation Steps

1. Add autonomy metadata to adapter descriptors.
2. Update built-in adapter descriptors.
3. Update dispatcher validation to check autonomy metadata before auto-dispatch.
4. Ensure adapter breakers and leases remain enforced.
5. Ensure run manifests include autonomy decision ids.
6. Ensure output artifacts satisfy terminal evidence requirements.

### Acceptance Criteria

- Chat cannot dispatch arbitrary adapter ids.
- Unknown adapters are denied.
- `dry_run` can be auto-dispatched under `safe-local`.
- Hosted adapters require scoped approval.
- Codex isolated edit requires isolated workspace enforcement.
- Apply-back remains separate and blocked.

### Implementation Status

Completed in the current implementation slice:

- Adapter descriptors now include autonomy defaults, retry limits, required autonomy scopes, output contracts, and terminal evidence requirements.
- Built-in descriptors are populated for `dry_run`, `read_only_summary`, `repo_planning`, and `codex_isolated_edit`.
- Chat-side autonomous dispatch validates registered adapter metadata before execution.
- Unknown adapters are denied before dispatch.
- `dry_run` auto-dispatches under `safe-local`.
- `repo_planning` pauses without scoped hosted approval and runs under `supervised-codex` when scoped approval exists.
- Adapter breakers block autonomous dispatch.
- Autonomous dispatch records decision, approval, outcome, and run-manifest linkage evidence.

### Tests

- `test_dry_run_adapter_auto_allowed_under_safe_local`
- `test_unknown_adapter_denied`
- `test_repo_planning_requires_hosted_approval`
- `test_codex_isolated_edit_requires_isolated_workspace`
- `test_apply_back_not_auto_allowed_by_adapter_dispatch`
- `test_adapter_breaker_blocks_autonomous_dispatch`

Implemented coverage:

- `test_adapter_descriptors_expose_autonomy_metadata`
- `test_safe_local_auto_dispatches_dry_run_adapter_contract`
- `test_autonomous_dispatch_denies_unknown_adapter`
- `test_repo_planning_autonomous_dispatch_requires_hosted_approval`
- `test_repo_planning_autonomous_dispatch_runs_with_scoped_approval`
- `test_adapter_breaker_blocks_autonomous_dispatch`

## Phase 6: Autonomous Objective Runner

### Goal

Run existing objective/task graphs to completion under budgets, leases, dependency checks, and adapter descriptors.

Commands:

```bash
harness objectives run <objective_id> --autonomy safe-local --output json
harness daemon run-autonomous --project . --autonomy daemon-safe --output json
```

### Execution Model

The objective runner should be graph-driven, not free-form chat-driven:

```text
load objective
load task graph
select ready task
lease task
evaluate autonomy policy
dispatch registered adapter
record run/evidence
update task status
ask LLM reviewer/orchestrator for next task only when graph expansion is allowed
repeat
```

### Graph Expansion Gates

Add policy controls:

- `allow_create_tasks`
- `max_new_tasks_per_objective`
- `allowed_agents`
- `allowed_workbenches`
- `allowed_templates`
- `allowed_task_types`
- `allowed_adapters`

The LLM may propose new tasks, but Harness validates:

- Task type.
- Adapter id.
- Dependencies.
- Agent id.
- Workbench id.
- Output contracts.
- Permissions.
- Budget.
- Idempotency.

### Acceptance Criteria

- Runner selects only ready tasks.
- Runner leases before dispatch.
- Runner does not run tasks with unmet dependencies.
- Runner respects budgets.
- Runner stops on terminal state, blocked state, denial, approval requirement, or repeated failure.
- Runner writes objective-level event logs.
- Runner can resume without duplicating idempotent tasks.

### Implementation Status

Completed in the current implementation slice:

- Added `src/harness/objective_runner.py` with a graph-driven autonomous objective runner.
- Added `harness objectives run <objective_id> --autonomy safe-local --output json`.
- Added `harness daemon run-autonomous --project . --autonomy daemon-safe --output json`.
- Runner selects objective-scoped ready or dependency-unblocked tasks only.
- Runner leases before dispatch and reuses an existing active objective lease on resume.
- Runner evaluates autonomy policy and adapter descriptor metadata before every dispatch.
- Runner records objective-level JSONL events under `.harness/autonomy/objectives/`.
- Runner writes autonomy decisions, autonomous approvals, autonomous outcomes, and run-manifest linkage evidence.
- Runner stops on objective success, terminal failure state, blocked state, approval requirement, denial, execution failure, or adapter dispatch budget exhaustion.
- Runner does not create or duplicate tasks in this phase; graph expansion remains gated future work.

### Tests

- `test_objective_runner_runs_ready_task`
- `test_objective_runner_respects_dependencies`
- `test_objective_runner_requires_lease`
- `test_objective_runner_stops_on_budget`
- `test_objective_runner_stops_on_policy_denial`
- `test_objective_runner_resume_does_not_duplicate_task`
- `test_objective_runner_links_tasks_leases_runs_artifacts`

Implemented coverage:

- `test_objective_runner_runs_ready_task`
- `test_objective_runner_respects_dependencies`
- `test_objective_runner_requires_lease_and_links_run_evidence`
- `test_objective_runner_stops_on_budget`
- `test_objective_runner_stops_on_policy_denial`
- `test_objective_runner_resume_does_not_duplicate_task`
- `test_objectives_run_cli_outputs_json`
- `test_daemon_run_autonomous_cli_runs_next_active_objective`

## Phase 7: Supervised-Codex Approval Profiles

### Goal

Replace repeated live confirmation with predeclared scoped approval profiles for hosted Codex planning and isolated edits.

Example command:

```bash
harness approvals add \
  --backend codex_cli \
  --data-boundary hosted_provider \
  --project . \
  --task-types repo_planning,codex_code_edit \
  --duration-hours 8 \
  --autonomy-scope supervised-codex
```

### Approval Fields

Add or extend approval records with:

- `approval_id`
- `allowed_task_types`
- `allowed_adapters`
- `allowed_workbenches`
- `allowed_objective_ids`
- `expires_at`
- `max_runs`
- `max_total_runtime`
- `max_context_bytes`
- `revoked_at`
- `autonomy_scope`

### Behavior

The autonomous loop can proceed without asking again only inside the exact approval scope.

Hosted approval does not allow:

- Active repo mutation.
- Apply-back.
- Arbitrary network access.
- Arbitrary shell commands.
- Approval extension.
- Task type expansion.

### Acceptance Criteria

- Scoped approval allows `repo_planning` under `supervised-codex`.
- Scoped approval allows `codex_isolated_edit` under `supervised-codex` only with isolated workspace enforcement.
- Expired approvals are ignored.
- Revoked approvals are ignored.
- Approval max run count is enforced.
- Approval max runtime is enforced.
- Active repo apply-back remains separate and paused or denied.

### Implementation Status

Completed in the current implementation slice:

- Approval profiles now include allowed task types, allowed adapters, allowed workbenches, allowed objective ids, revoked timestamp, max run count, max total runtime seconds, max context bytes, and autonomy scope.
- `harness approvals add` now accepts `--duration-hours`, `--autonomy-scope`, `--allowed-adapters`, `--allowed-workbenches`, `--allowed-objectives`, `--max-runs`, `--max-total-runtime-seconds`, and `--max-context-bytes`.
- Approval validation remains backward compatible with existing `task_types` and `--duration-days` approvals.
- Strict scoped validation is used by autonomous chat and objective runners so `supervised-codex` approvals cannot be reused across mismatched task types, adapters, objectives, workbenches, or autonomy scopes.
- Registered adapter execution and progress/security checks now pass task context into hosted approval lookup so scoped approvals are recognized by the execution layer.
- Expired and revoked approvals are ignored, and revoked approvals record `revoked_at`.
- Approval max run count and max total runtime are enforced from local run records that reference the approval id.
- Apply-back remains outside hosted approval scope and is still denied or paused by the existing active-repo boundary.

### Tests

- `test_scoped_hosted_approval_allows_repo_planning`
- `test_scoped_hosted_approval_allows_codex_isolated_edit`
- `test_expired_approval_does_not_allow_autonomy`
- `test_revoked_approval_does_not_allow_autonomy`
- `test_approval_max_runs_enforced`
- `test_approval_scope_cannot_be_broadened_by_model`
- `test_apply_back_still_requires_separate_policy`

Implemented coverage:

- `test_scoped_hosted_approval_allows_repo_planning_under_supervised_codex`
- `test_scoped_hosted_approval_allows_codex_isolated_edit_exact_scope`
- `test_expired_approval_is_rejected`
- `test_revoke_approval`
- `test_approval_max_runs_enforced`
- `test_approval_max_total_runtime_enforced`
- `test_approval_scope_cannot_be_broadened_by_model`
- `test_approvals_add_accepts_duration_hours_and_scope_fields`
- `test_repo_planning_autonomous_dispatch_runs_with_scoped_approval`

## Phase 8: Evidence, Memory, And Recovery Hardening

### Goal

Make autonomous runs durable, inspectable, and recoverable.

### Artifact-Based Memory

Current local memory notes exist. Add durable working memory derived from artifacts.

Memory sources:

- `artifact_summary`
- `objective_state`
- `run_review`
- `failed_attempt_summary`

Rules:

- Memory cannot grant permissions.
- Memory cannot satisfy approvals.
- Memory must include source artifact/run/task ids.
- Memory must have redaction state and hash.
- Memory must be scoped to project, workbench, objective, agent, or task.

### Evidence Requirements

Every autonomous step should write:

- Model turn id.
- Tool request.
- Action contract.
- Autonomy decision.
- Approval record if auto-allowed.
- Policy hash.
- Lease id if applicable.
- Run id if applicable.
- Artifact ids.
- Stop reason.

### Crash Recovery

Recovery should:

1. Load last objective state.
2. Load active leases.
3. Detect in-flight runs.
4. Reconcile completed artifacts.
5. Avoid duplicate idempotent side effects.
6. Mark abandoned runs appropriately.
7. Resume only within current policy and current approvals.

### Acceptance Criteria

- Autonomous loop writes JSONL evidence.
- Objective runner can recover after interruption.
- Idempotency prevents duplicate task/objective creation.
- Memory does not grant permissions.
- Secret-like artifacts are blocked or redacted.
- Evidence links are complete enough to audit the run.

### Implementation Status

Completed in the current implementation slice:

- Added task-scoped memory records.
- Added durable derived memory source kinds: `artifact_summary`, `objective_state`, `run_review`, and `failed_attempt_summary`.
- Added `SQLiteStore.save_derived_memory(...)` for artifact/run/objective/failed-attempt derived memory with source validation.
- Added `harness memory save-derived` for explicit derived memory capture.
- Derived memory records preserve source ids, source artifact ids where applicable, redaction state, hash, size, and non-authoritative lineage.
- Secret-like derived memory summaries are redacted before persistence.
- Derived memory cannot grant permissions, satisfy approvals, weaken policy, or act as approval evidence.
- Autonomous read-loop JSONL now records stable `model_turn_id` values.
- Objective runner now performs daemon lease recovery before each autonomous objective run and records a `recovery_checked` objective event.
- Objective runner `adapter_dispatched` events now include autonomy decision ids, autonomous approval ids, autonomous outcome ids, lease ids, run ids, artifact ids, policy id, and stop reason placeholder.

### Tests

- `test_autonomous_loop_records_jsonl_evidence`
- `test_autonomous_objective_links_tasks_leases_runs_artifacts`
- `test_autonomous_recovery_does_not_duplicate_idempotent_task`
- `test_memory_cannot_satisfy_approval`
- `test_memory_cannot_grant_permission`
- `test_secret_like_artifact_blocks_or_redacts`

Implemented coverage:

- `test_autonomous_read_loop_records_jsonl_evidence`
- `test_objective_runner_requires_lease_and_links_run_evidence`
- `test_objective_runner_resume_does_not_duplicate_task`
- `test_artifact_based_memory_has_source_links_hash_and_no_authority`
- `test_secret_like_derived_memory_is_redacted`
- `test_memory_save_derived_cli_outputs_json`
- Existing `test_malicious_memory_cannot_authorize_hosted_execution`
- Existing `test_memory_notes_redact_secret_looking_text_without_persisting_raw_values`

## Phase 9: Reviewer Agents And Bounded Multi-Agent Workflows

Status: implemented in the current working tree.

Implementation notes:

- The `coding_fix` workflow now expands to read-only planning, isolated Codex edit, local sandbox-test evidence, implementation review, security review, and final synthesis tasks.
- Coding reviewer agents are registered as built-in read-only reviewers: `implementation_reviewer`, `security_reviewer`, and `factuality_reviewer`.
- Reviewer stages carry task metadata such as `workflow_stage`, `review_role`, `completion_gate`, `blocks_apply_back`, and `requires_evidence_links`.
- Chat action contracts and foreground orchestration preserve reviewer metadata into stored task records.
- Dry-run final reports now include objective, task, lease, run, policy, workflow-stage, review-role, and artifact-evidence links.
- Context packing keeps security, sandbox, built-in domain, and recent artifact metadata available even after adding reviewer specs.

### Goal

Add mandatory review phases so autonomy is bounded by independent artifact review, not an endless single-model loop.

### Coding Workflow

Target graph:

```text
coding_orchestrator
  -> repo_planning
  -> codex_isolated_edit
  -> test_sandbox
  -> implementation_reviewer
  -> security_reviewer
  -> final report
  -> apply-back remains paused unless explicitly allowed
```

### Research Workflow

Target graph:

```text
research_orchestrator
  -> read-only inspection
  -> research brief
  -> factuality reviewer
  -> synthesis
```

### Stop Conditions

Stop when:

- Objective succeeded.
- All tasks are terminal.
- Approval is required.
- Policy denies an action.
- Budget is exhausted.
- Same failure occurs twice.
- Adapter breaker is open.
- Secret/path finding is detected.
- Diff contains unsupported changes.
- Tests fail after max retries.
- Model produces no valid next action.

### Acceptance Criteria

- Reviewer agents produce artifacts.
- Reviewer tasks are part of the task graph.
- Reviewer failure blocks completion.
- Security reviewer can block apply-back.
- Final report includes objective, task, run, artifact, and policy evidence.

### Tests

- `test_coding_workflow_requires_implementation_review`
- `test_coding_workflow_requires_security_review`
- `test_review_failure_blocks_completion`
- `test_final_report_links_required_evidence`
- `test_same_failure_twice_stops_workflow`

## Security Regression Matrix

Status: implemented in the current working tree.

Implementation notes:

- Unknown model-requested tools such as shell execution now fail closed as rejected tool requests instead of being folded back into normal chat text.
- `request_approval` is no longer auto-eligible under non-manual autonomy profiles; approval grants and approval-expiry changes remain outside the autonomous loop.
- Chat-created task metadata is checked against registered adapter rejected metadata before an action contract can be accepted.
- Regression tests now cover forbidden path reads, shell requests, network and paid-provider task metadata, apply-back pause behavior, secret redaction, self-approval attempts, approval extension attempts, budget/profile tampering, and autonomous forbidden-path reads.

Add and maintain these tests across phases:

- `test_prompt_injection_cannot_request_forbidden_path`
- `test_model_requested_shell_is_rejected`
- `test_model_requested_network_is_rejected_without_policy`
- `test_model_requested_paid_fallback_is_rejected`
- `test_model_requested_apply_back_pauses`
- `test_secret_like_artifact_blocks_or_redacts`
- `test_model_cannot_self_grant_approval`
- `test_model_cannot_extend_approval_expiry`
- `test_model_cannot_raise_budget`
- `test_model_cannot_change_autonomy_profile_mid_run`
- `test_forbidden_path_protection_applies_to_autonomous_runs`

## Golden Flow Matrix

Status: implemented in the current working tree.

Implementation notes:

- Golden tests now cover autonomous repo summary, autonomous local planning/task graph creation, supervised-Codex autonomous repo planning, supervised-Codex autonomous isolated edit with denied apply-back, and kill-switch pause behavior.
- The autonomous objective runner now checks active execution controls before dispatch and records a policy denial without creating a run when a matching control is disabled.
- Hosted golden flows use scoped approval profiles with adapter/objective/autonomy scope constraints and deterministic fake Codex backends.
- The isolated-edit golden flow verifies diff evidence while preserving the active repository byte-for-byte before apply-back.

Add end-to-end golden flows gradually:

```text
init -> autonomous repo summary -> artifact/report evidence
init -> autonomous planning -> objective/task graph evidence
init -> supervised-codex approval -> autonomous repo_planning -> run manifest
init -> supervised-codex approval -> autonomous isolated edit -> diff artifact -> no active repo mutation
init -> kill switch open -> autonomous loop pauses
```

## Immediate PR Sequence

### PR 1: `AutonomyPolicy` And `AutonomyDecision`

Status: implemented in the current working tree.

Add:

- Models.
- Built-in profiles.
- JSON explain/inspect command.
- Tests.

Behavior change:

- None.

### PR 2: Action Contract Auto-Evaluator

Status: implemented in the current working tree.

Add:

- Pending chat action contracts pass through autonomy evaluation.
- Manual profile preserves current behavior.
- Auto-allowed decisions execute through existing handler path.
- Denied decisions return blocked-state explanation.

Behavior change:

- Only when non-manual autonomy profile is selected.

### PR 3: Autonomous Read-Only Chat Loop

Status: implemented in the current working tree.

Add:

- Multi-turn read-only loop.
- Objective/session budgets.
- Evidence JSONL.

Behavior change:

- Read-only autonomy under explicit command/profile.

### PR 4: Autonomous Control-Plane Writes

Status: implemented in the current working tree.

Add:

- Auto-create objectives, tasks, task graphs, local memory, and progress records under `safe-local`.
- Idempotency and evidence requirements.

Behavior change:

- Local Harness records can be written without live confirmation under policy.

### PR 5: Autonomous Registered Adapter Dispatch

Status: implemented in the current working tree.

Add:

- Adapter autonomy metadata.
- Auto `dry_run`.
- Then `read_only_summary` and `repo_planning` when approvals exist.

Behavior change:

- Allowed registered adapters can be dispatched under policy.

### PR 6: Autonomous Objective Runner

Status: implemented in the current working tree.

Add:

- Graph-driven runner.
- Lease-aware task selection.
- Budgeted adapter dispatch.
- Recovery-safe task progression.

Behavior change:

- Existing task graphs can run to policy-bounded completion.

### PR 7: Supervised-Codex Autonomy Profile

Status: implemented in the current working tree.

Add:

- Scoped hosted approval profile behavior.
- Codex repo planning and isolated edit autonomy.
- Apply-back remains blocked.

Behavior change:

- Codex planning/editing can proceed without live confirmation only inside explicit approval scope.

### PR 8: Evidence And Recovery Hardening

Status: implemented in the current working tree.

Add:

- Chat session JSONL.
- Autonomy decision artifacts.
- Crash recovery.
- Idempotency checks.
- Duplicate-prevention tests.
- Compare/baseline integration if applicable.

Behavior change:

- Autonomous runs become more durable and auditable.

### PR 9: Reviewer Agents And Bounded Multi-Agent Workflows

Status: implemented in the current working tree.

Add:

- Artifact-producing reviewers.
- Final synthesis tasks.
- Required review gates.

Behavior change:

- Completion requires reviewer artifacts for selected workflow types.

## Definition Of Done

Status: implemented in the current working tree.

Implemented now:

- `harness act` is no longer limited to read-only tool loops.
- Under non-manual autonomy, model-requested side effects can become Harness action contracts and be auto-approved or denied by policy.
- When `harness act` creates a task graph, Harness can immediately run the created objective through the autonomous objective runner and return objective/run evidence to the model loop.
- Local dry-run task graphs can be created and run end to end from `harness act` without live confirmation under `safe-local`.
- `harness act` can complete the full supervised-Codex coding chain from a model-created graph through `repo_planning`, `codex_isolated_edit`, sandbox-test evidence, implementation review, security review, and final synthesis in one command when scoped approvals exist.
- Apply-back remains stopped or denied unless a separate explicit apply-back policy exists.
- A deterministic full Definition-of-Done golden test covers scoped supervised-Codex approvals and fake Codex backends.

The target is met when this works:

```bash
harness act "inspect this repo, identify the next missing autonomy feature, implement it in isolation, run the safe tests, and produce a review report" \
  --project . \
  --autonomy supervised-codex \
  --output json
```

Expected behavior:

- Chat model inspects repo with read tools.
- Harness creates objective/task graph without live confirmation.
- Harness dispatches `repo_planning` if approval exists.
- Harness dispatches `codex_isolated_edit` in isolated workspace if approval exists.
- Harness runs only approved/sandboxed tests.
- Harness writes manifests, events, artifacts, policy hashes, security decisions, and autonomy decisions.
- Harness reviews output.
- Harness stops at apply-back, blocked policy, failure, or budget boundary.
- Harness never mutates the active repo unless a separate apply-back policy explicitly permits it.

## Session Checklist

Use this checklist at the start of each implementation session while changing the
autonomous runtime:

- [ ] Identify whether the work is implementation, hardening, documentation, or release preparation.
- [ ] Confirm manual mode remains supported.
- [ ] Confirm the behavior change has a narrow boundary.
- [ ] Read nearby policy, chat, approval, execution, and test code.
- [ ] Reuse existing Harness types where possible.
- [ ] Add or update tests before broad wiring.
- [ ] Run focused tests.
- [ ] Run nearby regression tests.
- [ ] Confirm no active repo mutation path changed unless the phase explicitly allows it.
- [ ] Write a final report listing changed files, behavior changes, and verification commands.

## Non-Negotiable Constraints

- The model cannot execute arbitrary shell commands.
- The model cannot write files directly.
- The model cannot self-approve.
- The model cannot grant itself new tools.
- The model cannot broaden policy.
- The model cannot broaden approval scope.
- The model cannot mutate active repo files through an isolated edit path.
- Apply-back is always a separate higher boundary.
- Every autonomous side effect must be idempotent or explicitly non-repeatable with evidence.
- Every autonomous side effect must produce auditable evidence.

## Next Session Recommendation

The PR 1 through PR 9 implementation sequence is complete in the current working
tree. The next session should be a stabilization and review pass, not another
feature expansion pass.

Recommended order:

- Review the full diff by subsystem: autonomy policy, chat runtime, objective runner, approvals, adapters, memory, workflow templates, CLI, docs, and tests.
- Re-run the full test suite from a clean shell.
- Inspect the generated golden evidence tests and make sure they describe the intended release contract.
- Decide whether to land as one large autonomy-runtime PR or split into staged PRs matching the sequence above.
- If splitting, preserve this order: PR 1 autonomy models, PR 2 action-contract evaluator, PR 3 chat loop, PR 4 control-plane writes, PR 5 adapter dispatch, PR 6 objective runner, PR 7 supervised-Codex profile, PR 8 evidence/recovery, PR 9 reviewers.
- Keep active repo apply-back out of scope unless a separate approval-profile design is written and tested.

Completion evidence already recorded in this working tree:

- All major phases and PR sequence sections above are marked implemented.
- `harness act` supports policy-gated side-effect action contracts under non-manual autonomy.
- `safe-local` can create and run local dry-run task graphs without live confirmation.
- `supervised-codex` can run the reviewed coding workflow when scoped hosted approvals exist.
- The active repo remains protected from apply-back unless a separate explicit boundary permits it.
- Full-suite verification snapshot on 2026-05-13: `pytest -q` passed with 739 tests in 101.63 seconds.
- Diff hygiene snapshot: `git diff --check` passed.

The detailed historical PR 1 plan remains available at:

```text
docs/plans/autonomy_pr1_implementation_plan.md
```
