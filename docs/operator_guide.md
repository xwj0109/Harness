# Operator Guide

This guide covers the currently implemented operator flows:

- supervised isolated `codex_code_edit`;
- direct Docker-sandboxed test execution;
- model-visible Docker `run_tests` for `simple_code_edit`.

The harness does not commit or push changes for these flows. Paid API execution, generic shell execution, workflows, plugins, MCP, browser/email/calendar integrations, hosted fallback, and local fallback are outside the implemented scope.

## Codex Supervised Isolated Editing

`codex_code_edit` uses `CodexCliBackend` as an external agent backend. Codex does not run as a raw model provider, and the harness does not assume Codex internal actions appear as harness-native tool calls. Supervision is done through workspace isolation, Codex subprocess flags, captured output, artifacts, git status, diff inspection, policy validation, and explicit apply-back approval.

Create the required hosted data-boundary approval profile before running an edit:

```bash
harness approvals add --backend codex_cli --data-boundary hosted_provider --project . --task-types codex_code_edit --duration-days 1
```

Run an isolated edit:

```bash
harness run "Modify only scratch_codex_edit.py. Add a docstring inside greet(). Do not create, delete, or modify any other files." --project . --task-type codex_code_edit --keep-isolation
```

Approval behavior:

- Codex edits only an isolated workspace, not the active project.
- The active project remains unchanged until apply-back approval.
- After Codex exits, the harness inspects the isolated diff.
- The operator can view the full diff, deny all changes, or approve all validated changes.
- Denial leaves the active project unchanged.
- Approval applies only the inspected, sanitized, validated diff.
- Apply-back is not based on Codex final messages, stdout, stderr, or events.
- No commit or push is performed.

First-version file-change policy:

- Allowed: modifications to existing text files.
- Rejected: file creation, deletion, rename, binary changes, symlink changes, secret-like paths, `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, `secrets/`, `.venv/`, `node_modules/`, `data/raw/`, and other blocked paths.
- Generated/local artifacts such as `*.egg-info/`, `.DS_Store`, `__pycache__/`, `.pytest_cache/`, `.mypy_cache/`, `dist/`, and `build/` are ignored for apply-back and do not block valid source-file modifications.

Common outcomes:

- `HostedBoundaryApprovalRequired`: no valid hosted data-boundary project approval profile exists for `codex_code_edit`.
- Dirty repo refusal: the active repository has uncommitted changes and the run refuses by default.
- Codex CLI capability refusal: an edit-capable command cannot be constructed safely for the installed Codex CLI.
- Policy violation: the isolated diff includes unsupported or blocked changes.
- Completed denied: Codex completed and the operator denied apply-back; the active project remains unchanged.
- Completed applied: Codex completed, the operator approved apply-back, freshness checks passed, and the validated diff was applied.

Notes:

- If `AGENTS.md` is missing, the harness warns and recommends creating one, but does not auto-create it.
- If the installed `codex exec` does not expose an internal approval flag, the harness reports that Codex internal command approval was not enforceable and relies on isolated workspace execution plus explicit apply-back approval.
- Network isolation for Codex subprocesses is only claimed when the installed CLI exposes an enforceable network-control flag.

## Direct Docker Test CLI

The direct Docker test CLI runs tests inside a sanitized temporary workspace. The active project root is never mounted into Docker.

Recommended `.harness/config.yaml` sandbox section for local harness validation:

```yaml
sandbox:
  image: "harness-test:local"
  image_build_file: "Dockerfile.harness-test"
  network: false
  timeout_seconds: 120
  memory_limit: "2g"
  cpu_limit: 2
  workdir: "/workspace"
  install_project: true
  install_project_no_build_isolation: true
