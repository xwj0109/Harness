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
harness core inspect-evidence --run <run_id> --project . --output json
harness core inspect-evidence --task <task_id> --project . --output json
harness core inspect-run <run_id> --project . --output json
harness core inspect-events <run_id> --project . --output json
harness core inspect-task <task_id> --project . --output json
harness doctor --release --project . --output json
harness tui-home set-image ~/Pictures/home.png --width 80 --output json
harness quickstart agent --project .
harness quickstart agent --project . --output json
```

`harness "prompt"` is the primary foreground coding path. Explicit `--agent plan --output json` and `--agent build --output json` prompts now use the headless core loop and return the same stable `harness.core_run/v1` shape as `harness core run`; `plan` maps to `repo_planning`, `build` maps to `codex_isolated_edit`, and both Codex-backed modes fail closed without scoped hosted-boundary approval. Other foreground prompts still run the configured `codex_cli` backend end-to-end in the active project workspace with Codex `workspace-write` sandboxing, stream concise Codex event summaries, record stdout/stderr/events/final-message artifacts, and print a final report with status, changed files, diff stat, artifact paths, and the next `harness show <run_id>` command. `harness run "prompt"` defaults to the same direct foreground agent mode. Use `--output json` for the machine-readable report, `--no-stream` to suppress live event summaries, `--fail-on-dirty` to refuse a dirty workspace, and `--model` or `--reasoning-effort` to override the configured Codex settings for one run.

Bare `harness` with no prompt launches the unified Textual app: passive dashboard context, palette/search sections, and the real chat/orchestrator prompt in one terminal surface. `harness --output json` is a read-only context probe that reports `harness.chat/v1` without launching the UI. `harness --plain` runs the line-oriented chat fallback for tests and unsuitable terminals. `--codex-like` starts the session in a testing-friendly foreground action mode where one explicit confirmation can create the approved Harness records and drive registered-adapter dispatch.

The unified app is a conversational operator shell over explicit harness actions: it can initialize project state with `/init`, provide deterministic local guidance, inspect state, select an orchestrator, draft objective/task graphs when policy requires review, acquire daemon run-once leases, and dispatch already-leased work only through registered adapters. Repository summaries route to `read_only_summary/read_only_repo_summary`; repo planning requests route to `repo_planning/repo_planning`; coding-fix and file-write requests route to a bounded reviewed workflow with `repo_planning/repo_planning`, `codex_isolated_edit/codex_code_edit`, sandbox-test evidence, implementation review, security review, and final synthesis. Under the default `supervised-codex` profile, chat-routed isolated-edit contracts can auto-start without a live approval prompt after deterministic policy evaluation creates scoped authority evidence. External filesystem write requests such as Downloads or Desktop are blocked before orchestration with explicit boundary evidence instead of prompting. Dirty active Git repositories use an isolated copy from the current workspace state for supervised Codex edits, while active apply-back remains a separate boundary. Manual mode still renders interpreted intent, proposed action, equivalent commands, safety boundary, required approvals, and the confirmation prompt. Results show task/adapter/lease/run/artifact evidence and next inspection commands. Session tools such as `cd`, `pwd`, `read`, `grep`, `glob`, `git-diff`, and permissioned `shell` route through the session-tool gateway with persisted evidence before display. Shell is not ambient generic shell access: it is exact-permission, bounded, non-idempotent execution. The app does not persist chat history or mutate active repository files from chat/model text outside the explicit foreground prompt and registered adapter paths.

The dashboard, palette, and slash-command sections remain passive read-only context. They show project state, summary counts, imported agents, tasks, active leases, daemon events, recent runs, safety reminders, static generated terminal pixel art, local in-memory search over loaded dashboard and command metadata, session-local section collapse, and palette-only focus. They do not execute commands, spawn subprocesses, invoke a shell, copy commands to the clipboard, mutate harness state, persist UI preferences, load image files at runtime, or call providers. `home` and `quickstart agent` remain read-only/non-mutating orientation commands. `tui-home set-image` is an explicit local visual-customization command that imports the provided image into tracked static TUI art files; it does not touch project runtime state, execute adapters, preflight backends, or expose image contents.

`harness --output json` includes registered adapters for compatibility plus the richer capability catalog, runtime controls summary, explicit memory summary, and orchestration progress summary when project state exists. These fields are app context only; they do not grant execution authority.

`harness core run` is the minimal headless backend loop for one vertical slice. It creates existing Harness project state when needed, records a session/objective/task, acquires a lease, dispatches only through the registered adapter dispatcher, writes append-only run evidence and manifests when a run is created, and returns a concise JSON summary. The initial modes are `dry_run`, `repo_planning`, and `codex_isolated_edit`; the Codex-backed modes still fail closed without scoped hosted-boundary approval. The narrow foreground JSON aliases `harness "goal" --agent plan --output json` and `harness "goal" --agent build --output json` consume this same service path; text output, direct active-workspace mode, session modifiers, file attachments, and mention-only native aliases remain on their existing compatibility paths. `harness core inspect-evidence` returns the canonical bundled read-only evidence envelope for a run or task, including run/task/blocked-state/event/artifact-metadata projections where available. `harness core inspect-run` returns the canonical read-only run projection used for backend stabilization; it reports persisted ids, lease/task/adapter status, manifest path, artifact metadata, policy hash, blocked reasons, and next commands without reading artifact bodies. `harness core inspect-events` returns sanitized persisted run events through the same projection layer. `harness core inspect-task` returns the matching task or blocked-state projection for tasks that have run evidence or persisted no-run rejection evidence.

`harness show <run_id> --output json` is a compatibility wrapper over the canonical bundled evidence projection. It returns `harness.show/v2` with `core_evidence` containing the same bundle as `harness core inspect-evidence --run <run_id>`. `harness tasks inspect <task_id> --output json` returns `harness.tasks_inspect/v2` with `core_evidence` when run or blocked-task evidence exists. `harness events <run_id> --output json` returns `harness.events_inspect/v2` with `core_events` from the canonical run-event projection. Text output remains legacy, and run/task listing, task mutation, tailing, JSONL event output, event following, and artifact commands keep their existing contracts.

## Model Catalog

Model/provider commands operate on distinct concepts. Provider descriptors are metadata records for endpoint policy, protocol adapter, data boundary, billing boundary, discovery behavior, credential policy, and provider-level capabilities; they are not credentials and do not prove runtime readiness. Model descriptors are provider-owned entries with canonical refs, aliases, variants, API ids, limits, modalities, tool/reasoning support, cost metadata, source labels, status, and blocked reasons. Provider accounts bind a provider to a redacted credential source, while approvals bind a provider or data boundary to an explicit operator decision. Runtime execution uses all four pieces in order: selected model ref, descriptor validation, policy approval, then credential resolution. Catalog reads stop before credential resolution or network construction unless the command is the explicit `models refresh` discovery operation.

Model refs use `provider_id/model_id`, with optional `@variant` for named option profiles. `raw_model_ref` preserves what the operator selected, `canonical_model_ref` records the concrete descriptor to validate and execute, and `alias_used` records the alias when a ref such as `codex/gpt-5.5`, `local/qwen3-coder`, or `openai/gpt-5.3-codex` resolves to a canonical target. Aliases do not search, enable providers, approve hosted boundaries, read credentials, or create hidden fallback. Local model ids may contain colons, such as `qwen3-coder:30b`; only known suffixes like `:high` are treated as legacy variant selectors, so `@variant` is the unambiguous form.

Default model resolution chooses one candidate ref, then validates it. The order is command argument, active session model, session default metadata, workspace default ref, operator default preference, and workbench default profile. The first candidate is audited as `session.model_resolution`; Harness does not try later defaults if the selected candidate is unknown, disabled, missing credentials, policy-blocked, or unsupported. Missing defaults fail with `model_ref_missing`. This is deterministic selection, not hidden fallback, and evidence must keep `hidden_provider_fallback=false`, `hidden_model_fallback=false`, and `no_hidden_fallback=true`.

Credential commands and projections are redacted by contract. Provider account metadata lives in the local SQLite store; API-key and OAuth-token values live in `.harness/provider_secrets.json` behind local `0600` file permissions and account-id lookup; env-backed accounts store only the env var name. CLI and local-server list/status/validate surfaces may show credential kind, source, status, account id, expiry, env var name, and header names, but they must keep `credential_value_included=false` and `credentials_included=false`. TUI projections redact env-var names further, for example as `env:<redacted>`, so dashboard/model-picker JSON does not contain `OPENAI_API_KEY` or equivalent names. Secret material is read only during runtime credential resolution after validation and approval gates, and action outputs never print stored, env, header, OAuth, or removed credential values.

Provider connect/disconnect commands mutate only local account state. `providers login` records a provider account for `env`, `api_key`, `oauth`, `static_local`, `codex_login`, `aws_env`, or `aws_profile`; env login stores an env var name, API-key login writes the local secret store, and OAuth login stores supplied manual-code tokens when provided. `providers accounts` lists redacted rows, `providers activate-account` switches the active account, and `providers logout` removes the provider's local accounts and matching secret-store entries. The local server mirrors these actions through bearer-auth `POST /provider/{provider_id}/auth/env`, `POST /provider/{provider_id}/auth/api-key`, `POST /provider/{provider_id}/auth/local`, `POST /provider/{provider_id}/auth/activate`, `POST /provider/{provider_id}/oauth/authorize`, `POST /provider/{provider_id}/oauth/callback`, and `DELETE /provider/{provider_id}/auth`. These actions do not refresh models, test credentials, select models, enable providers, grant approvals, or start provider execution. In the TUI, provider connect lives inside `/models`: `Ctrl+A` on a selected model/provider row opens the account/auth-method chooser, masked API-key entry, env-var entry, local-only account connect, or OAuth handoff; successful connect records redacted evidence and returns to the model picker filtered to the connected provider. `/provider` is a compatibility alias that opens the same model-picker flow.

The TUI model picker is another projection of this catalog, not a runtime backend. Open it with `ctrl+x m`, `/models`, or `/model`; select with Enter or `/model <number|search|provider/model>`. Rows are grouped as current, favorites, recents, connected providers, local providers, hosted providers, then disabled or blocked providers. Selection validates and stores only active-session model metadata plus validation evidence. Favorite/default/inspect actions mutate only preference or evidence state, and provider connect/refresh/disconnect keys route to explicit provider actions. Opening, filtering, moving through, or selecting rows must keep provider/model execution, network access, permission grants, and hidden fallback disabled.

Model discovery is explicit through `models refresh <provider_id>`. Local refresh validates the local endpoint and may call only that provider's model-list endpoint. Hosted or non-local refresh requires `--approve-hosted` for that command and otherwise fails before network access; this discovery flag is not a persistent runtime approval. `--with-credentials` permits credential-backed discovery where supported, but credential values stay redacted and missing credentials fail before network. `--clear-cache` removes only discovered overlay rows for that provider with no network access. Runtime execution later re-checks hosted, paid, and data-boundary approvals before credentials or provider clients are resolved.

```bash
harness models list --project . --output json
harness models providers --project . --output json
harness models inspect codex/gpt-5.5 --project . --output json
harness models validate codex/gpt-5.5 --project . --output json
harness models protocols --project . --output json
harness models preferences --project . --output json
harness models favorite codex_cli/gpt-5.5 --project . --output json
harness models unfavorite codex_cli/gpt-5.5 --project . --output json
harness models default codex_cli/gpt-5.5 --project . --output json
harness models refresh local_openai_compatible --project . --output json
harness models refresh paid_openai_compatible --project . --output json
harness models refresh local_openai_compatible --clear-cache --project . --output json
harness models config validate --project . --output json
harness providers list --project . --output json
harness providers status --project . --output json
harness providers login paid_openai_compatible --project . --output json
harness providers accounts paid_openai_compatible --project . --output json
harness providers activate-account paid_openai_compatible <account_id> --project . --output json
harness providers logout paid_openai_compatible --project . --output json
```

Model catalog list, provider, inspect, validate, protocol, and preference commands are metadata-only unless a command explicitly says it refreshes discovery or mutates a local preference/account row. They expose explicit refs, canonical refs, aliases, protocol adapter ids, source labels, context limits, max output, reasoning support, modalities, tool support, cost metadata, provider enablement, data boundary, and blocked reasons without preflighting backends, calling providers, reading credentials, or granting hidden fallback. `models favorite`, `models unfavorite`, and `models default` update only the local model preference store after validating the ref; they do not call providers, start execution, or guarantee future runtime executability. `models refresh` is explicit discovery: local OpenAI-compatible refresh validates the local endpoint before calling `/models`; hosted refresh requires `--approve-hosted` and fails closed before network access without it. Discovered rows are marked `source=discovered` and do not auto-enable hosted or paid execution.

Discovered models are durable overlays. After an explicit successful `harness models refresh <provider_id>`, discovered model refs are persisted in the catalog cache and appear in later `harness models list`, `harness models inspect`, TUI picker projections, and validation output without another provider call. Discovery cache rows carry metadata such as discovery timestamp, endpoint, network/credential evidence, hosted approval evidence when applicable, discovered model ids, and a SHA-256 hash of the raw provider response. `harness models refresh <provider_id> --clear-cache` removes only cached discovered models for that provider; built-in, backend-config, and custom-config rows remain intact and no network is accessed.

Provider protocol adapters normalize through canonical message/content types before producing provider-specific payloads. Text request parts are supported by the current `openai_chat`, `openai_responses`, `openai_codex_responses`, `anthropic_messages`, `google_generative`, `bedrock_converse`, `codex_cli`, and legacy chat-model paths. Multimodal, reasoning, tool-call, and provider-native metadata parts have explicit canonical representations; unsupported request parts fail with a visible provider error rather than being omitted silently.

Model selection validation applies capability metadata before execution. Unsupported context-window requests, output limits, requested reasoning levels, input modalities, and required tool support fail closed before adapter dispatch. Runtime provider events include normalized usage/cost evidence when providers report usage: `normalized_usage` contains input, output, cache-read, cache-write, and total token counts, `provider_reported_cost` is preserved when present, and `estimated_cost` is populated when model cost metadata is available.

The `openai_chat` adapter uses true OpenAI-compatible streaming by default: it sends `/chat/completions` with `stream=true`, parses SSE chunks, and emits normalized text delta, tool-call delta, usage, finish, and error events. Non-streaming fallback is explicit through `stream: false` backend settings and is not used silently.

The `openai_responses` and `openai_codex_responses` adapters send `/responses` with `stream=true`, parse Responses SSE chunks, and emit normalized text/refusal delta, reasoning summary delta, tool-call delta/completion, usage, completion, response id, and error events. The paid built-in Codex API model uses `openai_codex_responses` metadata while staying disabled by default until the provider and credentials are explicitly configured.

The `anthropic_messages` adapter sends `/messages` with `stream=true`, parses Anthropic SSE chunks, and emits normalized message id, text delta, thinking delta, tool-use start/delta/completion, usage, stop reason, and error events. The built-in Anthropic provider/model metadata is visible but disabled by default and uses `ANTHROPIC_API_KEY` only as a redacted credential reference.

The `google_generative` adapter sends Gemini-compatible `:streamGenerateContent?alt=sse` requests, parses Google stream chunks, and emits normalized usage, thought-signature reasoning, text delta, function-call, completion, and error events. The built-in Google provider/model metadata is visible but disabled by default and uses `GOOGLE_API_KEY` only as a redacted credential reference.

The `bedrock_converse` adapter serializes canonical messages into Bedrock Converse-shaped payloads and parses Converse stream events into normalized text, tool-call, usage/metrics, completion, and error events. Built-in Bedrock provider/model metadata is visible but disabled by default and uses AWS profile/env metadata only; AWS secret values are not included in command output.

Provider account commands are explicit credential-account actions. `providers login <provider_id>` accepts `--credential-kind KIND`, `--env-var NAME`, `--api-key VALUE`, `--access-token VALUE`, `--refresh-token VALUE`, `--expires-at TIMESTAMP`, `--scopes TEXT`, and `--description TEXT` where applicable. Env-backed login stores the env var name and derives configured/missing status from the current process; API-key and OAuth-token login store secret values in the provider secret store without printing them; static-local, Codex-login, and AWS account kinds store only local account metadata. `providers accounts`, `providers activate-account`, and `providers logout` inspect, switch, and remove those redacted account records. These commands report `credentials_included=false`, `network_accessed=false`, and no hidden provider/model fallback. TUI provider-connect evidence follows the same contract: typed API keys are masked and cleared after submission, env values are never read, and provider navigation remains metadata-only.

Project-local custom providers and models live in `.harness/models.yaml`. `harness models config validate` parses that file without calling providers, reading credentials, writing credentials, or starting model execution. The supported provider schema includes `display_name`, `enabled`, `data_boundary`, `base_url`/`endpoint`, `protocol`, redacted `credential` references, env-backed `headers`, `compatibility`, and nested `models`. Model entries support `display_name`, `api_id`, protocol override, `context_window`, `max_output_tokens`, modalities, tool support, reasoning support/map, cost, status, and variants. Local providers must use loopback URLs unless an explicit LAN endpoint is listed, hosted providers cannot be enabled unless `approved: true`, and credential/header values must be references rather than raw secrets.

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

Line-oriented chat defaults to `--autonomy supervised-codex`; `--autonomy manual` preserves interactive confirmation, and `--autonomous` remains shorthand for `--autonomy safe-local`. Non-manual autonomy affects only validated action contracts. It does not let the model call shell, mutate the active repo, apply back isolated changes, or bypass policy. For `supervised-codex` isolated-edit contracts, Harness can create the scoped hosted-provider authority record itself after policy evaluation so the workflow starts without a live approval prompt. Autonomous contract decisions are recorded under `.harness/autonomy/`.

`safe-local` can auto-create only local Harness control-plane records that pass the autonomy policy, including objectives, dry-run tasks, dry-run task graphs, and explicit project memory notes. Chat-created tasks use stable idempotency keys to avoid duplicate task records for repeated equivalent requests. Memory records remain scoped, hashed, redacted when needed, and non-authoritative for permissions or approvals.

Scoped hosted approval profiles can constrain autonomous Codex use by task type, adapter id, workbench id, objective id, autonomy scope, run count, total runtime, and context byte budget. These profiles still satisfy explicit autonomous adapter dispatch and objective-run hosted-boundary checks only inside their exact stored scope. Chat-routed `edit_isolated` contracts under `supervised-codex` use a narrower Toloclaw-style auto-transition: Harness creates scoped internal authority for Codex planning/edit execution, keeps writes in the isolated workspace, and leaves active-repo apply-back outside the auto path. Legacy hosted approvals without `--autonomy-scope supervised-codex` remain manual-flow approvals and do not satisfy strict autonomous Codex dispatch. They do not authorize apply-back, active repo writes, arbitrary network, shell commands, approval extension, or task type expansion.

`harness act` returns `harness.autonomous_read_loop/v1`. It runs a bounded autonomous act loop: read tools may run within budget, and side-effecting tool requests become Harness action contracts evaluated by the selected autonomy profile. Auto-allowed local control-plane contracts can create objectives, tasks, task graphs, and memory notes. When an auto-created task graph produces an objective, `harness act` can immediately run that objective through the autonomous objective runner and return task/lease/run/artifact evidence to the model loop.

Under `supervised-codex`, chat-routed `edit_isolated` requests auto-transition into the reviewed coding workflow without a live confirmation prompt. Direct autonomous dispatch of `repo_planning` or `codex_isolated_edit` still requires a scoped hosted approval profile for the exact task type, adapter, objective/workbench scope, and autonomy scope. Isolated edits run in isolated workspaces, reviewer/final-synthesis tasks run as local evidence-producing tasks, and apply-back remains a separate higher boundary that is denied unless an explicit apply-back policy later permits it.

## Governance Authority

```bash
harness governance gates --output json
harness governance tasks create governance-slice \
  --agent repo_inspector \
  --goal "Wire governed change evidence" \
  --base main \
  --project . \
  --output json
