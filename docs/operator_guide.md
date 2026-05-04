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
- `pytest` missing in `python:3.12-slim`: use a project-specific image such as `harness-test:local`; the default Python image does not include project test dependencies.
- Editable install build isolation requiring network: set `install_project: true` and `install_project_no_build_isolation: true`; the generated in-container helper runs `python -m pip install -e . --no-deps --no-build-isolation`.
- Missing Git in the test image: temporary test repositories need `git`; `Dockerfile.harness-test` installs Git.
- Pytest collection warnings: collection warnings are test output, not sandbox errors. They appear in stdout/stderr summaries and artifacts.

Artifacts for each run include:

- `test_stdout.txt`
- `test_stderr.txt`
- `test_result.json`
- `events.jsonl`
- `transcript.jsonl`
- `final_report.md`

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
