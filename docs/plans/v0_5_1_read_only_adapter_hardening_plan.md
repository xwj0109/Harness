# v0.5.1 Read-Only Adapter Hardening Plan

Status: complete as a hardening checkpoint for the existing v0.5 read-only adapter.

## Summary

v0.5.1 hardens the existing `read_only_summary/read_only_repo_summary` lease adapter without adding a new adapter or expanding execution scope.

The milestone focuses on failure evidence, recovery coverage, inspection clarity, and operator troubleshooting for `harness daemon execute-read-only`.

## Scope

In scope:

- Backend preflight failure behavior before run creation.
- Runner failure behavior after run creation.
- Duplicate execution rejection when a lease is released or an attempt already has `run_id`.
- Read-only recovery coverage for completed, failed, and expired linked-run states.
- Eligibility evidence for `daemon inspect-lease`.
- Operator troubleshooting docs.

Out of scope:

- Additional execution adapters.
- Codex execution from the queue.
- Docker execution from the queue.
- Shell access.
- Hosted fallback or paid fallback.
- OpenAI API usage.
- Active repo writes.
- MCP/A2A, browser, email, or calendar tools.
- Generic queued task execution.
- Unmanaged daemon loops.

## Hardening Requirements

- `daemon execute-read-only` must leave task, attempt, lease, and run state unchanged if the local backend is unavailable before run creation.
- If a run has been created and the read-only runner fails, the run, task, and attempt must become failed and the lease must be released.
- `TaskAttempt.run_id` remains the authoritative lease-to-run join.
- `daemon recover` must reconcile read-only linked-run evidence without creating a second run.
- `daemon inspect-lease` must show read-only eligibility and linked run/manifest evidence without mutation.
- Outputs and evidence must not expose backend settings, `api_key`, `OPENAI_API_KEY`, `base_url`, environment variables, secret-like data, or artifact contents.

## Verification

- Focused tests:
  - `pytest -q tests/test_sqlite_store.py tests/test_cli_smoke.py tests/test_runner_phase_1b.py`
- Safety/regression tests:
  - `pytest -q tests/test_effective_policy_v0_3_5.py tests/test_tool_capabilities_v0_3_5.py tests/test_evals_traces_v0_3_5.py`
  - `pytest -q`
  - `git diff --check`
- Manual inspection:
  - `git status --short`
  - `git diff --name-only`
  - Confirm no tracked edits touch `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, or `secrets/`.

## Completion Note

- Backend preflight failure before run creation is covered and leaves task, attempt, lease, and run state unchanged.
- Runner failure after run creation is covered and marks run, task, and attempt failed while releasing the lease.
- Duplicate execution, existing attempt/run linkage, unresolved approvals, forbidden metadata, and unsafe backend descriptors are covered.
- Read-only recovery now has parity coverage for completed, failed, and expired non-terminal linked-run states.
- Operator troubleshooting docs describe inspect/recover paths without authorizing new execution behavior.
