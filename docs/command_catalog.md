# Command Catalog

This catalog groups the main operator commands by workflow. It is a navigation aid only; it does not add hidden automation or broaden the harness safety model.

Unless a command explicitly says otherwise in the operator guide, these surfaces are local control-plane or evidence commands. They do not call Codex, hosted providers, paid providers, shell tools, MCP/A2A, browser/email/calendar tools, broker APIs, or unmanaged daemon loops.

## Orientation

```bash
harness --help
harness --project .
harness --project . --output json
harness --project . --plain
harness --project . --plain --codex-like
harness home --project .
harness home --project . --output json
harness doctor --release --project . --output json
harness tui-home set-image ~/Pictures/home.png --width 80 --output json
harness quickstart agent --project .
harness quickstart agent --project . --output json
```

Bare `harness` is the primary operator application. It launches the unified Textual app: passive dashboard context, palette/search sections, and the real chat/orchestrator prompt in one terminal surface. `harness --output json` is a read-only context probe that reports `harness.chat/v1` without launching the UI. `harness --plain` runs the line-oriented chat fallback for tests and unsuitable terminals. `--codex-like` starts the session in a testing-friendly foreground action mode where one explicit confirmation can create the approved Harness records and drive registered-adapter dispatch.

The unified app is a conversational operator shell over explicit harness actions: it can initialize project state with `/init`, provide deterministic local guidance, inspect state, select an orchestrator, draft objective/task graphs, ask for confirmation, acquire daemon run-once leases, and dispatch already-leased work only through registered adapters. Repository summaries route to `read_only_summary/read_only_repo_summary`; repo planning tasks route to `repo_planning/repo_planning`; orchestrated edit work routes generated tasks to `codex_isolated_edit/codex_code_edit` through the registered dispatcher. It does not call providers directly, run a generic shell, preflight backends for context display, persist chat history, or mutate active repository files from chat/model text.

The dashboard, palette, and slash-command sections remain passive read-only context. They show project state, summary counts, imported agents, tasks, active leases, daemon events, recent runs, safety reminders, static generated terminal pixel art, local in-memory search over loaded dashboard and command metadata, session-local section collapse, and palette-only focus. They do not execute commands, spawn subprocesses, invoke a shell, copy commands to the clipboard, mutate harness state, persist UI preferences, load image files at runtime, or call providers. `home` and `quickstart agent` remain read-only/non-mutating orientation commands. `tui-home set-image` is an explicit local visual-customization command that imports the provided image into tracked static TUI art files; it does not touch project runtime state, execute adapters, preflight backends, or expose image contents.

`harness --output json` includes registered adapters for compatibility plus the richer capability catalog, runtime controls summary, explicit memory summary, and orchestration progress summary when project state exists. These fields are app context only; they do not grant execution authority.

## Agent Authoring

```bash
harness agents scaffold my_agent \
  --workbench quant \
  --kind specialist \
  --parent quant_research \
  --model-profile local_reasoning \
  --tool-policy read_only \
  --memory-scope quant \
  --output agents/my_agent \
  --output-format json
harness agents validate agents/my_agent --output json
harness agents preview agents/my_agent --output json
```

Custom bundles are explicit-path metadata. They are not auto-discovered and are not persisted into project state until imported.

## Project Agents

```bash
harness init --project .
harness agents import agents/my_agent --project . --output json
harness agents list --project .
harness agents inspect my_agent --project .
harness agents inspect my_agent --project . --output json
harness agents preview-imported my_agent --project . --output json
harness agents remove my_agent --project . --output json
```

Imported agents remain declarative metadata. Importing an agent does not grant new tools, create tasks, create runs, or start background work.

## Built-In Specs

```bash
harness specs --output json
harness specs agent repo_inspector --output json
harness specs workbench quant --output json
harness specs preview agent commodities_researcher --output json
harness specs export --source builtin --output json
harness specs diff bundle.yaml --output json
```

Spec commands inspect or validate declarative registry state. They do not preflight backends or execute agents.

## Objectives And Tasks

