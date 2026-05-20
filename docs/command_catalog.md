# Command Catalog

This catalog groups the main operator commands by workflow. It is a navigation aid only; it does not add hidden automation or broaden the harness safety model.

Unless a command explicitly says otherwise in the operator guide, these surfaces are local control-plane or evidence commands. They do not call Codex, hosted providers, paid providers, shell tools, MCP/A2A, browser/email/calendar tools, broker APIs, or unmanaged daemon loops.

## Orientation

```bash
harness --help
harness "fix the failing tests" --project .
harness "add a CLI flag and update tests" --project . --model gpt-5.5 --reasoning-effort medium
harness run "fix the failing tests" --project .
harness --project .
harness --project . --output json
harness --project . --plain
harness --project . --plain --codex-like
harness home --project .
harness home --project . --output json
harness core run "smoke test core loop" --mode dry_run --project . --output json
harness doctor --release --project . --output json
harness tui-home set-image ~/Pictures/home.png --width 80 --output json
harness quickstart agent --project .
harness quickstart agent --project . --output json
```

`harness "prompt"` is the primary foreground coding path. Explicit `--agent plan --output json` and `--agent build --output json` prompts now use the headless core loop and return the same stable `harness.core_run/v1` shape as `harness core run`; `plan` maps to `repo_planning`, `build` maps to `codex_isolated_edit`, and both Codex-backed modes fail closed without scoped hosted-boundary approval. Other foreground prompts still run the configured `codex_cli` backend end-to-end in the active project workspace with Codex `workspace-write` sandboxing, stream concise Codex event summaries, record stdout/stderr/events/final-message artifacts, and print a final report with status, changed files, diff stat, artifact paths, and the next `harness show <run_id>` command. `harness run "prompt"` defaults to the same direct foreground agent mode. Use `--output json` for the machine-readable report, `--no-stream` to suppress live event summaries, `--fail-on-dirty` to refuse a dirty workspace, and `--model` or `--reasoning-effort` to override the configured Codex settings for one run.

Bare `harness` with no prompt launches the unified Textual app: passive dashboard context, palette/search sections, and the real chat/orchestrator prompt in one terminal surface. `harness --output json` is a read-only context probe that reports `harness.chat/v1` without launching the UI. `harness --plain` runs the line-oriented chat fallback for tests and unsuitable terminals. `--codex-like` starts the session in a testing-friendly foreground action mode where one explicit confirmation can create the approved Harness records and drive registered-adapter dispatch.

The unified app is a conversational operator shell over explicit harness actions: it can initialize project state with `/init`, provide deterministic local guidance, inspect state, select an orchestrator, draft objective/task graphs, ask for confirmation, acquire daemon run-once leases, and dispatch already-leased work only through registered adapters. Repository summaries route to `read_only_summary/read_only_repo_summary`; repo planning requests route to `repo_planning/repo_planning`; coding-fix requests route to a bounded reviewed workflow with `repo_planning/repo_planning`, `codex_isolated_edit/codex_code_edit`, sandbox-test evidence, implementation review, security review, and final synthesis. Drafts show interpreted intent, proposed action, equivalent commands, safety boundary, required approvals, and the confirmation prompt. Results show task/adapter/lease/run/artifact evidence and next inspection commands. Session tools such as `cd`, `pwd`, `read`, `grep`, `glob`, `git-diff`, and permissioned `shell` route through the session-tool gateway with persisted evidence before display. Shell is not ambient generic shell access: it is exact-permission, bounded, non-idempotent execution. The app does not persist chat history or mutate active repository files from chat/model text outside the explicit foreground prompt and registered adapter paths.

The dashboard, palette, and slash-command sections remain passive read-only context. They show project state, summary counts, imported agents, tasks, active leases, daemon events, recent runs, safety reminders, static generated terminal pixel art, local in-memory search over loaded dashboard and command metadata, session-local section collapse, and palette-only focus. They do not execute commands, spawn subprocesses, invoke a shell, copy commands to the clipboard, mutate harness state, persist UI preferences, load image files at runtime, or call providers. `home` and `quickstart agent` remain read-only/non-mutating orientation commands. `tui-home set-image` is an explicit local visual-customization command that imports the provided image into tracked static TUI art files; it does not touch project runtime state, execute adapters, preflight backends, or expose image contents.