```

Build the local test image:

```bash
harness tests image build --project .
```

The managed build command validates the configured Dockerfile and runs `docker build` with subprocess argument lists. It is a direct CLI operation only; test execution never auto-builds images. To create or validate the managed Dockerfile:

```bash
harness tests image generate --project .
harness tests image validate --project .
```

The equivalent raw Docker command is:

```bash
docker build -f Dockerfile.harness-test -t harness-test:local .
```

Run tests through the harness:

```bash
harness tests run --project . -- python -m pytest -q
```

Approval behavior:

- Every test execution requires per-run approval.
- Denial records `execution_denied` and does not call `docker run`.
- There is no auto-approval flag.

Isolation behavior:

- The harness creates a sanitized temporary workspace outside the active project.
- Only the temporary workspace is mounted into Docker at `/workspace`.
- The active project root is not mounted.
- Network is disabled by default with Docker no-network mode.
- Host environment variables are not passed into the container.
- The container is not privileged, does not use host networking, and does not mount the Docker socket.
- The temporary workspace is cleaned after execution or denial.

Excluded from the temporary workspace:

```text
.git/
.harness/
.venv/
node_modules/
data/raw/
secrets/
.env
.env*
*.pem
*.key
*.sqlite
*.db
*.egg-info/
.DS_Store
__pycache__/
.pytest_cache/
.mypy_cache/
dist/
build/
```

Common outcomes:

- `docker_unavailable`: Docker is not installed, not on `PATH`, or not reachable by the local Docker CLI.
- `docker_image_missing`: `docker image inspect <image>` failed. Pull or build the configured image manually.
- `execution_denied`: the operator denied execution.
- `tests_failed`: the command exited nonzero, including missing `pytest` or missing project dependencies.
- `tests_timed_out`: the Python harness timeout expired and the container was stopped.
- `tests_passed`: the command exited with status `0`.

Troubleshooting:

- Docker not on `PATH`: verify `docker --version` works in the same terminal environment.
- Image missing: run `docker build -f Dockerfile.harness-test -t harness-test:local .` or `docker pull <configured-image>`.
- Managed image missing: run `harness tests image build --project .`; the harness does not auto-build during test execution.
- `pytest` missing in `python:3.12-slim`: use a project-specific image such as `harness-test:local`; the default Python image does not include project test dependencies.
- Editable install build isolation requiring network: set `install_project: true` and `install_project_no_build_isolation: true`; the generated in-container helper runs `python -m pip install -e . --no-deps --no-build-isolation`.
- Missing Git in the test image: temporary test repositories need `git`; `Dockerfile.harness-test` installs Git.
- Pytest collection warnings: collection warnings are test output, not sandbox errors. They appear in stdout/stderr summaries and artifacts.

The `failure_guidance` field in `test_result.json` gives short operator hints for common dependency cases such as missing `pytest`, missing project imports, missing dependencies, and editable install failures.

Artifacts for each run include:

- `test_stdout.txt`
- `test_stderr.txt`
- `test_result.json`
- `events.jsonl`
- `transcript.jsonl`
- `final_report.md`

## Read-Only v0.2 Spec Inspection

The v0.2 spec commands expose declarative model profiles, tool policies, memory scopes, agents, and workbenches. They are operator inspection surfaces only. They do not register, persist, activate, execute, schedule, route, or preflight agents.

```bash
harness specs
harness specs --output json
harness specs agent repo_inspector
harness specs agent repo_inspector --output json
harness specs workbench coding
harness specs workbench coding --output json
```

Built-in inspection reads only the in-memory built-in registry. JSON output is schema-versioned:

- `harness.spec_registry/v1`
- `harness.agent_spec/v1`
- `harness.workbench_spec/v1`

## Read-Only Custom Spec Validation

Custom bundles must be explicit JSON or YAML files with a top-level schema version:

```yaml
schema_version: harness.spec_bundle/v1
```

Validate a custom bundle:

```bash
harness specs validate path/to/specs.json
harness specs validate path/to/specs.json --output json
harness specs validate path/to/specs.yaml --output json
```

The `validate` command reads only the explicit file path provided by the operator and validates it against the declarative spec registry schema. It supports `.json`, `.yaml`, and `.yml` only.

Validation failures are returned as stable JSON when `--output json` is used:

```json
{
  "schema_version": "harness.spec_validation/v1",
  "ok": false,
  "path": "/absolute/path/to/specs.json",
  "errors": [
    "Spec bundle missing schema_version."
  ]
}
```

Unsupported schema versions are also rejected safely:

```json
{
  "schema_version": "harness.spec_validation/v1",
  "ok": false,
  "path": "/absolute/path/to/specs.json",
  "errors": [
    "Unsupported spec bundle schema_version: harness.spec_bundle/v0"
  ]
}
```

Custom bundle paths are guarded before file contents are read. Paths under or matching `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, and `secrets/` are rejected.

## Normalized Spec Export

Export the built-in registry or an explicit custom bundle in a stable JSON shape:

```bash
harness specs export --source builtin --output json
harness specs export --source path/to/specs.yaml --output json
```

The JSON wrapper is `harness.spec_export/v1`:

```json
{
  "schema_version": "harness.spec_export/v1",
  "source": {
    "kind": "builtin",
    "path": null
  },
  "registry": {
    "agents": {},
    "memory_scopes": {},
    "model_profiles": {},
    "tool_policies": {},
    "workbenches": {}
  }
}
```

For custom bundles, `source.kind` is `custom` and `source.path` is the absolute explicit path.

## Registry Diff

Compare the built-in registry with an explicit custom bundle:

```bash
harness specs diff --source path/to/specs.yaml --output json
```

The JSON wrapper is `harness.spec_diff/v1`. Each registry section reports deterministic `added`, `removed`, `changed`, and `unchanged` id lists:

