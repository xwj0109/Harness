# v0.3.5 Control-Plane Stabilization Plan

## Summary

v0.3.5 stabilizes the evidence and policy control plane before any v0.4 daemon work. The goal is to make runs, queued tasks, approvals, backend boundaries, artifacts, sandbox decisions, and safety checks comparable and replayable enough that future automation can rely on durable local evidence rather than implicit CLI behavior.

This milestone must not add daemon behavior, background scheduling, autonomous execution, hosted fallback, paid fallback, OpenAI API usage, MCP/A2A adapters, browser/email/calendar tools, broker actions, external-message sends, or generic shell access.

The first implementation target is **Slice 1: EffectivePolicy and manifest v1.1**. Later slices should build artifact immutability, typed tool descriptors, compare/baseline commands, safety-smoke evals, and trace export on top of that foundation.

## Key Changes

### Slice 1 — EffectivePolicy and manifest v1.1

- Add typed `PolicyLevel` and `EffectivePolicy` models.
- Resolve effective policy deterministically for initial subjects:
  - run task type and run mode.
  - backend descriptor boundary metadata.
  - built-in agent and workbench declarations where available.
  - queued task metadata when a task/objective link exists.
- Add `harness policy explain --subject-kind <kind> --subject-id <id> --project . --output json`.
- JSON output schema should be `harness.effective_policy/v1` and include `ok`, `subject_kind`, `subject_id`, `levels`, `sources`, `required_approvals`, `forbidden_reasons`, `monotonicity_checked`, and `resolved_at`.
- Upgrade `RunManifest` compatibly to `harness.manifest/v1.1`.
- Manifest v1.1 should keep existing v1 fields and add nullable/additive evidence fields:
  - `trace_id`.
  - `task_id`.
  - `objective_id`.
  - `effective_policy`.
  - `effective_policy_sha256`.
  - `backend_descriptor_sha256`.
  - `sandbox_profile`.
  - `validation_results`.
- Do not remove support for existing `harness.manifest/v1` tests without updating them to the compatible v1.1 contract.
- Do not make task queue commands execute, preflight backends, create runs, or create run artifacts.

### Slice 2 — Artifact evidence and immutability

- Add or upgrade artifact evidence with `schema_version`, `sha256`, `size_bytes`, producer metadata, and redaction state.
- Make registered artifact mutation detectable, and reject or clearly flag mismatched artifact evidence.
- Add JSON inspection output for run artifacts if missing.
- Keep artifact paths local and do not expose secret-like path contents.

### Slice 3 — Tool capability descriptors

- Add `ToolCapabilityDescriptor` for harness-controlled tools only.
- Capture tool id, input/output schemas where available, side-effect level, data boundary, sandbox requirement, approval requirement, idempotency behavior, replay policy, and allowed run modes.
- Cover initial harness-native capabilities: repo read, artifact read/write, isolated edit, diff inspection, secret scan, Docker test, policy explain, and approval request.
- Do not expose MCP, A2A, browser/email/calendar tools, networked tool execution, or generic shell as part of this slice.

### Slice 4 — Compare and baseline

- Add `harness compare <run_a> <run_b> --project . --output json`.
- Add `harness baseline set <run_id> --name <name> --project . --output json`.
- Add `harness baseline compare <run_id> --baseline <name> --project . --output json`.
- Compare policy hash, backend boundary, sandbox profile, artifact checksums, approvals, task/objective linkage, run status, and test-result evidence.
- Store baseline metadata locally through the harness runtime; do not use `.harness/` as a direct planning or edit target.

### Slice 5 — Safety-smoke evals and trace export

- Add `harness evals run --suite safety-smoke --project . --output json`.
- Add `harness traces export <run_id> --format otel-json --project . --output json`.
- Safety-smoke should fail on regressions in policy resolution, backend boundary handling, sandbox constraints, artifact evidence, apply-back safety, and task queue non-execution.
- Trace export should link run, task, objective, agent, backend, approval, sandbox, artifact, policy, and test evidence where present.

## Test Plan

- EffectivePolicy tests:
  - Policy resolution is deterministic for the same inputs.
  - Policy levels are monotonic: forbidden cannot become approval-required or allowed through a later source.
  - Hosted, networked, active-repo, Docker, and paid/provider boundaries remain forbidden or approval-required as declared.
  - `policy explain` returns stable JSON and does not preflight backends or inspect secrets.

- Manifest tests:
  - New runs write `harness.manifest/v1.1`.
  - Existing manifest evidence fields remain present and compatible.
  - Manifest v1.1 includes effective policy hash and backend descriptor hash without backend settings.
  - Task/objective/trace fields are nullable when no task context exists.
  - Golden evidence tests are updated to the v1.1 contract.

- Artifact/tool/compare tests:
  - Registered artifact checksum and size persist and round-trip.
  - Artifact mutation is rejected or reported as evidence mismatch.
  - Tool descriptors include side-effect, approval, sandbox, boundary, and replay metadata.
  - Compare/baseline detects policy, backend boundary, sandbox profile, artifact checksum, approval, task linkage, and test-result changes.

- Safety and regression tests:
  - Safety-smoke fails on known policy/sandbox/artifact regressions.
  - Trace export is schema-versioned and machine-readable.
  - Full suite passes with `pytest -q`.
  - `git diff --check` passes.

## Assumptions

- v0.3 queue hardening is complete and committed.
- v0.3.5 starts with EffectivePolicy and manifest v1.1 because compare, baseline, evals, and trace export need stable policy and manifest evidence.
- Existing `harness.spec_effective_preview/v1` remains a v0.2 read-only spec preview; it should not be silently rebranded as runtime EffectivePolicy.
- EffectivePolicy in Slice 1 is evidence and explanation, not broad runtime authorization for new execution paths.
- Manifest v1.1 is additive and compatible; it must not break existing initialized projects or current run flows.
- Daemon scheduling, lease renewal, task execution from the queue, MCP/A2A adapters, hosted fallback, paid fallback, and generic shell access remain out of scope until after v0.3.5 exit criteria pass.