```bash
harness objectives add --title "Research objective" --project . --output json
harness objectives list --project .
harness objectives inspect objective_abc123 --project . --output json
harness tasks add --title "Read-only summary" \
  --agent my_agent \
  --workbench quant \
  --execution-adapter read_only_summary \
  --task-type read_only_repo_summary \
  --project . \
  --output json
harness tasks add --title "Plan repo change" \
  --execution-adapter repo_planning \
  --task-type repo_planning \
  --project . \
  --output json
harness tasks list --project .
harness tasks inspect task_abc123 --project .
harness tasks graph --project . --output json
harness tasks status task_abc123 succeeded --project . --output json
harness tasks cancel task_abc123 --project . --output json
harness tasks retry task_abc123 --project . --output json
harness tasks run-next --project . --output json
```

Task queue commands are manual SQLite control-plane operations. `tasks run-next` leases work for inspection/adapter handoff; it does not execute agents or create runs.

## Daemon Control Plane

```bash
harness daemon run-once --project . --output json
harness daemon adapters --project . --output json
harness daemon status --project .
harness daemon inspect-lease task_lease_abc123 --project .
harness daemon inspect-lease task_lease_abc123 --project . --output json
harness daemon execute task_lease_abc123 --project . --output json
harness daemon recover --project . --output json
harness daemon stop --project . --output json
```

`daemon run-once` is lease-only. `daemon adapters` lists registered adapter descriptors without preflighting backends or executing anything. `daemon inspect-lease` is read-only and reports generic `execution_eligibility`. `daemon execute` is a registered-adapter dispatcher for already-leased tasks only: no adapter means no execution, unknown adapter means fail closed, and adapter descriptors are documentation and validation metadata rather than permission grants. `daemon recover` reconciles existing linked-run evidence without creating a second run or retrying ambiguous work.

`daemon inspect-lease` and `daemon execute` include `blocked_state_explanations` in JSON and print `Blocked state` rows in text output. These explanations normalize missing approvals, disabled adapters, unsafe metadata, unknown adapters, sandbox profile evidence gaps, breaker-open state, and forbidden path or secret-like blocks without changing the underlying decision.

## Runtime Controls

```bash
harness controls list --project . --output json
harness controls disable --target-kind adapter --target-id dry_run --reason "pause dry run" --project . --output json
harness controls enable --target-kind adapter --target-id dry_run --project . --output json
harness controls breaker-status --project . --output json
harness controls breaker-reset dry_run --reason "operator reviewed failures" --project . --output json
```

Runtime controls are local kill switches and adapter breakers. They only narrow execution authority: a disabled control or open breaker can deny generic registered dispatch, but enabling a control cannot bypass lease, policy, approval, sandbox, or adapter validation.

## v1.8 Local App Surfaces

```bash
harness capabilities list --project . --output json
harness capabilities inspect dry_run --project . --output json
harness memory save-note --scope project --summary "Local operator note" --project . --output json
harness memory list --project . --output json
harness memory inspect memory_abc123 --project . --output json
harness memory forget memory_abc123 --project . --output json
harness progress --objective obj_abc123 --project . --output json
```

`harness capabilities list` returns `harness.capability_catalog/v1`, a read-only view over registered execution adapters, required approvals, sandbox/readiness notes, safety notes, runtime controls, and equivalent commands. `harness capabilities inspect` returns one capability or a schema-stable fail-closed JSON error.

Unavailable capabilities include structured `blocked_state_explanations` alongside existing readiness reasons so operators can see whether a capability is paused by a runtime control, breaker, approval requirement, or other local policy evidence.

`harness memory save-note`, `list`, `inspect`, and `forget` return `harness.memory_record/v1` or `harness.memory_records/v1`. Memory records are explicit local operator notes in v1.8, scoped by project/workbench/agent/objective, redacted before persistence when secret-looking content appears, and forgotten by replacing retained content with `[FORGOTTEN]`.

`harness progress --objective` returns `harness.orchestration_progress/v1`, a read-only objective/task/lease/run state summary with mode, blockers, active lease/run ids, task rows, and deterministic next commands.

The chat aliases `/capabilities`, `/memory`, `/remember <text>`, `/forget <memory_id>`, `/progress [objective_id]`, “show capabilities”, “what can Harness do here?”, “show memory”, “show progress”, and “where are we” render these same local surfaces. The TUI right panel prefers capability rows and adds a Progress section. None of these surfaces create tasks, acquire leases, create runs, dispatch adapters, call providers, preflight backends, touch Docker, invoke shell commands, or mutate active repository files.

