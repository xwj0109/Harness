# v0.4 Local Daemon Plan

## Summary

v0.4 introduces a conservative local daemon as a scheduler over the durable v0.3 task queue and v0.3.5 evidence control plane. The daemon should wake up locally, acquire one eligible task lease, renew or release that lease safely, record heartbeat/recovery evidence, and pause cleanly when dependencies, approvals, policy, or execution boundaries are not satisfied.

The daemon is not an agent loop and not a broad automation platform. It must not add autonomous planning, hosted fallback, paid fallback, OpenAI API usage, MCP/A2A adapters, browser/email/calendar tools, broker actions, external-message sends, generic shell access, or irreversible real-world actions.

## Scope Boundaries

### In scope

- Local daemon lifecycle commands:
  - `harness daemon start --project .`
  - `harness daemon stop --project .`
  - `harness daemon status --project . --output json`
  - `harness daemon run-once --project . --output json`
- Durable daemon state in initialized harness persistence.
- Heartbeat records and stale-daemon detection.
- One-task-at-a-time scheduling by default.
- Lease acquisition, renewal, expiry detection, and recovery.
- Approval-aware and policy-aware task gating.
- Attempt lifecycle evidence for selected tasks.
- Non-executing `run-once` in Slice 1, then tightly scoped execution adapters only after daemon control-plane safety is proven.
- Safety-smoke coverage for daemon non-execution, lease recovery, and approval pause behavior.

### Out of scope

- Background autonomy beyond local queue scheduling.
- Multi-task parallelism.
- Objective auto-planning.
- Task decomposition by the daemon.
- Hosted fallback, paid fallback, OpenAI API usage, or hidden provider routing.
- MCP/A2A, browser/email/calendar tools, generic shell, external-message sends, broker/trading actions, or job submissions.
- Active repo writes without an isolated edit/apply-back boundary and explicit approval.

## Target Behavior

The daemon should operate as a supervised local scheduler:

1. Start only for an initialized project.
2. Write daemon state and heartbeat evidence locally.
3. On each tick, inspect durable queue state.
4. Select only tasks that are eligible under state, dependency, approval, and EffectivePolicy gates.
5. Acquire or renew leases atomically.
6. Record task attempts and daemon decisions.
7. Pause tasks requiring approvals instead of executing or failing silently.
8. Recover safely after stale daemon heartbeats, expired leases, process restarts, SQLite contention, and interrupted attempts.
9. Produce inspectable JSON status without reading secrets or backend settings.

Initial daemon defaults:

- `max_concurrent_tasks = 1`.
- `lease_duration_minutes = 30`.
- `heartbeat_interval_seconds = 30`.
- `tick_interval_seconds = 10`.
- `owner = local_daemon:<hostname>:<pid>`.
- Daemon commands require initialized project state.
- `run-once` performs a single scheduler tick and exits.

## Implementation Slices

### Slice 1 — Daemon Control Plane, No Task Execution

Purpose: add daemon lifecycle and recovery primitives without executing task work.

Key changes:

- Add daemon persistence:
  - `DaemonRecord` with daemon id, owner, status, pid, started_at, heartbeat_at, stopped_at, project_root, and metadata.
  - `DaemonEvent` or equivalent event records for start, stop, heartbeat, tick, pause, lease renewal, stale recovery, and errors.
- Add SQLite tables additively, for example `daemon_records` and `daemon_events`.
- Add CLI:
  - `harness daemon run-once --project . --output json`
  - `harness daemon status --project . --output json`
  - `harness daemon stop --project . --output json`
- Keep `start` as either a foreground loop or a documented stub until Slice 2; do not create an unmanaged background process in Slice 1.
- `run-once` may acquire or renew leases and record decisions, but must not create runs, call backends, run Docker, execute tools, mutate active repo files, or create run artifacts.
- JSON schema:
  - `harness.daemon_status/v1`
  - `harness.daemon_tick/v1`

Acceptance criteria:

- `run-once` returns stable JSON showing daemon id, tick id, selected task if any, decision, lease/attempt ids, and pause reasons.
- Duplicate `run-once` calls do not select the same task concurrently.
- Stale daemon state can be detected and reported without killing arbitrary processes.
- Full suite passes.

### Slice 2 — Lease Renewal and Recovery

Purpose: make the daemon safe across restarts and interrupted work.

Key changes:

- Add lease renewal helper that only renews active leases owned by the current daemon owner.
- Add stale lease detection:
  - Expired active leases can be marked expired.
  - Tasks whose lease expired can return to `ready`, `blocked`, or `waiting_approval` based on dependencies and approvals.
  - Attempts linked to expired leases record failure or expired status metadata without creating runs.
- Add daemon heartbeat update and stale daemon reporting.
- Add recovery command:
  - `harness daemon recover --project . --output json`