harness governance tasks list --project . --output json
harness governance tasks show task_abc123 --project . --output json
harness governance context build --task task_abc123 --project . --output json
harness governance tests plan task_abc123 --project . --output json
harness governance tests run task_abc123 --project . --output json
harness governance merge-check feature/governed-change --base main --project . --output json
harness governance data-audit --project . --output json
harness governance network validate --policy /tmp/network-policy.json --project . --output json
harness governance network check-url https://docs.example.com/page --policy /tmp/network-policy.json --project . --output json
harness governance network quarantine https://docs.example.com/report.pdf --policy /tmp/network-policy.json --project . --output json
harness governance applyback validate --input /tmp/applyback-request.json --project . --output json
harness governance tasks close task_abc123 --project . --output json
```

Governance is an authority layer, not a helper command group. These commands create or inspect the evidence Harness uses to decide whether work is within scope, whether context and tests are bound to a governed task segment, whether network and quarantine rules were followed, and whether promotion is allowed. Governance evidence narrows or blocks authority; it does not create provider approvals, grant future permissions, start hidden work, or bypass the normal session, task, adapter, approval, sandbox, and protected-path boundaries.

The core JSON schemas are:

- `harness.governance.gate_registry/v1` for `governance gates`.
- `harness.governance_task/v1` and `harness.governance_tasks/v1` for governed task records.
- `harness.governance_context_pack/v1` for context packs.
- `harness.governance_test_plan/v1` and `harness.governance_test_run/v1` for test evidence.
- `harness.governance.merge_check/v1` for merge-check evidence.
- `harness.data_inventory/v1` and `harness.data_cleanup_proposal/v1` for data audit output.
- `harness.governance_network_policy_check/v1`, `harness.governance_network_check_url/v1`, and `harness.governance_download_quarantine/v1` for network policy evidence.
- `harness.governance_applyback_verdict/v1` for apply-back and promotion verdicts.

`governance merge-check` is a fail-closed evidence command. It runs local checks, writes local evidence under `.harness/governance/`, and exits nonzero for blocking verdicts. It does not merge branches, push commits, comment on pull requests, call providers, start adapters, execute arbitrary shell commands, or mutate the active repository.

`governance applyback validate` validates an input request that includes `task_id`, `segment_id` or `objective_id`, `context_pack_hash`, `approval_id`, `allowed_paths`, `changed_files`, `diff_summary`, and fresh passing `test_evidence`. Protected path hits require explicit exception evidence. Quarantined artifacts are rejected unless a visual, security, or quality review has promoted them. The command writes durable evidence only; its payload explicitly reports that it did not grant permission, future authority, or active repo mutation.

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
