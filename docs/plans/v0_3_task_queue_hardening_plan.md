# v0.3 Task Queue Hardening Plan

This plan defines v0.3 as a queue-first control-plane milestone. It is an implementation plan for planning and later code work; it does not authorize broad autonomy, daemon execution, backend calls from queued tasks, hosted fallback, or any external automation.

## 1. Goal

Make the manual task queue durable, policy-aware, lease-aware, idempotent, and inspectable.

The v0.3 task queue must become the substrate for future daemon execution without becoming a daemon itself. It should persist objectives, tasks, dependencies, attempts, leases, transitions, and enough JSON evidence for an operator to understand queue state without manually opening SQLite.

The central v0.3 rule:

```text
Task commands may create, inspect, organize, select, lease, cancel, and retry manual work.
Task commands must not execute agents, call backends, run Docker, mutate active repos, start background work, or create run artifacts.
```

## 2. Scope Boundaries

### In scope

- Objective persistence and CLI.
- Task persistence upgrades.
- Explicit task status state machine.
- Dependency persistence and graph inspection.
- Task attempts.
- Task leases.
- Task transition evidence.
- Idempotency metadata.
- Approval-aware task state.
- Hardened `run-next`.
- Stable JSON output.
- Regression tests for queue behavior.

### Related but not implemented in this v0.3 queue slice

- `EffectivePolicy` snapshots.
- Manifest v1.1 runtime integration.
- Artifact immutability upgrades.
- Tool capability descriptors.
- Trace export.
- Compare/baseline/eval commands.

These remain control-plane prerequisites or follow-on slices. The queue should reserve fields where useful, but this plan should not over-specify those systems.

### Explicitly out of scope

- Daemon.
- Scheduler loop.
- Autonomous background work.
- Backend execution from tasks.
- Codex execution from tasks.
- Local model execution from tasks.
- Docker execution from tasks.
- MCP.
- A2A.
- Browser automation.
- Email/calendar integration.
- Generic shell.
- Hosted fallback.
- Paid API fallback.
- OpenAI API or `OPENAI_API_KEY` usage.
- Live trading, broker integration, external messages, job submission, or any irreversible real-world action.

## 3. Current Baseline

The current repository already has a minimal manual queue:

- `TaskRecord`.
- `TaskStatus` values: `queued`, `blocked`, `running`, `completed`, `failed`, `canceled`.
- `tasks` SQLite table.
- `harness tasks add/list/inspect/status/run-next`.
- Built-in agent/workbench reference validation.
- JSON output for task add/list/inspect/run-next.
- Guard tests proving task commands do not preflight backends or expose settings.

The current `run-next` behavior selects the highest-priority queued task whose inline `depends_on` tasks are completed, then immediately changes it to `running`. v0.3 must harden this into explicit ready/blocked/leased/attempt behavior before any future execution layer depends on it.

## 4. Public CLI Contract

### Objectives

Add a top-level `objectives` command group:

```bash
harness objectives add --title "..." [--description "..."] [--workbench coding] [--priority 0] --project . --output json
harness objectives list --project . --output json
harness objectives inspect <objective_id> --project . --output json
```

Rules:

- Objective commands are persistence and inspection only.
- Objective commands must not create tasks automatically.
- Objective commands must not execute agents or backends.
- Invalid workbench references return stable JSON errors.

JSON schemas:

```text
harness.objective/v1
harness.objectives/v1
```

### Tasks

Keep and harden:

```bash
harness tasks add --title "..." [--description "..."] [--objective <objective_id>] [--agent repo_inspector] [--workbench coding] [--priority 0] --project . --output json
harness tasks list [--status ready] [--objective <objective_id>] --project . --output json
harness tasks inspect <task_id> --project . --output json
harness tasks status <task_id> <status> --project . --output json
harness tasks run-next --project . --output json
```

Add:

```bash
harness tasks cancel <task_id> --project . --output json
harness tasks retry <task_id> --project . --output json
harness tasks graph [--objective <objective_id>] --project . --output json
```

