# Smoke Checklist

Use this checklist after changes to the isolated Codex edit route, the Docker test runner, or the simple edit loop. Commands assume the repository root is the current directory.

## Inspect Repository State

```bash
git status --short
git log --oneline --decorate -5
```

## Run Local Unit Suite

```bash
pytest -q
```

## Verify Packaging and Distribution

```bash
rm -rf /tmp/harness-wheel /tmp/harness-install /tmp/harness-package-project
python3 -m pip wheel --no-deps --no-build-isolation -w /tmp/harness-wheel .
python3 -m venv --system-site-packages /tmp/harness-install
/tmp/harness-install/bin/python -m pip install --no-deps /tmp/harness-wheel/agent_harness-*.whl
/tmp/harness-install/bin/harness --help
/tmp/harness-install/bin/harness --project /tmp/harness-package-project --output json
/tmp/harness-install/bin/harness home --project /tmp/harness-package-project --output json
/tmp/harness-install/bin/harness specs --output json
/tmp/harness-install/bin/harness quickstart agent --project /tmp/harness-package-project --output json
/tmp/harness-install/bin/harness doctor --release --project /tmp/harness-package-project --output json
```

Expected packaging properties:

- The installed wheel exposes the `harness` console script.
- Package metadata reports version `1.5.0`.
- Packaged built-in YAML specs under `harness/builtin_specs/` are available after wheel install.
- `harness --output json`, `harness home`, and `harness quickstart agent` remain non-mutating in the temporary project.
- `harness doctor --release --output json` reports release-readiness metadata without backend/provider preflight.
- Textual is a normal dependency for the installed app. `harness --output json` is a non-interactive probe and must not launch the terminal UI.
- The packaging smoke does not preflight backends, call providers, run Docker, create tasks, acquire leases, create runs, execute adapters, expose secrets, or use hosted/paid fallback.

## Verify v1.5 Registered Adapter Path

This smoke path exercises the declarative agent lifecycle, project-local import, manual queue metadata, daemon lease inspection, and the bounded read-only adapter. Replace `task_lease_...` with the lease id returned by `daemon run-once`.

```bash
rm -rf /tmp/harness-v1-agent
harness agents scaffold smoke_v1_agent \
  --workbench quant \
  --kind specialist \
  --parent quant_research \
  --model-profile local_reasoning \
  --tool-policy read_only \
  --memory-scope quant \
  --output /tmp/harness-v1-agent \
  --output-format json
harness agents validate /tmp/harness-v1-agent --output json
harness agents preview /tmp/harness-v1-agent --output json
harness init --project .
harness agents import /tmp/harness-v1-agent --project . --output json
harness agents inspect smoke_v1_agent --project . --output json
harness agents preview-imported smoke_v1_agent --project . --output json
harness tasks add --title "v1 read-only summary" \
  --agent smoke_v1_agent \
  --workbench quant \
  --execution-adapter read_only_summary \
  --task-type read_only_repo_summary \
  --project . \
  --output json
harness daemon run-once --project . --output json
harness daemon inspect-lease "$LEASE_ID" --project . --output json
harness daemon execute-read-only "$LEASE_ID" --project . --output json
```

Expected v1.5 safety properties:

- Agent and task lifecycle commands are declarative/control-plane operations only.
- `daemon run-once` leases work but does not execute it.
- `daemon execute-read-only` uses only the configured Codex CLI subscription route in read-only sandbox mode and requires hosted-boundary approval for `read_only_repo_summary`.
- The MVP read-only path does not authorize Codex execution from the queue, Docker-from-queue, generic shell, hosted fallback, paid fallback, OpenAI API usage, MCP/A2A, browser/email/calendar tools, broker actions, live trading, order placement, active repo writes, external messaging, application submission, or autonomous workflows.

## Verify Operator Cockpit

Replace `TASK_ID`, `LEASE_ID`, and `ARTIFACT_ID` with ids produced by the v1.5 registered adapter smoke path when checking inspect text output.

```bash
harness --project . --output json
harness home --project . --output json
harness quickstart agent --project . --output json
harness --project . --plain
harness --project . --plain --codex-like
harness home --project .
harness quickstart agent --project .
harness runs --project .
harness tasks list --project .
harness agents list --project .
harness daemon status --project .
harness agents inspect smoke_v1_agent --project .
harness tasks inspect "$TASK_ID" --project .
harness daemon inspect-lease "$LEASE_ID" --project .
harness policy explain --subject-kind task --subject-id "$TASK_ID" --project .
harness artifacts inspect "$ARTIFACT_ID" --project .
```

