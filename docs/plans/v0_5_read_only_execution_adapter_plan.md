# v0.5 Read-Only Execution Adapter Plan

Status: complete for v0.5 Slice 1; release hygiene is the active checkpoint.

## Summary

v0.5 starts from the completed v0.4.5 dry-run lease-to-run contract and authorizes exactly one real adapter: a daemon-held lease can execute `read_only_repo_summary` through the existing local read-only runner.

This milestone remains narrow. It does not authorize Codex execution, Docker, shell access, hosted fallback, paid fallback, OpenAI API usage, active repo writes, MCP/A2A, browser/email/calendar tools, autonomous planning, generic task execution, or unmanaged daemon loops.

## Authorized Adapter

The first v0.5 adapter is `read_only_summary` for task type `read_only_repo_summary`.

Required task metadata:

- `execution_adapter = "read_only_summary"`.
- `task_type = "read_only_repo_summary"`.

Execution command:

- `harness daemon execute-read-only <lease_id> --project . --output json`.

The command requires an existing active daemon lease and linked task attempt. It must not select work itself. `daemon run-once` remains lease-only and non-executing.

## Implemented Slice 1 — Read-Only Summary Lease Adapter

Slice 1 is implemented as the first v0.5 checkpoint:

- `harness tasks add` accepts the `read_only_summary/read_only_repo_summary` metadata pair.
- `harness daemon execute-read-only` binds an active lease to one `RunRecord`.
- `TaskAttempt.run_id` is the authoritative lease-to-run join.
- The task and attempt transition through `leased -> running -> succeeded` on success.
- The lease is released with sanitized run metadata.
- `daemon inspect-lease` reports read-only eligibility.
- `daemon recover` reconciles read-only linked-run evidence without creating another run.
- The read-only runner can execute inside an already-created run while preserving existing `harness run --task-type read_only_repo_summary` behavior.

## Slice 1 Completion Note

- `harness tasks add` accepts `--execution-adapter read_only_summary --task-type read_only_repo_summary` as sanitized task metadata.
- `harness daemon execute-read-only <lease_id>` requires an existing active daemon lease and does not select work itself.
- `TaskAttempt.run_id` is the authoritative lease-to-run join, with compatible `TaskRecord.run_id` and manifest `task_id` evidence.
- `daemon inspect-lease` reports read-only eligibility, and `daemon recover` reconciles read-only linked-run evidence without creating a second run.
- The adapter is gated to the configured local-only, no-cost `local_openai_compatible` backend and the existing read-only tools.
- The adapter does not authorize Codex execution, Docker, shell access, hosted fallback, paid fallback, OpenAI API usage, active repo writes, MCP/A2A, browser/email/calendar tools, generic task execution, or unmanaged daemon loops.

## Safety Contract

The v0.5 Slice 1 adapter may use only the configured `local_openai_compatible` backend when its descriptor remains local-only and no-cost:

- `billing_mode = local_no_api_cost`.
- `execution_location = local_machine`.
- `data_boundary = local_only`.
- `allow_network = false`.

The adapter may execute only existing read-only tools:

- `list_files`.
- `read_file`.
- `git_status`.
- `git_diff`.
- `final_answer`.

The adapter must reject unresolved approvals and metadata requesting active repo write, Docker, external network, hosted boundary, paid provider, generic shell, MCP/A2A, browser, email, or calendar capabilities.

CLI output and run evidence must not expose backend settings, `base_url`, API keys, environment variables, or secret-like data.

## Verification

Slice 1 verification:

- `pytest -q tests/test_sqlite_store.py tests/test_cli_smoke.py tests/test_runner_phase_1b.py`.
- `pytest -q tests/test_effective_policy_v0_3_5.py tests/test_tool_capabilities_v0_3_5.py tests/test_evals_traces_v0_3_5.py`.
- `pytest -q`.
- `git diff --check`.
- Forbidden target check for `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, and `secrets/`.

## Next Decisions

The next v0.5 work should be release hygiene for the read-only adapter checkpoint before authorizing any additional adapter.

Potential follow-on work must be planned separately:

- Read-only adapter hardening and additional recovery tests.
- Operator documentation and smoke checklist updates.
- A decision plan for the next adapter, if any.

The following remain unauthorized until a separate decision-complete plan is accepted:

- `repo_planning`.
- `simple_code_edit`.
- `codex_code_edit`.
- Docker task execution.
- Shell or generic command execution.
- Hosted or paid provider fallback.
- OpenAI API usage.
- MCP/A2A, browser, email, or calendar adapters.
- Active repo write from queued tasks.