`harness --output json` includes registered adapters for compatibility plus the richer capability catalog, runtime controls summary, explicit memory summary, and orchestration progress summary when project state exists. These fields are app context only; they do not grant execution authority.

`harness core run` is the minimal headless backend loop for one vertical slice. It creates existing Harness project state when needed, records a session/objective/task, acquires a lease, dispatches only through the registered adapter dispatcher, writes append-only run evidence and manifests when a run is created, and returns a concise JSON summary. The initial modes are `dry_run`, `repo_planning`, and `codex_isolated_edit`; the Codex-backed modes still fail closed without scoped hosted-boundary approval. The narrow foreground JSON aliases `harness "goal" --agent plan --output json` and `harness "goal" --agent build --output json` consume this same service path; text output, direct active-workspace mode, session modifiers, file attachments, and mention-only native aliases remain on their existing compatibility paths.

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
harness objectives run objective_abc123 --project . --autonomy safe-local --output json
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

`objectives run` is a bounded autonomous objective runner over existing task graphs. It selects only ready or dependency-unblocked tasks within the objective, leases before dispatch, evaluates the selected autonomy profile and adapter metadata before each registered-adapter dispatch, writes objective JSONL evidence under `.harness/autonomy/objectives/`, and stops on success, blocked state, approval requirement, denial, execution failure, or budget exhaustion. It does not create new tasks, expand graphs, call arbitrary tools, bypass approvals, or mutate the active repo.

## Daemon Control Plane

```bash
harness daemon run-once --project . --output json
harness daemon run-autonomous --project . --autonomy daemon-safe --output json
harness daemon adapters --project . --output json
harness daemon status --project .
harness daemon inspect-lease task_lease_abc123 --project .
harness daemon inspect-lease task_lease_abc123 --project . --output json
harness daemon execute task_lease_abc123 --project . --output json
harness daemon recover --project . --output json
harness daemon stop --project . --output json
```

`daemon run-once` is lease-only. `daemon adapters` lists registered adapter descriptors without preflighting backends or executing anything. `daemon inspect-lease` is read-only and reports generic `execution_eligibility`. `daemon execute` is a registered-adapter dispatcher for already-leased tasks only: no adapter means no execution, unknown adapter means fail closed, and adapter descriptors are documentation and validation metadata rather than permission grants. `daemon recover` reconciles existing linked-run evidence without creating a second run or retrying ambiguous work.

`daemon run-autonomous` runs the next active objective that already has runnable work using the graph-driven objective runner and the selected autonomy profile. It is still bounded by leases, adapter descriptors, approval profiles, runtime controls, adapter breakers, budgets, and evidence requirements.

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
harness memory save-derived \
  --scope objective \
  --scope-id obj_abc123 \
  --source-kind objective_state \
  --source-id obj_abc123 \
  --summary "Objective has one ready dry-run task." \
  --project . \
  --output json