- JSON schema:
  - `harness.daemon_recovery/v1`

Acceptance criteria:

- Expired leases are not treated as active.
- Recovery is idempotent.
- Recovery never retries terminal tasks automatically.
- Recovery preserves task idempotency keys and transition evidence.

### Slice 3 — Approval and Policy Gating

Purpose: ensure the daemon pauses safely before any future execution path exists.

Key changes:

- Reuse runtime EffectivePolicy and task required-approval metadata.
- Add a central daemon eligibility function that returns:
  - eligible.
  - blocked by dependencies.
  - waiting for approval.
  - forbidden by policy.
  - skipped because already leased/running/terminal.
- Tasks requiring approval transition or remain in `waiting_approval`.
- Forbidden tasks are not executed or retried by the daemon; they remain inspectable with a recorded daemon decision.
- Add `harness daemon status --json` fields for paused tasks and reasons.

Acceptance criteria:

- Approval-required tasks are paused, not failed.
- Policy-forbidden work is not leased for execution.
- Queue commands remain non-executing.
- Daemon status exposes enough state to debug without manual SQLite reads.

### Slice 4 — Minimal Execution Adapter Planning Gate

Purpose: decide whether v0.4 includes any execution adapter or stops at scheduler readiness.

Default decision:

- v0.4 should stop after scheduler/lease/recovery/approval safety unless a separate plan explicitly authorizes a minimal execution adapter.

If a later implementation plan authorizes execution, it must:

- Bind task attempts to `RunRecord`.
- Create run manifests and artifacts through existing runtime APIs.
- Use typed tool capability descriptors.
- Respect EffectivePolicy.
- Require approval for hosted boundary, Docker execution, active repo write, and external network.
- Keep Codex as a supervised external agent backend, not a raw model provider.

This v0.4 plan does not authorize implementation of task execution.

## Data and JSON Contracts

New durable records should be additive and compatible with initialized projects:

- `DaemonRecord`
- `DaemonEvent`
- Optional `DaemonTickResult` model for CLI output
- Optional `DaemonRecoveryResult` model for recovery output

All daemon JSON output must include:

- `schema_version`
- `ok`
- `daemon_id`
- `owner`
- `project_root`
- status or decision payload
- stable `errors` on failure

Daemon output must not include backend settings, API keys, environment variables, secret-like metadata, artifact contents, `.env*`, `*.pem`, `*.key`, external SQLite content, or files under `secrets/`.

## Test Plan

- Store tests:
  - Fresh initialization creates daemon tables additively.
  - Daemon records and events persist and round-trip.
  - Heartbeat update is durable.
  - Stale daemon detection is deterministic.
  - Lease renewal only works for the current owner.
  - Expired lease recovery is idempotent.
  - Recovery preserves idempotency keys and transition evidence.

- Scheduler tests:
  - `run-once` selects only eligible ready tasks.
  - Dependency-blocked tasks are skipped.
  - Approval-required tasks pause in `waiting_approval`.
  - Policy-forbidden tasks are skipped with evidence.
  - Duplicate `run-once` calls do not lease the same task.
  - Terminal tasks are never retried automatically.
  - No run records or run artifact directories are created by daemon control-plane slices.

- CLI tests:
  - `daemon run-once/status/stop/recover --output json` return stable schemas.
  - Commands require initialized projects.
  - Unknown daemon/task/lease references return stable JSON errors.
  - Daemon commands do not preflight Codex/local backends, run Docker, create runs, create artifacts, mutate active repo files, expose secrets, or inspect environment variables.

- Regression:
  - `pytest -q tests/test_sqlite_store.py tests/test_cli_smoke.py tests/test_evals_traces_v0_3_5.py`
  - `pytest -q`
  - `git diff --check`

## Exit Criteria

v0.4 is complete when:

- Operators can run one daemon scheduler tick safely with `harness daemon run-once`.
- Operators can inspect daemon status and events without reading SQLite manually.
- The daemon records heartbeat and recovery evidence locally.
- Lease renewal and expired lease recovery are idempotent and tested.
- Approval-required and policy-forbidden tasks pause with durable evidence.
- No daemon command executes tasks, calls backends, runs Docker, creates run artifacts, starts unmanaged background work, or exposes secrets in the control-plane slices.
- A separate implementation plan exists before any task execution adapter is added.

## Assumptions

- v0.3 queue hardening and v0.3.5 control-plane stabilization are complete.
- v0.4 starts as scheduler control-plane work, not execution work.
- Foreground or `run-once` operation is preferred before unmanaged background process behavior.
- Local daemon work should be safe on an 8 GB M1 MacBook and should default to one task at a time.
- Existing AGENTS.md restrictions remain binding.