Task command rules:

- `tasks add` creates a task in `ready` when it has no unsatisfied dependencies and no required approvals.
- `tasks add` creates a task in `blocked` when dependencies are declared and not yet satisfied.
- `tasks add` creates a task in `waiting_approval` when metadata marks approval as required and not granted.
- `tasks status` remains available for operator-controlled manual correction, but it must enforce the same state machine unless an explicit future override flag is added.
- `tasks cancel` transitions cancellable tasks to `cancelled`.
- `tasks retry` transitions failed tasks back to `ready` or `blocked`, depending on dependencies.
- `tasks graph --json` emits objective/task/dependency state and blocked reasons.
- No task command creates a run, run directory, run manifest, run artifact, Docker process, backend subprocess, network call, or background worker.

JSON schemas:

```text
harness.task/v1
harness.tasks/v1
harness.task_graph/v1
harness.task_run_next/v1
harness.task_error/v1
```

All JSON output must include:

- `schema_version`.
- `ok`.
- Payload object or `errors`.

## 5. Data Model

### TaskStatus

Replace or compatibility-map the old status vocabulary to:

```text
created
ready
blocked
waiting_approval
leased
running
succeeded
failed
cancelled
skipped
```

Compatibility mapping:

```text
queued    -> ready
completed -> succeeded
canceled  -> cancelled
```

Existing projects should remain readable. Additive migration is preferred over destructive rewrite. If a stored old value is encountered, the runtime should either map it when reading or migrate it during store initialization.

### ObjectiveRecord

Fields:

```text
id
title
description
status
project_root
created_at
updated_at
priority
workbench_id
metadata
```

Initial statuses:

```text
created
active
completed
cancelled
```

v0.3 does not need objective automation. Objectives group and inspect work only.

### TaskRecord

Upgrade tasks with:

```text
id
title
description
status
project_root
created_at
updated_at
priority
objective_id
workbench_id
agent_id
spec_source_kind
spec_source_path
idempotency_key
required_approvals
approval_state
run_id
metadata
```

Rules:

- Every task gets an `idempotency_key` at creation.
- `idempotency_key` must be stable across retry.
- `run_id` remains nullable in v0.3 because task commands do not execute work.
- `metadata` must be sanitized before persistence/output.
- Secret-like values must not be emitted.

### TaskDependency

Persist dependencies in a dedicated table instead of relying only on inline JSON.

Fields:

```text
id
upstream_task_id
downstream_task_id
dependency_type
required_artifact_kind
created_at
```

Initial dependency types:

```text
success
manual
approval
artifact
```

v0.3 implementation may initially expose only success dependencies in CLI if that keeps the first slice small, but the persisted shape should not block the later dependency types.

Rules:

- No self-dependency.
- No dependency cycles.
- Downstream task cannot become `ready` until dependencies are satisfied.
- Missing dependency references are stable errors.

### TaskAttempt

Fields:

```text
id
task_id
attempt_number
status
lease_id
run_id
created_at
started_at
finished_at
failure_code
failure_message
metadata
```

Rules:

- `run-next` creates an attempt when it leases a task.
- In v0.3, attempts may stop at `leased` because no execution happens.
- Later execution can bind `run_id`.
- Attempt numbers are monotonic per task.

### TaskLease

Fields:

```text
id
task_id
attempt_id
owner
status
acquired_at
expires_at
heartbeat_at
released_at
metadata
```

Initial lease statuses:

```text
active
released
expired
cancelled
```

Rules:

- `run-next` atomically creates one active lease for one selected ready task.
- A task with an active lease is not selectable.
- Duplicate or concurrent `run-next` calls must not lease the same task twice.
- Lease duration should use a conservative default, for example 30 minutes, recorded in code as a named constant.
- v0.3 does not need heartbeat renewal unless simple to add with the data model.

### TaskTransitionRecord

Persist state transitions or equivalent evidence.

Fields:

```text
id
task_id
from_status
to_status
reason
actor
created_at
metadata
```

Rules:

- Every status change records a transition.
- Invalid transitions return stable errors and should be test-covered.
- Transition metadata must be sanitized.

## 6. State Machine

Allowed transitions:

```text
created -> ready
created -> blocked
created -> waiting_approval
ready -> blocked
ready -> waiting_approval
ready -> leased
ready -> cancelled
blocked -> ready
blocked -> cancelled
waiting_approval -> ready
waiting_approval -> cancelled
leased -> running
leased -> ready
leased -> failed
leased -> cancelled
running -> succeeded
running -> failed
running -> waiting_approval
failed -> ready
failed -> cancelled
```

Optional terminal transition:

```text
ready -> skipped
blocked -> skipped
waiting_approval -> skipped
```

Manual `tasks status` should use these transitions. If the requested transition is invalid, return a stable JSON error and do not mutate the task.

`tasks retry` is the operator-friendly path for:

```text
failed -> ready
failed -> blocked
```

Retry target selection:

- If dependencies are satisfied, retry moves to `ready`.
- If dependencies are unsatisfied, retry moves to `blocked`.
- Retry must not create a run or backend execution.

`tasks cancel` may cancel:

```text
created
ready
blocked
waiting_approval
leased
running
failed
```

It must not change:

```text
succeeded
cancelled
skipped
```

## 7. Scheduler Behavior

`harness tasks run-next` must:

1. Open one SQLite transaction.
2. Find tasks with status `ready`.
3. Exclude tasks with active leases.
4. Verify dependencies are satisfied.
5. Select the highest priority task, then oldest `created_at`.
6. Create a task attempt.
7. Create an active lease.
8. Transition task `ready -> leased`.
9. Return selected task, attempt, and lease in JSON.

If no task is selectable, return:

```json
{
  "schema_version": "harness.task_run_next/v1",
  "ok": true,
  "selected_task": null
}
```

`run-next` must not:

- Change a task to `running`.
- Create a `RunRecord`.
- Create `.harness/runs/<run_id>`.
- Initialize run artifacts.
- Execute a backend.
- Execute Docker.
- Inspect or read secrets.
- Start background work.

## 8. Policy and Approval Handling

v0.3 queue commands are non-executing, so policy handling is metadata and gating only.

Rules:

- Tasks can record `required_approvals`.
- Tasks with unresolved required approvals should be `waiting_approval`.
- `run-next` must skip `waiting_approval`.
- Approval metadata in task JSON must not contain secrets.
- No v0.3 task command should preflight Codex, local-compatible backends, Docker, hosted providers, or network access.

If EffectivePolicy is not implemented yet, v0.3 should use a small local queue-level representation for `required_approvals` and `approval_state`, then leave full policy resolution to the planned EffectivePolicy slice.

## 9. SQLite Migration Strategy

Use additive migrations through `SQLiteStore.initialize()`:

- Keep the existing `tasks` table readable.
- Add missing columns with `_ensure_column`.
- Create new tables if absent:
  - `objectives`.
  - `task_dependencies`.
  - `task_attempts`.
  - `task_leases`.
  - `task_transitions`.
- Migrate or map old statuses:
  - `queued` to `ready`.
  - `completed` to `succeeded`.
  - `canceled` to `cancelled`.
- Add indexes for scheduler-critical queries:
  - task status/priority/created_at.
  - dependency downstream/upstream.
  - active lease by task.
  - attempt by task.

Do not implement destructive migration. Do not drop old columns such as `depends_on_json`; keep them as compatibility inputs until the new dependency table is stable.

## 10. Implementation Slices

### Slice 1 — Models and migrations

- Add new records/enums.
- Add tables and additive columns.
- Add status compatibility mapping.
- Add transition validator.
- Add tests for fresh initialization and old status mapping.

### Slice 2 — Objective CLI

- Add `objectives` Typer app.
- Add objective add/list/inspect.
- Validate workbench references.
- Add JSON and text output.
- Add CLI smoke tests.

### Slice 3 — Task upgrades