## Registered Execution Adapters

```bash
harness daemon execute-dry-run task_lease_abc123 --project . --output json
harness daemon execute-read-only task_lease_abc123 --project . --output json
harness daemon execute task_lease_abc123 --project . --output json
```

`execute-dry-run` and `execute-read-only` are compatibility commands with their original JSON contracts. The generic `daemon execute` command dispatches the same already-leased tasks through the registered-adapter registry and returns `harness.daemon_execute/v1`.

The read-only adapter requires an existing active daemon lease, exact metadata `execution_adapter=read_only_summary` plus `task_type=read_only_repo_summary`, and a valid hosted-boundary Codex approval profile for `read_only_repo_summary`. It uses the supervised `codex_cli` subscription backend with ChatGPT auth, `gpt-5.5`, low reasoning effort, and Codex read-only sandbox mode. It does not use the local model backend as a fallback.

The Codex isolated adapter requires exact metadata `execution_adapter=codex_isolated_edit` plus `task_type=codex_code_edit`, a valid hosted-boundary Codex approval profile, and a safe `codex_cli` backend. Hosted-boundary approval is not apply-back approval: active repo mutation remains denied by default unless the explicit apply-back approval path approves the inspected diff.

The repo planning adapter requires exact metadata `execution_adapter=repo_planning` plus `task_type=repo_planning`, a valid hosted-boundary Codex approval profile, and a safe `codex_cli` backend. It uses Codex read-only sandbox mode to produce planning evidence and fails the task if the read-only policy check detects active repository changes.

The TUI command palette and right-panel context include copy-only templates for repo-planning task creation and generic registered dispatch. Displaying these commands does not execute them, acquire leases, call providers, run Docker, or grant adapter permissions.

Registered adapters do not authorize Docker-from-queue, generic shell access, hosted fallback, paid fallback, OpenAI API usage, MCP/A2A, browser/email/calendar tools, broker actions, live trading, order placement, or unmanaged daemon loops.

## Runtime Evidence

```bash
harness runs --project .
harness show run_abc123 --project . --output json
harness artifacts list run_abc123 --project .
harness artifacts inspect artifact_abc123 --project .
harness policy explain --subject-kind task --subject-id task_abc123 --project . --output json
harness tools list --project . --output json
harness tools inspect repo_read --project . --output json
harness compare run_a run_b --project . --output json
harness baseline set run_abc123 --name local --project . --output json
harness baseline compare run_def456 --baseline local --project . --output json
harness evals run --suite safety-smoke --project . --output json
harness evals run --suite security --project . --output json
harness evals run --suite integrity --project . --output json
harness evals run --suite security-layer --project . --output json
harness security audit --project . --output json
harness security check --project . --output json
harness integrity check --project . --output json
harness traces export run_abc123 --format otel-json --project . --output json
```

Evidence commands report metadata, manifests, hashes, verification status, policy decisions, local security findings, local integrity checks, security-layer audit checks, and trace/export envelopes. The security check is metadata-only: it inspects persisted local records and manifests without reading artifact bodies, calling providers, touching Docker, or creating new runtime evidence. The integrity check is package/local metadata-only: it hashes built-in specs, adapter descriptors, security docs when present, and static TUI assets without initializing project state or running adapters. The security-layer audit verifies the local-first completion scope without remediation or hidden execution. Evidence commands must not print artifact file contents, secret-like data, backend settings, API keys, environment variables, or provider configuration.

## Packaging Smoke

```bash
python3 -m pip wheel --no-deps --no-build-isolation -w /tmp/harness-wheel .
python3 -m venv --system-site-packages /tmp/harness-install
/tmp/harness-install/bin/python -m pip install --no-deps /tmp/harness-wheel/agent_harness-*.whl
/tmp/harness-install/bin/harness --help
/tmp/harness-install/bin/harness specs --output json
/tmp/harness-install/bin/harness integrity check --project /tmp/harness-project --output json
/tmp/harness-install/bin/harness home --project /tmp/harness-project --output json
```

The wheel smoke confirms console-script wiring, packaged built-in YAML availability, security-layer model availability, registered adapter descriptor integrity, and packaged security-sensitive docs. It remains local-only and non-executing.
