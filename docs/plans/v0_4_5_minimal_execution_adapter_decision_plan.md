# v0.4.5 Minimal Execution Adapter Decision Plan

Status: Slice 1 dry-run lease-to-run execution contract and Slice 2 dry-run recovery/inspection hardening are implemented. Further real execution remains unauthorized until a separate follow-on implementation slice is accepted.

## Summary

v0.4 completed local daemon scheduler readiness: the daemon can record lifecycle evidence, acquire or renew leases, recover expired leases, and pause dependency-blocked, approval-required, or daemon-policy-forbidden tasks. It still does not execute tasks.

This plan decides whether the next milestone should add a minimal execution adapter from a daemon-held task lease into the existing run machinery. The default decision is conservative: do not implement execution unless all policy, approval, sandbox, artifact, trace, idempotency, and crash-recovery contracts below are decision-complete.

## Default Decision

Do not implement daemon task execution yet.

The next implementation may proceed only if it remains a narrow, supervised, local execution adapter and avoids every out-of-scope capability listed in this plan. If the contract cannot be kept small, the project should stop at v0.4 scheduler readiness and defer execution to a later major milestone.

## Candidate Scope

If approved, the first execution adapter should support one task-to-run path:

- Input: a task that already has a daemon-owned active lease and task attempt.
- Operation: bind the leased task attempt to one `RunRecord` through existing runtime APIs.
- Output: one run manifest, existing evidence artifacts, trace evidence, and terminal task/attempt/lease state.
- Execution mode: supervised local run path only.
- Concurrency: one task at a time.
- Queue behavior: no autonomous planning, no task decomposition, no background loop beyond explicit local daemon commands.

The adapter should start as a foreground or single-tick operation. A long-running unmanaged daemon loop should remain out of scope until a later plan explicitly authorizes process management and shutdown semantics.

## Explicit Non-Goals

This plan does not authorize:

- OpenAI API usage or `OPENAI_API_KEY`.
- Hosted fallback or paid API fallback.
- Raw model-provider integration.
- Generic shell access.
- Docker execution from queued tasks.
- Browser, email, calendar, broker, trading, external-message, or job-application actions.
- MCP/A2A adapters.
- Autonomous task planning or task decomposition.
- Unmanaged background scheduling.
- Direct edits to `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, or `secrets/`.
- Reading or exposing secrets, backend settings, environment variables, artifact contents, or private local files outside the existing harness runtime boundaries.

## Required Decisions Before Implementation

### 1. First Executable Task Type

Choose exactly one initial task type.

Recommended first candidate:

- `phase_1a_test` or another existing evidence-producing run type that already has bounded behavior and established manifest/artifact/trace coverage.

Rejected first candidates:

- Active repo write tasks.
- Docker tasks.
- Networked tasks.
- Hosted-provider tasks.
- Generic shell tasks.
- Tasks that require browser/email/calendar/broker actions.

Decision required:

- Which task type is allowed first?
- What task metadata is required before the daemon may execute it?
- What task metadata makes the daemon pause instead of execute?

### 2. Lease, Attempt, And Run Binding

The adapter must define a single authoritative state transition sequence:

1. Task is `leased` with an active daemon-owned `TaskLease`.
2. Existing `TaskAttempt` is linked to a new `RunRecord`.
3. Task transitions from `leased` to `running`.
4. Run completes through existing runtime flow.
5. Task attempt transitions to `succeeded` or `failed`.
6. Task transitions to `succeeded` or `failed`.
7. Lease is released with terminal metadata.

Decision required:

- Is `RunRecord.task_id` enough linkage, or does `TaskAttempt.run_id` become the primary join?
- Does task transition to `running` before or after `RunRecord` insertion?
- What exact recovery state is used if a process dies after creating the run but before updating the task attempt?

### 3. EffectivePolicy Enforcement

The adapter must resolve runtime `EffectivePolicy` before execution and persist policy evidence into the run manifest.

Minimum hard gates:

- `paid_provider = forbidden` blocks execution.
- `external_network = forbidden` blocks execution.
- `background_scheduling = forbidden` blocks unmanaged background work.
- `task_queue_execution = forbidden` must be explicitly handled by adapter-specific policy semantics before any queued task executes.

Approval gates:

- `hosted_boundary = approval_required`.
- `docker_execution = approval_required`.
- `active_repo_write = approval_required`.
- Any task `required_approvals`.

Decision required:

- Which policy keys are hard-forbidden for the first adapter?
- Which policy keys can be approval-gated?
- What stored evidence proves policy was checked before execution?

### 4. Approval Token Contract

Queued execution must not infer approval from metadata alone.

Decision required:

- What is an approval token or approval record?
- Where is it persisted?
- How is it linked to task id, attempt id, policy key, approver, and timestamp?
- Does an approval expire?
- Can approval be reused across retries?
- How are unresolved approvals represented in `daemon status`?

Default:

- No execution for `waiting_approval` tasks.
- No execution for approval-required policy keys until a typed approval record exists.

### 5. Sandbox And Data Boundary

The first adapter must preserve local/private data-boundary safeguards.

Decision required:

- What sandbox profile is required for the first task type?
- Is active repo write categorically blocked in the first adapter?
- Are artifact writes allowed only under existing run artifact directories?
- Are reads limited to the initialized project and existing runtime inputs?
- What evidence records sandbox profile and data boundary?

Default:

- Active repo writes are not allowed.
- Docker is not allowed.
- External network is not allowed.
- Artifact writes may occur only through existing run artifact APIs.

### 6. Backend Boundary

Codex remains a supervised external agent backend, not a raw model provider.

Decision required:

- Is the first adapter allowed to invoke Codex at all?
- If yes, what existing backend wrapper and run mode are used?
- What proof prevents raw OpenAI API usage, hosted fallback, paid fallback, and hidden provider routing?
- What backend descriptor fields are persisted in the manifest without exposing settings or secrets?

Default:

- Do not call Codex in the first execution adapter unless a separate implementation plan proves the supervised boundary is enforceable and tested.

### 7. Artifact, Manifest, And Trace Evidence

Execution must use existing evidence paths.

Required behavior:

- New runs write `harness.manifest/v1.1`.
- Manifest includes `task_id`, `objective_id` when present, EffectivePolicy evidence, backend descriptor hash when present, sandbox profile, validation results, and artifact evidence.
- Artifact records include schema version, checksum, size, producer, redaction state, and evidence status.
- Trace export remains OTEL-shaped and metadata-only.

Decision required:

- Which artifacts are produced by the first adapter?
- Which artifacts are required for success?
- What failures are terminal versus retryable?
- How is artifact drift handled for task success evidence?

### 8. Idempotency And Retry Semantics

The adapter must preserve task `idempotency_key`.

Decision required:

- Does a retry create a new `TaskAttempt` only after the previous attempt is terminal?
- Can a failed run be retried with the same idempotency key?
- How are duplicate daemon ticks prevented from creating duplicate runs for the same active lease?
- What stable error is returned when an attempt already has a run?

Default:

- One active lease can create at most one run.
- Duplicate ticks must renew/report the existing lease rather than create another run.

### 9. Crash Recovery

The adapter must define recovery for every intermediate state:

- Lease exists, no run exists.
- Run exists, task still `leased`.
- Task `running`, process exits.
- Attempt has `run_id`, run terminal, task not terminal.
- Lease expired while run is non-terminal.

Decision required:

- Which states are requeued?
- Which states require operator inspection?
- Which states become `failed`?
- How does `daemon recover` avoid corrupting an actively running supervised process?

Default:

- Ambiguous execution states should pause for operator inspection rather than retry automatically.

## Proposed Slice Breakdown If Approved

### Slice 1 — Execution Contract Models Only

- Add approval-record model/table if needed.
- Add attempt/run binding helpers.
- Add execution eligibility evidence model.
- Add tests for state transitions and duplicate-run prevention.
- No backend/tool execution.

### Slice 2 — Dry-Run Adapter

- Add a dry-run execution adapter that creates no real backend/tool side effects.
- It binds a lease to a run-like evidence record only if this can be done without invoking providers.
- It proves manifest/task/attempt/lease transitions and recovery paths.

### Slice 3 — One Real Bounded Adapter

- Implement only the approved first task type.
- Keep one task at a time.
- Enforce EffectivePolicy and approval records.
- Use existing run/artifact/trace APIs.
- No Docker, external network, hosted fallback, paid fallback, raw OpenAI API, generic shell, MCP/A2A, browser/email/calendar tools, or active repo write.

### Slice 4 — Release Hygiene

- Update docs and smoke checklist.
- Run focused tests and full regression.
- Verify no forbidden targets were modified.
- Commit a clean checkpoint.

## Required Test Plan Before Any Implementation

- Store tests for lease/attempt/run binding.
- Duplicate tick tests proving one lease cannot create duplicate runs.
- EffectivePolicy hard-gate tests.
- Approval-required pause tests.
- Missing approval rejection tests.
- Crash-recovery matrix tests.
- Manifest v1.1 task/objective/policy/artifact/trace evidence tests.
- Artifact checksum/size evidence tests.
- CLI smoke tests proving execution commands do not expose secrets or backend settings.
- Regression proving queue commands remain non-executing unless the explicit adapter command is invoked.
- Full suite: `pytest -q`.
- `git diff --check`.
- Forbidden target check for `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, and `secrets/`.