```json
{
  "schema_version": "harness.spec_diff/v1",
  "source": {
    "base": {
      "kind": "builtin",
      "path": null
    },
    "compare": {
      "kind": "custom",
      "path": "/absolute/path/to/specs.yaml"
    }
  },
  "diff": {
    "agents": {
      "added": [],
      "removed": [],
      "changed": [],
      "unchanged": []
    }
  }
}
```

Diff is structural and declarative. It does not explain semantic impact or activate custom specs.

## Spec Effective Policy Preview

Preview resolved policy relationships for one agent or one workbench:

```bash
harness specs preview agent repo_inspector --output json
harness specs preview workbench coding --output json
harness specs preview agent repo_inspector --source path/to/specs.yaml --output json
harness specs preview workbench coding --source path/to/specs.yaml --output json
```

The JSON wrapper is `harness.spec_effective_preview/v1`. Agent previews include the agent declaration plus resolved model profile, tool policy, memory scope, and parent id. Workbench previews include the workbench declaration, default model profile, allowed agents with resolved references, forbidden actions, and workbench-local declarative policy maps.

Effective preview is not runtime permission enforcement. It does not execute agents, check backend availability, route work, create tasks, or persist custom specs.

## Runtime Effective Policy Evidence

Explain runtime policy evidence for persisted harness subjects:

```bash
harness policy explain --subject-kind run --subject-id run_abc123def456 --project . --output json
harness policy explain --subject-kind task --subject-id task_abc123def456 --project . --output json
harness policy explain --subject-kind agent --subject-id repo_inspector --project . --output json
harness policy explain --subject-kind workbench --subject-id coding --project . --output json
harness policy explain --subject-kind backend --subject-id codex_cli --project . --output json
```

The JSON wrapper is `harness.effective_policy/v1`. Runtime policy evidence includes policy levels, sources, required approvals, forbidden reasons, a deterministic policy hash, and subject identity. It is an evidence and explanation surface only; it does not grant permissions, execute agents, preflight backends, run Docker, create runs, create artifacts, mutate tasks, or start schedulers.

Run manifests are written as `harness.manifest/v1.1` and include additive runtime policy evidence such as `effective_policy`, `effective_policy_sha256`, and backend descriptor hash when a backend descriptor exists. Manifest evidence does not include backend settings, API keys, environment variables, or secret-like metadata.

## Artifact Evidence

Inspect registered run artifact evidence without printing artifact contents:

```bash
harness artifacts list run_abc123def456 --project . --output json
harness artifacts inspect art_abc123def456 --project . --output json
```

The JSON wrappers are `harness.artifacts/v1` for list output and `harness.artifact/v1` for inspect output. Artifact evidence includes local path, kind, producer metadata, redaction state, persisted `sha256`, persisted `size_bytes`, and current evidence status.

Evidence status values are:

```text
verified
mismatch
missing
unknown
```

Artifact inspection recomputes checksum and size to report evidence drift, but it does not repair, rewrite, delete, or expose artifact file contents. A mismatch means the current local file no longer matches the checksum and size recorded when the artifact was registered.

## Tool Capability Descriptors

Inspect harness-native tool capability metadata:

```bash
harness tools list --project . --output json
harness tools inspect repo_read --project . --output json
```

The JSON wrappers are `harness.tool_capabilities/v1` for list output and `harness.tool_capability/v1` for inspect output. Tool descriptors include input/output schema sketches, side-effect level, data boundary, approval requirements, sandbox requirement, replay policy, allowed run modes, and related policy keys.

Descriptors are control-plane metadata only. They do not grant permissions, execute tools, preflight backends, run Docker, create runs, create artifacts, mutate tasks, or start schedulers. Generic shell, MCP, A2A, browser, email, calendar, hosted fallback, paid fallback, and networked arbitrary execution are not exposed as tool descriptors in v0.3.5.

## v0.2 Specs Safety Boundary

All `harness specs ...` commands are read-only inspection commands. They do not auto-discover spec files, read or write `.harness/`, read project config, read SQLite, inspect environment variables, read backend settings, read secrets, create tasks, execute agents, preflight backends, run Docker, start schedulers, or change project state.

## Manual Objectives and Task Queue

The v0.3 queue stores operator-created objective and task records in the initialized project database at `.harness/harness.sqlite`. It is a manual queue only: objectives can group work, tasks can be created, listed, inspected, moved through statuses, and selected with `run-next`, but no objective or task command executes agents, calls a backend, runs Docker, starts a scheduler, or creates background work.

Initialize the project before using the queue:

```bash
harness init --project .
```

Create and inspect objectives:

```bash
harness objectives add --title "Queue hardening" --workbench coding --project . --output json
harness objectives list --project . --output json
harness objectives inspect obj_abc123def456 --project . --output json
```

Objective commands use stable JSON wrappers:

- `harness.objective/v1` for add and inspect output.
- `harness.objectives/v1` for list output.

