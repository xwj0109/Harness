# v0.1 Hardening Plan

## Goal

Finish the safety-kernel hardening work before adding generic agent, workbench, task queue, or daemon abstractions. The result should make current runs more explicit, inspectable, machine-readable, and easier to verify without weakening local-first safeguards.

## 1. Missing Types And Contracts

Expected behavior:
- Add a typed `RunMode` concept for supported run modes, keeping existing task-type routing compatible.
- Add a `BackendDescriptor` contract that captures backend identity, metadata, capabilities, and operator-relevant constraints.
- Preserve current backend safety semantics, especially Codex as a supervised external agent backend and local-compatible endpoints as local-only only when configured that way.

Acceptance criteria:
- Existing backend metadata tests still pass.
- New tests cover valid and invalid run modes and backend descriptors.
- No paid API or hosted fallback behavior is introduced.

## 2. Run Manifest Generation

Expected behavior:
- Every run artifact directory gets a `manifest.json`.
- The manifest indexes run identity, task type, run mode when available, backend metadata, approval id when present, artifact paths, status, and timestamps.
- Manifest writing should be part of the normal run lifecycle and should not replace existing SQLite persistence or artifact files.

Acceptance criteria:
- New and existing run flows create a manifest.
- Manifest content is deterministic enough for golden tests after normalizing timestamps and ids.
- Missing or failed optional artifacts are represented clearly.

## 3. Stable Machine-Readable CLI Output

Expected behavior:
- Add stable JSON/JSONL output options for commands that expose run, backend, approval, and test results.
- Keep current human-readable output as the default.
- Use structured serializers rather than ad hoc string parsing.

Acceptance criteria:
- CLI tests cover human-readable defaults and machine-readable modes.
- JSON output validates as JSON.
- JSONL output emits one valid JSON object per line where applicable.

## 4. Security Documentation

Expected behavior:
- Add `SECURITY.md` with the project security model, supported local/private boundaries, forbidden paths, approval gates, and non-goals.
- Include a threat model for hosted-boundary approvals, isolated edit workspaces, apply-back validation, Docker test execution, secret-path blocking, and secret scanning.
- Explicitly document that OpenAI API usage, paid API fallback, hosted fallback, secret exposure, live trading, and automatic external sends are out of scope.

Acceptance criteria:
- Documentation matches current behavior and roadmap constraints.
- Existing operator docs link to the security document where relevant.
- Tests are not required for docs-only security text unless documentation consistency tests are added.

## 5. `harness doctor`

Expected behavior:
- Add a non-mutating readiness command that checks initialization, config loadability, backend preflight status, Docker availability, managed Dockerfile status, ignored local artifact paths, and key safety settings.
- The command should report clear pass/warn/fail statuses without writing `.harness/` state or changing repo files.
- Human-readable output should be default, with machine-readable output following the stable CLI output design.

Acceptance criteria:
- Doctor succeeds in initialized projects with expected local constraints.
- Doctor reports actionable failures for missing config, unavailable Docker, invalid Dockerfile, or unavailable backends.
- Tests verify the command does not mutate project state.

## 6. Golden End-To-End Tests

Expected behavior:
- Add golden tests for the main safety flows after manifests and structured output are stable.
- Normalize volatile values such as paths, timestamps, run ids, and approval ids.
- Cover initialization, read-only run artifacts, backend listing/preflight serialization, Docker test denial or mocked execution, and Codex planning/edit refusal paths where feasible.

Acceptance criteria:
- Golden tests are deterministic on local development machines and CI-like environments.
- The tests prove core evidence artifacts are present and structured.
- Existing tests continue to pass.