## Acceptance Criteria For Authorizing Implementation

Implementation should not start until this checklist is answered in a follow-on implementation plan:

- Exactly one first executable task type is selected.
- Approval records are specified or execution remains approval-free by construction.
- EffectivePolicy hard gates are enumerated.
- Sandbox profile is specified.
- Backend boundary is specified and does not use raw OpenAI API or hidden hosted fallback.
- TaskAttempt, TaskLease, RunRecord, RunManifest, artifact, and trace linkage is specified.
- Crash-recovery behavior is specified for every intermediate state.
- Duplicate-run prevention is specified and testable.
- Operator-visible JSON schemas and stable errors are specified.
- Safety boundaries are restated and testable.

## Recommended Next Action

Do not implement real backend or tool execution yet.

Slice 1 proves the local dry-run lease-to-run contract only. The next step should be a short decision document or issue that chooses whether to remain at dry-run evidence or authorize one real bounded adapter. If those answers stay small and local, write a follow-on implementation plan for exactly one real adapter. If they expand into backend/tool execution complexity, defer execution to v0.5.

## Slice 1 Completion Note

- `harness tasks add` accepts `--execution-adapter dry_run --task-type phase_1a_test` as metadata only.
- `harness daemon execute-dry-run <lease_id>` requires an existing active daemon lease and does not select work itself.
- The dry-run adapter links `TaskAttempt.run_id`, sets compatible `TaskRecord.run_id`, creates a local `phase_1a_test` run, registers metadata-only artifacts, marks the task/attempt succeeded, and releases the lease.
- The dry-run adapter does not call Codex, local model backends, Docker, shell tools, network, hosted providers, paid providers, or active repo write paths.

## Slice 2 Completion Note

- `harness daemon inspect-lease <lease_id>` is read-only and reports lease, task, attempt, run/manifest linkage, dry-run eligibility, and recovery recommendation.
- `harness daemon recover` reconciles existing dry-run evidence for completed or failed runs and handles expired active leases with non-terminal linked runs by failing closed for operator inspection.
- Recovery does not create another run for an attempt that already has `run_id`.
- Real backend/tool execution remains unauthorized.