Objectives are metadata only in v0.3. They do not create tasks automatically and do not imply planning, routing, backend execution, scheduling, or autonomy.

Create and inspect tasks:

```bash
harness tasks add --title "Inspect repository" --agent repo_inspector --workbench coding --project . --output json
harness tasks add --title "Review queue plan" --objective obj_abc123def456 --depends-on task_abc123def456 --project . --output json
harness tasks list --project . --output json
harness tasks list --objective obj_abc123def456 --project . --output json
harness tasks inspect task_abc123def456 --project . --output json
harness tasks graph --objective obj_abc123def456 --project . --output json
harness tasks status task_abc123def456 succeeded --project . --output json
harness tasks cancel task_abc123def456 --project . --output json
harness tasks retry task_abc123def456 --project . --output json
```

Task commands use stable JSON wrappers:

- `harness.task/v1` for add, inspect, and status updates.
- `harness.tasks/v1` for list output.
- `harness.task_graph/v1` for graph output.
- `harness.task_run_next/v1` for manual next-task selection.

Task records may store declarative built-in registry ids:

- `workbench_id`, from `--workbench`.
- `agent_id`, from `--agent`.
- `objective_id`, from `--objective`.
- `depends_on`, from repeated `--depends-on`.
- `required_approvals`, from repeated `--requires-approval`.
- `spec_source_kind: builtin` when registry ids are attached.

These ids are metadata only in v0.3. They do not route work or imply backend execution. Dependencies are persisted locally and can make a task `blocked`; required approvals are recorded locally and can make a task `waiting_approval`. v0.3.5 runtime policy explanation can summarize this metadata, but it remains non-executing evidence rather than authorization for autonomous work.

Select the next runnable task manually:

```bash
harness tasks run-next --project . --output json
```

`run-next` selects the highest-priority, oldest ready task whose dependencies are complete, creates a local task attempt and active lease, marks it `leased`, and returns the task, attempt, and lease. If no task is runnable, it returns `ok: true` with `selected_task: null`, `attempt: null`, and `lease: null`. It does not create a run record, create run artifacts, call a backend, execute tools, or mutate repository files outside the harness SQLite database.

Task statuses are:

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

Legacy stored or input task statuses are compatibility-mapped as `queued -> ready`, `completed -> succeeded`, and `canceled -> cancelled`.

## v0.3 Task Queue Safety Boundary

Task queue commands require initialized local project state and may read or write `.harness/harness.sqlite`. They do not read environment variables, backend settings, secrets, `.env*`, `*.pem`, `*.key`, `*.sqlite` outside the harness database, or `secrets/`. They do not add hosted fallback, paid fallback, OpenAI API usage, browser/email/calendar automation, broker actions, trading actions, external message sends, application submission, daemon behavior, scheduling, or autonomous background work.

## Model-Visible Docker `run_tests` For `simple_code_edit`

`run_tests` is available only inside the local/native `simple_code_edit` model loop. It is rejected by default in protocol parsing, rejected for `read_only_repo_summary`, unavailable to Codex `repo_planning`, and not exposed to `codex_code_edit`.

Model command shape:

```json
{
  "command": "run_tests",
  "arguments": {
    "command": ["python", "-m", "pytest", "-q"],
    "cwd": "optional/relative/dir"
  }
}
```

`arguments.command` must be a non-empty list of strings. Shell strings and shell metacharacter tokens are rejected. If provided, `cwd` must be project-relative, resolve inside the active project, and point to an existing directory. Inside Docker, `cwd` maps under `/workspace`.

Observation shape returned to the model:

```json
{
  "tool": "run_tests",
  "status": "tests_passed",
  "exit_code": 0,
  "timed_out": false,
  "failure_hint": "",
  "stdout_summary": "...",
  "stderr_summary": "...",
  "artifacts": {
    "stdout": "...",
    "stderr": "...",
    "result": "..."
  },
  "next_guidance": "Tests passed. Provide final_answer unless more changes are required."
}
```

The simple edit loop supports patch/test/fix/final cycles:

```text
apply_patch -> run_tests -> targeted apply_patch -> run_tests -> final_answer
```

Restrictions:

- `run_tests` is Docker-only.
- There is no host execution fallback.
- Shell strings, `/bin/sh -c`, and generic shell commands are not supported.
- Test execution requires per-execution approval.
- `run_tests` is not exposed to Codex routes.
- `run_tests` remains rejected for `read_only_repo_summary` and unavailable to `repo_planning`.
- Nonzero test exits are returned as `tests_failed` observations, not harness crashes.

Multiple test executions in one simple edit run use non-clobbering artifacts:

- first execution: `test_stdout.txt`, `test_stderr.txt`, `test_result.json`;
- second execution: `test_stdout_2.txt`, `test_stderr_2.txt`, `test_result_2.json`;
- later executions continue with numeric suffixes.