harness memory list --project . --output json
harness memory inspect memory_abc123 --project . --output json
harness memory forget memory_abc123 --project . --output json
harness progress --objective obj_abc123 --project . --output json
```

`harness capabilities list` returns `harness.capability_catalog/v1`, a read-only view over registered execution adapters, required approvals, sandbox/readiness notes, safety notes, runtime controls, and equivalent commands. `harness capabilities inspect` returns one capability or a schema-stable fail-closed JSON error.

Unavailable capabilities include structured `blocked_state_explanations` alongside existing readiness reasons so operators can see whether a capability is paused by a runtime control, breaker, approval requirement, or other local policy evidence.

`harness memory save-note`, `save-derived`, `list`, `inspect`, and `forget` return `harness.memory_record/v1` or `harness.memory_records/v1`. Memory records are explicit local operator notes or artifact-derived working memory, scoped by project/workbench/agent/objective/task, redacted before persistence when secret-looking content appears, and forgotten by replacing retained content with `[FORGOTTEN]`. Derived memory source kinds include `artifact_summary`, `objective_state`, `run_review`, and `failed_attempt_summary`; they must link to source ids and remain non-authoritative for permissions, policy, or approvals.

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
harness autonomy policy inspect --project . --profile safe-local --output json
harness act "summarize this repo" --project . --autonomy safe-local --output json
harness approvals add \
  --backend codex_cli \
  --data-boundary hosted_provider \
  --task-types repo_planning,codex_code_edit \
  --duration-hours 8 \
  --autonomy-scope supervised-codex \
  --allowed-adapters repo_planning,codex_isolated_edit \
  --allowed-objectives obj_abc123 \
  --max-runs 4 \
  --project .
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

Evidence commands report metadata, manifests, hashes, verification status, policy decisions, autonomy profiles, local security findings, local integrity checks, security-layer audit checks, and trace/export envelopes. The security check is metadata-only: it inspects persisted local records and manifests without reading artifact bodies, calling providers, touching Docker, or creating new runtime evidence. The integrity check is package/local metadata-only: it hashes built-in specs, adapter descriptors, security docs when present, and static TUI assets without initializing project state or running adapters. The security-layer audit verifies the local-first completion scope without remediation or hidden execution. Evidence commands must not print artifact file contents, secret-like data, backend settings, API keys, environment variables, or provider configuration.

Autonomy policy inspection returns `harness.autonomy_policy_inspect/v1`. It is an explanation surface for built-in profiles such as `manual`, `safe-local`, `supervised-codex`, and `daemon-safe`; it does not execute tools, create approvals, mutate project state, or grant authority outside existing policy, sandbox, approval, lease, adapter, runtime-control, budget, and evidence gates.

Line-oriented chat accepts `--autonomous` as shorthand for `--autonomy safe-local`. Non-manual autonomy affects only validated action contracts. It does not let the model call shell, mutate the active repo, create hosted approvals, apply back isolated changes, or bypass policy. Autonomous contract decisions are recorded under `.harness/autonomy/`.

`safe-local` can auto-create only local Harness control-plane records that pass the autonomy policy, including objectives, dry-run tasks, dry-run task graphs, and explicit project memory notes. Chat-created tasks use stable idempotency keys to avoid duplicate task records for repeated equivalent requests. Memory records remain scoped, hashed, redacted when needed, and non-authoritative for permissions or approvals.

Scoped hosted approval profiles can constrain autonomous Codex use by task type, adapter id, workbench id, objective id, autonomy scope, run count, total runtime, and context byte budget. These profiles can satisfy hosted-boundary checks for `supervised-codex` repo planning or isolated edits only inside their exact stored scope. Legacy hosted approvals without `--autonomy-scope supervised-codex` remain manual-flow approvals and do not satisfy strict autonomous Codex dispatch. They do not authorize apply-back, active repo writes, arbitrary network, shell commands, approval extension, or task type expansion.

`harness act` returns `harness.autonomous_read_loop/v1`. It runs a bounded autonomous act loop: read tools may run within budget, and side-effecting tool requests become Harness action contracts evaluated by the selected autonomy profile. Auto-allowed local control-plane contracts can create objectives, tasks, task graphs, and memory notes. When an auto-created task graph produces an objective, `harness act` can immediately run that objective through the autonomous objective runner and return task/lease/run/artifact evidence to the model loop.

Under `supervised-codex`, `harness act` can dispatch `repo_planning` and `codex_isolated_edit` only when scoped hosted approvals exist for the exact task type, adapter, objective/workbench scope, and autonomy scope. Isolated edits still run in isolated workspaces, reviewer/final-synthesis tasks run as local evidence-producing tasks, and apply-back remains a separate higher boundary that is denied unless an explicit apply-back policy later permits it.

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