- Add `objective_id`, `idempotency_key`, `required_approvals`, and approval state.
- Add task dependency persistence.
- Add task graph JSON.
- Preserve existing task add/list/inspect behavior where compatible.
- Add tests for dependency graph and missing references.

### Slice 4 — State machine, cancel, and retry

- Enforce valid transitions.
- Add transition records.
- Add `tasks cancel`.
- Add `tasks retry`.
- Add invalid transition tests.

### Slice 5 — Harden run-next

- Add atomic lease and attempt creation.
- Change selected task status to `leased`, not `running`.
- Return task, attempt, and lease JSON.
- Add duplicate/concurrent run-next tests.
- Confirm no run artifacts are created.

### Slice 6 — Release hygiene

- Update operator guide and smoke checklist.
- Run full regression suite.
- Verify no task command reads or exposes backend settings/secrets.
- Verify no `.harness/`, `.env*`, secret-like files, SQLite files, or private keys were modified as planning/edit targets.

## 11. Test Plan

### Model and state-machine tests

- Valid transitions succeed.
- Invalid transitions fail with stable errors.
- Old statuses map correctly:
  - `queued` -> `ready`.
  - `completed` -> `succeeded`.
  - `canceled` -> `cancelled`.
- Every task gets an `idempotency_key`.
- Retry preserves `idempotency_key`.
- Missing dependencies return stable errors.
- Dependency cycles are rejected.

### SQLite tests

- New tables initialize on a fresh project.
- Existing task data remains readable after additive migrations.
- Objective records persist and round-trip.
- Dependency records persist and round-trip.
- Attempt records persist and round-trip.
- Lease records persist and round-trip.
- Transition records persist and round-trip.
- Lease acquisition is atomic.
- Duplicate `run-next` does not lease the same task twice.

### CLI smoke tests

- `objectives add/list/inspect` support text output.
- `objectives add/list/inspect` support JSON output.
- `tasks cancel` enforces valid transitions.
- `tasks retry` enforces valid transitions.
- `tasks graph --json` returns objective, tasks, dependencies, blocked reasons, and current statuses.
- `run-next` returns `selected_task: null` when no ready work exists.
- `run-next` creates no runs and no run artifact directories.
- Task commands do not preflight Codex or local model backends.
- Task commands do not expose `api_key`, `OPENAI_API_KEY`, `base_url`, environment variables, or secret-like metadata.

### Failure-mode tests

- Dependency cycle.
- Blocked dependency.
- Approval-required pause.
- Duplicate `run-next`.
- Retry after failure.
- Retry rejected for non-failed tasks.
- Unknown task.
- Unknown objective.
- Unknown dependency.
- Invalid agent reference.
- Invalid workbench reference.
- Missing required title.
- Task asks for forbidden active repo write in a non-executing queue command.

## 12. Acceptance Criteria

v0.3 queue hardening is complete when:

- A new project initializes all queue tables.
- Existing minimal task records remain readable.
- Objectives can be added, listed, and inspected.
- Tasks can be added, listed, inspected, cancelled, retried, and graphed.
- Tasks have deterministic statuses and transition evidence.
- Tasks have idempotency keys.
- Dependencies are persisted and cycle-checked.
- `run-next` selects only ready work.
- `run-next` creates an attempt and lease atomically.
- `run-next` never executes work or creates run artifacts.
- Duplicate `run-next` cannot lease the same task twice.
- Approval-required tasks pause in `waiting_approval`.
- JSON output is schema-versioned and stable.
- Full regression tests pass.

## 13. Assumptions and Defaults

- v0.3 is queue-first and manual.
- `run-next` means select-and-lease, not execute.
- Lease duration defaults to 30 minutes unless a later implementation plan changes it.
- Old task statuses remain compatibility inputs during migration.
- Full EffectivePolicy integration is not required for the first queue implementation slice.
- Full manifest v1.1 runtime integration is not required for the first queue implementation slice.
- Artifact immutability and tool descriptors remain follow-on control-plane work.
- Daemon work starts no earlier than v0.4.
- Existing AGENTS.md hard rules remain binding.