Expected operator cockpit safety properties:

- Bare `harness` is the single primary app surface. It combines passive dashboard context with the chat/orchestrator prompt in one Textual terminal app. `harness --plain` is a line-oriented fallback for tests/dev use, not a separate product surface.
- `harness home` is read-only and does not initialize projects, import agents, create tasks, create runs, create artifacts, acquire leases, mutate daemon state, or execute adapters.
- `harness --output json` is read-only and returns `harness.chat/v1` context without launching a prompt, preflighting backends, calling providers, touching Docker, or initializing `.harness/`.
- The unified app and `--plain` fallback keep session state in memory only. `/help`, `/init`, `/mode`, `/home`, `/dashboard`, `/orchestrators`, `/use`, `/agents`, `/tasks`, `/adapters`, and `/quit` should work without traceback on an uninitialized project. `/init` is the explicit in-app setup path; `harness --output json`, dashboard refresh, and passive slash commands must not initialize. Task creation, orchestrated graph creation, lease acquisition, and registered-adapter dispatch require explicit confirmation and use the normal objective, task, daemon run-once, and daemon execute paths.
- In normal mode, chat drafts before confirmation. In `--codex-like` or `/mode codex-like`, one explicit confirmation may create the approved task/objective graph and run it in the foreground through registered adapters. Missing hosted-boundary approval should be offered as an explicit in-app approval step; apply-back remains separate and denied by default.
- Chat-first orchestration should draft the full objective/task graph before creation. The foreground `/run` path may drive only the approved graph through `daemon run-once` and `daemon execute`; it must stop on blocked dependencies, rejection, missing hosted approval, operator `/stop`, or terminal graph completion.
- The dashboard side renders a light-theme chat-style interface, project state, summary counts, imported agents, task details, active lease details, daemon event summaries, recent runs, safety reminders, local in-memory search over loaded dashboard/command metadata, in-memory section collapse, palette-only search focus, and a copy-only command palette without initializing projects, importing agents, creating tasks, creating runs, creating artifacts, acquiring leases, mutating daemon state, executing adapters, crawling files, or searching artifact contents.
- The slash-command and command-palette surfaces show grouped command templates, mutation/safety notes, and selected command text for manual use only. They must not execute commands, spawn subprocesses, invoke a shell, copy to the clipboard, run daemon actions, execute adapters, preflight backends, run Docker, call providers, or expose artifact file contents.
- The TUI layout keeps chat and dashboard context in stable read-only sections, shows keyboard/navigation hints for `/`, `escape`, `tab`, `shift+tab`, `ctrl+p`/`F2`, prompt-unfocused `c`, prompt-unfocused `shift+c`, `enter`, and `ctrl+q`, reports no-match states, and displays only static generated terminal pixel art without persisting preferences, loading image files at runtime, mutating harness state, or adding command actions.
- Section collapse and palette-only focus are session-local TUI state only. They must not write project config, user config, `.harness/`, SQLite, static art files, or any preference file.
- `harness tui-home set-image <image> --output json` explicitly imports a local image into tracked static TUI art files and returns `harness.tui_home_image/v1`; it does not mutate `.harness/`, create tasks, create runs, acquire leases, execute adapters, preflight backends, run Docker, invoke shell tools, call providers, or expose image contents.
- `harness quickstart agent` prints commands only; it does not create files, initialize projects, import agents, create tasks, acquire leases, create runs, create artifacts, execute adapters, or start daemon work.
- The dashboard does not preflight Codex or local backends, run Docker, invoke shell tools, call providers, start schedulers, or inspect backend settings.
- JSON output uses `harness.home/v1` and `harness.quickstart_agent/v1` and does not include `api_key`, `OPENAI_API_KEY`, `base_url`, environment variables, artifact contents, or secret-like metadata.
- `harness home` and `harness quickstart agent` text output uses simple sections for scanning while keeping JSON unchanged.
- Text list/status commands use compact tab-separated headers; JSON schemas remain unchanged.
- Text inspect/explain commands use compact sections for scanning; JSON schemas remain unchanged.

## Verify Read-Only v0.2 Specs Commands

Built-in inspection:

```bash
harness specs --output json
harness specs agent repo_inspector --output json
harness specs workbench coding --output json
harness specs workbench quant --output json
harness specs agent quant_orchestrator --output json
harness specs agent statistical_validity_reviewer --output json
harness specs preview agent commodities_researcher --output json
```

Create a temporary valid custom bundle outside `.harness/`:

```bash
cat > /tmp/harness-v0-2-specs.yaml <<'EOF'
schema_version: harness.spec_bundle/v1
model_profiles:
  local_reasoning:
    id: local_reasoning
    kind: local
    backend: local_openai_compatible
tool_policies:
  read_only:
    tools:
      repo_read: allowed
    network: forbidden
    active_repo_write: forbidden
    hosted_boundary: approval_required
memory_scopes:
  project:
    id: project
agents:
  repo_inspector:
    id: repo_inspector
    kind: specialist
    role: Inspect repository evidence.
    model_profile: local_reasoning
    tool_policy: read_only
    memory_scope: project
workbenches:
  coding:
    id: coding
    description: Coding workbench.
    allowed_agents:
      - repo_inspector
    default_model_profile: local_reasoning
    forbidden_actions:
      - paid_api_fallback
      - hosted_fallback
EOF
```

Validate, export, diff, and preview the explicit bundle:

```bash
harness specs validate /tmp/harness-v0-2-specs.yaml --output json
harness specs export --source /tmp/harness-v0-2-specs.yaml --output json
harness specs diff --source /tmp/harness-v0-2-specs.yaml --output json
harness specs preview agent repo_inspector --source /tmp/harness-v0-2-specs.yaml --output json
harness specs preview workbench coding --source /tmp/harness-v0-2-specs.yaml --output json
```

Expected safety properties:

- Specs commands do not execute agents or preflight backends.
- Specs commands do not read or write `.harness/`.
- Custom bundles are explicit-path only and are not persisted.
- JSON output uses stable `schema_version` wrappers.

v0.6 Quant Workbench expectations:

- `harness specs workbench quant --output json` lists the built-in quant agent set.
- Quant specs are declarations only; they do not create tasks, schedule workflows, execute agents, run Docker, call backends, connect to brokers, place orders, or trade.
- The `quant` workbench forbids live trading, broker actions, capital allocation, order placement, hosted fallback, and paid fallback.
- Agent profiles are declarations only; they expose customization metadata such as knowledge domains, preferred outputs, review responsibilities, and forbidden actions.
- Built-in specs and profiles are packaged YAML loaded through the typed registry; there is no runtime folder auto-discovery outside the repo-packaged built-ins.

Verify v0.7 explicit agent authoring:

```bash
rm -rf /tmp/harness-agent-authoring-smoke
harness agents scaffold smoke_agent \
  --workbench quant \
  --kind specialist \
  --parent quant_research \
  --model-profile local_reasoning \
  --tool-policy read_only \
  --memory-scope quant \
  --output /tmp/harness-agent-authoring-smoke \
  --output-format json
harness agents validate /tmp/harness-agent-authoring-smoke --output json
harness agents preview /tmp/harness-agent-authoring-smoke --output json
```

Expected v0.7 safety properties:

- Agent authoring commands read or write only the explicit operator path.
- Agent authoring commands reject symlinked paths, unsupported profile files, and hard-forbidden path targets.
- Built-in specs remain immutable and custom agent ids cannot shadow built-ins.
- Custom bundles are not auto-discovered and are not persisted into `.harness/`, SQLite, tasks, objectives, runs, leases, artifacts, daemon events, or runtime registry state.
- Authoring commands do not execute agents, preflight backends, run Docker, invoke shell tools, schedule work, call providers, connect to brokers, place orders, send messages, submit applications, or mutate active repo files.
- Output is schema-versioned and does not include backend settings, `api_key`, `OPENAI_API_KEY`, `base_url`, environment variables, or secret-like data.

Verify v0.8 project-local agent import after initializing a project:

```bash
harness init --project .
harness agents import /tmp/harness-agent-authoring-smoke --project . --output json
harness agents list --project . --output json
harness agents inspect smoke_agent --project . --output json
harness agents preview-imported smoke_agent --project . --output json
harness tasks add --title "Use smoke agent" --agent smoke_agent --workbench quant --project . --output json
```

Expected v0.8 safety properties:

- Import persists validated agent/profile metadata, source path, and content hash in initialized harness persistence.
- Import does not modify packaged built-ins and rejects built-in id shadowing or duplicate project-local ids.
- Imported task references record `spec_source_kind: project` but remain non-executing metadata.
- Import/list/inspect do not execute agents, call backends, preflight providers, run Docker, invoke shell tools, create runs, create artifacts, start daemon work, or authorize new tools.
- `agents preview-imported` reports source drift without rewriting the import record.
- `agents remove` is available only for unused imported agents; it rejects built-ins and task-referenced imports.

## Verify Manual v0.3 Task Queue

The task queue requires initialized project state and writes only to `.harness/harness.sqlite`.

```bash
harness init --project .
harness objectives add --title "Queue hardening" --workbench coding --project . --output json
harness objectives list --project . --output json
harness tasks add --title "Inspect repository" --agent repo_inspector --workbench coding --project . --output json
harness tasks list --project . --output json
harness tasks graph --project . --output json
harness tasks run-next --project . --output json
harness tasks cancel task_abc123def456 --project . --output json
```

Expected safety properties:

- `run-next` selects one ready task, creates a local attempt and lease, and marks it `leased`.
- `run-next` does not create a run record or run artifact directory.
- `tasks graph` is read-only and reports local objectives, tasks, dependencies, and blocked reasons.
- Objective and task commands do not execute agents, preflight backends, run Docker, start daemons, or schedule work.

## Verify v0.3.5 Control-Plane Evidence

Create a local run if none exists:

```bash
harness dev create-run --goal "v0.3.5 evidence smoke" --task-type phase_1a_test --project .
RUN_ID=$(harness runs --project . --output json | python -c 'import json,sys; print(json.load(sys.stdin)["runs"][0]["id"])')
```

Inspect runtime policy, artifacts, and tool descriptors:

```bash
harness policy explain --subject-kind run --subject-id "$RUN_ID" --project . --output json
harness artifacts list "$RUN_ID" --project . --output json
harness tools list --project . --output json
harness tools inspect repo_read --project . --output json
```

Compare and baseline local run evidence:

```bash
harness compare "$RUN_ID" "$RUN_ID" --project . --output json
harness baseline set "$RUN_ID" --name smoke-local --project . --output json
harness baseline compare "$RUN_ID" --baseline smoke-local --project . --output json
```

Run local safety-smoke evals and export trace evidence:

```bash
harness evals run --suite safety-smoke --project . --output json
harness traces export "$RUN_ID" --format otel-json --project . --output json
```

Inspect the v0.4 daemon control plane without executing work:

```bash
harness daemon status --project . --output json
harness daemon run-once --project . --output json
harness daemon recover --project . --output json
harness daemon stop --project . --output json
```

Inspect the explicit v0.4.5 dry-run adapter without invoking providers or tools:

```bash
harness tasks add --title "Dry-run contract" --execution-adapter dry_run --task-type phase_1a_test --project . --output json
harness daemon run-once --project . --output json
harness daemon inspect-lease "$LEASE_ID" --project . --output json
harness daemon execute-dry-run "$LEASE_ID" --project . --output json
```

Inspect the explicit v0.5 read-only adapter:

```bash
harness tasks add --title "Read-only summary" --execution-adapter read_only_summary --task-type read_only_repo_summary --project . --output json
harness daemon run-once --project . --output json
harness daemon inspect-lease "$LEASE_ID" --project . --output json
harness daemon execute-read-only "$LEASE_ID" --project . --output json
```

Inspect registered adapter dispatch:

```bash
harness daemon adapters --project . --output json
harness tasks add --title "Dry-run via dispatcher" --execution-adapter dry_run --task-type phase_1a_test --project . --output json
harness daemon run-once --project . --output json
harness daemon inspect-lease "$LEASE_ID" --project . --output json
harness daemon execute "$LEASE_ID" --project . --output json
```

Expected safety properties for the v0.3.5 evidence commands and v0.4 daemon control-plane commands after `RUN_ID` setup:

- These commands are local evidence inspection or baseline commands.
- They do not execute tools, preflight backends, run Docker, create extra runs or artifacts, start schedulers, or schedule background work.
- `daemon run-once` may lease one eligible task or renew an active daemon-owned lease and write daemon heartbeat/event evidence, but it must not execute the task or create a run.
- `daemon run-once` must pause approval-required or daemon-policy-forbidden tasks and report `pause_reasons` instead of failing or executing them.
- `daemon status` must expose paused task reasons so operators can debug approval, dependency, active-lease, or daemon-policy gates without reading SQLite manually.
- `daemon recover` may expire stale active leases and return tasks to `ready`, `blocked`, or `waiting_approval`, but it must not retry terminal tasks automatically.
- v0.4 scheduler commands do not execute tasks, bind task attempts to runs, call backends, run Docker, create run artifacts, add hosted fallback, add paid fallback, or start unmanaged background work.
- `daemon execute-dry-run` is explicit v0.4.5 contract evidence only: it may bind one active lease to one local `phase_1a_test` run and metadata-only artifacts, but it must not call backends, run Docker, execute shell commands, access the network, mutate active repo files, or use hosted/paid fallback.
- `daemon execute-read-only` is explicit read-only adapter execution only: it may bind one active lease to one `read_only_repo_summary` run through the configured `codex_cli` subscription backend in read-only sandbox mode after hosted-boundary approval.
- `daemon execute` is registered-adapter dispatch only: no adapter means no execution, unknown adapter fails closed, and adapter descriptors are documentation and validation metadata rather than permission grants.
- `daemon inspect-lease` is read-only and may report linked task, attempt, run, manifest, dry-run eligibility, read-only eligibility, generic execution eligibility, and recovery recommendation without creating runs or artifacts.
- `daemon recover` may reconcile existing dry-run or read-only evidence but must not create a second run for a linked attempt.
- Registered dispatch does not authorize Docker-from-queue, shell execution, hosted fallback, paid fallback, OpenAI API usage, active repo writes without apply-back approval, MCP/A2A, browser/email/calendar tools, generic task execution, or unmanaged daemon loops.
- Output is schema-versioned and does not include backend settings, `api_key`, `OPENAI_API_KEY`, `base_url`, environment variables, or artifact file contents.
- `harness compare "$RUN_ID" "$RUN_ID"` and baseline comparison against the same run should report no drift.

## Build Local Docker Test Image

```bash
harness tests image validate --project .
harness tests image build --project .
```

Equivalent raw Docker build command:

```bash
docker build -f Dockerfile.harness-test -t harness-test:local .
```

## Run Direct Docker Tests

```bash
harness tests run --project . -- python -m pytest -q
```

The command requires approval. Approve only after confirming the prompt shows a sanitized temporary workspace mounted to `/workspace`, not the active project root.

## Verify Latest Docker Run Artifacts

```bash
LATEST=$(ls -td .harness/runs/run_* | head -1)
cat "$LATEST/test_result.json"
cat "$LATEST/final_report.md"
```

## Optional: Codex Isolated Edit Smoke

The following commands create commits. Run them only in a disposable branch or when you intentionally want smoke-test commits.

Create a scratch file and commit it:

```bash
cat > scratch_codex_edit.py <<'EOF'
def greet():
    return "hello"
EOF
git add scratch_codex_edit.py
git commit -m "Add scratch file for Codex edit smoke test"
```

Create or refresh the required hosted data-boundary approval profile:

```bash
harness approvals add --backend codex_cli --data-boundary hosted_provider --project . --task-types codex_code_edit --duration-days 1
```

Run Codex in an isolated workspace:

```bash
harness run "Modify only scratch_codex_edit.py. Add a docstring inside greet(). Do not create, delete, or modify any other files." --project . --task-type codex_code_edit --keep-isolation
```

When prompted, use `view full diff`, `deny all changes`, or `approve all changes` according to the smoke objective. Denial should leave the active file unchanged.

Optional queued Codex dispatcher smoke:

```bash
harness tasks add --title "Codex queued scratch edit" --execution-adapter codex_isolated_edit --task-type codex_code_edit --project . --output json
harness daemon run-once --project . --output json
harness daemon inspect-lease "$LEASE_ID" --project . --output json
harness daemon execute "$LEASE_ID" --project . --output json
```

The queued smoke also requires hosted-boundary approval, but hosted-boundary approval is not apply-back approval. Apply-back remains denied by default unless an explicit apply-back approval provider is wired into the operator path.

Verify the active file after denial or apply-back:

```bash
cat scratch_codex_edit.py
git status --short
```

Optional cleanup. These commands also create a commit:

```bash
git rm scratch_codex_edit.py
git commit -m "Remove scratch Codex smoke file"
```

## Expected Safety Properties

- `codex_code_edit` edits only an isolated workspace until explicit apply-back approval.
- Denying apply-back leaves the active project unchanged.
- Direct Docker tests mount only a sanitized temporary workspace to `/workspace`.
- Docker test network is disabled by default.
- Docker test denial does not call Docker.
- `run_tests` is model-visible only for `simple_code_edit`.
- No command commits or pushes unless an optional smoke step explicitly runs `git commit`.
