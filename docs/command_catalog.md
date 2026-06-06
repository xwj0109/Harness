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

`harness "prompt"` is the primary foreground coding path. Explicit `--agent plan --output json` and `--agent build --output json` prompts now use the headless core loop and return the same stable `harness.core_run/v1` shape as `harness core run`; `plan` maps to `repo_planning`, `build` maps to `codex_isolated_edit`, and both Codex-backed modes fail closed before lease acquisition without scoped hosted-boundary approval. Other foreground prompts still run the configured `codex_cli` backend end-to-end in the active project workspace with Codex `workspace-write` sandboxing, stream concise Codex event summaries, record stdout/stderr/events/final-message artifacts, and print a final report with status, changed files, diff stat, artifact paths, and the next `harness show <run_id>` command. `harness run "prompt"` defaults to the same direct foreground agent mode. Use `--output json` for the machine-readable report, `--no-stream` to suppress live event summaries, `--fail-on-dirty` to refuse a dirty workspace, and `--model` or `--reasoning-effort` to override the configured Codex settings for one run.

`harness doctor --release` is a metadata-only release gate. It includes model/provider release checks, `extension_config_path_safety` for symlink-safe configured skill/MCP resource paths, `session_transcript_health` for malformed session transcript JSONL, `orchestration_readiness_release_gates`, `orchestration_efficiency_release_gates`, and `orchestration_synthesis_release_gates`. The readiness gate embeds the no-reference orchestration-readiness audit summary including pending chat action recovery, agent discovery and deterministic delegate allocation, bounded scheduling, workflow coordination contracts, orchestration scenario conformance, serialized delegate budgets, objective lifecycle controls, checkpoints, traces, runtime controls, tool exposure, external protocol compatibility, schema compatibility contracts, replay drift detection, agentic security controls for memory poisoning/insecure inter-agent communication/cascading failures, and apply-back governance. The efficiency gate embeds the security-versus-complexity audit summary including adapter control coverage, bounded critical-path scheduling, delegate budget ceilings, benchmark contracts, live benchmark permit contracts, retry/idempotency policy, daemon pre-lease descriptor approval gating, manual queue pre-lease descriptor approval gating, foreground core pre-lease descriptor approval gating, registered-adapter rejection finalization, daemon active-lease renewal, expired-lease recovery, stop/stale linked-run guards, and lease mutation authority guards, objective-runner pre-lease autonomy gating, and evidence-to-trace projection cost. The synthesis gate composes the no-reference readiness, efficiency, and passive microbenchmark summaries into the release posture, including adopted reference-pattern ids, deliberate non-adoption ids, passive workflow/scenario/replay posture, and the balanced/needs-review security-versus-complexity posture. This release gate does not preflight backends, call providers, call the network, execute adapters, replay captured logs by executing side effects, read extension bodies, read artifact bodies, include transcript contents, clear pending action metadata, mutate files, or grant permissions. If session status projection finds stale `active_run_id` pointers, normal doctor runs warn only; explicit `harness doctor --repair` clears only those missing-run pointers and appends session repair evidence without deleting runs, tasks, artifacts, messages, or events. Operator/session projections expose the same stale pointer as `harness.session_active_run_reference/v1` with the explicit repair command; they do not repair it implicitly. CLI, attached HTTP session projections, the dashboard, the session pane, and right-pane attention rows also expose compact transcript health without malformed line bodies, so corrupted local transcript JSONL is visible without turning passive inspection into execution authority.

Bare `harness` with no prompt launches the unified Textual app: passive dashboard context, palette/search sections, and the real chat/orchestrator prompt in one terminal surface. `harness --output json` is a read-only context probe that reports `harness.chat/v1` without launching the UI. `harness --plain` runs the line-oriented chat fallback for tests and unsuitable terminals. `--codex-like` starts the session in a testing-friendly foreground action mode where one explicit confirmation can create the approved Harness records and drive registered-adapter dispatch.

The unified app is a conversational operator shell over explicit harness actions: it can initialize project state with `/init`, provide deterministic local guidance, inspect state, select an orchestrator, draft objective/task graphs when policy requires review, acquire daemon run-once leases, and dispatch already-leased work only through registered adapters. Repository summaries route to `read_only_summary/read_only_repo_summary`; repo planning requests route to `repo_planning/repo_planning`; coding-fix and file-write requests route to a bounded reviewed workflow with `repo_planning/repo_planning`, `codex_isolated_edit/codex_code_edit`, sandbox-test evidence, implementation review, security review, and final synthesis. Reviewed workflow templates declare per-task `harness.workflow_agent_selection/v1` requirements; drafts select each task agent through `harness.delegate_allocation/v1` and persist a compact `delegate_allocation` receipt in task metadata, so reviewer and specialist selection is inspectable without becoming execution authority. Under the default `supervised-codex` profile, chat-routed isolated-edit contracts can auto-start without a live approval prompt after deterministic policy evaluation creates scoped authority evidence. External filesystem write requests such as Downloads or Desktop are blocked before orchestration with explicit boundary evidence instead of prompting. Dirty active Git repositories use an isolated copy from the current workspace state for supervised Codex edits, while active apply-back remains a separate boundary. Manual mode still renders interpreted intent, proposed action, equivalent commands, safety boundary, required approvals, and the confirmation prompt. Results show task/adapter/lease/run/artifact evidence and next inspection commands. Session tools such as `cd`, `pwd`, `read`, `grep`, `glob`, `git-diff`, and permissioned `shell` route through the session-tool gateway with persisted evidence before display. Shell is not ambient generic shell access: it is exact-permission, bounded, non-idempotent execution. The app does not persist chat history or mutate active repository files from chat/model text outside the explicit foreground prompt and registered adapter paths.

The dashboard, palette, and slash-command sections remain passive read-only context. They show project state, summary counts, imported agents, tasks, active leases, daemon events, recent runs, safety reminders, static generated terminal pixel art, local in-memory search over loaded dashboard and command metadata, session-local section collapse, and palette-only focus. They do not execute commands, spawn subprocesses, invoke a shell, copy commands to the clipboard, mutate harness state, persist UI preferences, load image files at runtime, or call providers. `home` and `quickstart agent` remain read-only/non-mutating orientation commands. `tui-home set-image` is an explicit local visual-customization command that imports the provided image into tracked static TUI art files; it does not touch project runtime state, execute adapters, preflight backends, or expose image contents.

`harness --output json` includes registered adapters for compatibility plus the richer capability catalog, runtime controls summary, explicit memory summary, and orchestration progress summary when project state exists. These fields are app context only; they do not grant execution authority.

`harness core run` is the minimal headless backend loop for one vertical slice. It creates existing Harness project state when needed, records a session/objective/task, evaluates descriptor approval, policy, dependency, and active-lease eligibility before lease acquisition, leases only the named task, dispatches only through the registered adapter dispatcher, writes append-only run evidence and manifests when a run is created, and returns a concise JSON summary. The initial modes are `dry_run`, `repo_planning`, and `codex_isolated_edit`; the Codex-backed modes still fail closed without scoped hosted-boundary approval before attempts, leases, runs, backend preflight, or adapter dispatch. The narrow foreground JSON aliases `harness "goal" --agent plan --output json` and `harness "goal" --agent build --output json` consume this same service path; text output, direct active-workspace mode, session modifiers, file attachments, and mention-only native aliases remain on their existing compatibility paths. In the unified app, `/plan-mode [status|on|off]`, `/browse <url>`, and `/research <query>` are first-class session-tool routes: plan mode records session-local planning metadata, browsing routes through `web-fetch`, and deep research routes through `web-search` with deep-search arguments. The web routes validate project `web_tools` configuration and require exact external-network approval before any request; approved results are saved as run/artifact evidence before display. `harness core inspect-evidence` returns the canonical bundled read-only evidence envelope for a run or task, including run/task/blocked-state/event/artifact-metadata projections where available. `harness core inspect-run` returns the canonical read-only run projection used for backend stabilization; it reports persisted ids, lease/task/adapter status, manifest path, artifact metadata, policy hash, blocked reasons, and next commands without reading artifact bodies. `harness core inspect-events` returns sanitized persisted run events through the same projection layer. `harness core inspect-task` returns the matching task or blocked-state projection for tasks that have run evidence or persisted no-run rejection evidence.

`harness show <run_id> --output json` is a compatibility wrapper over the canonical bundled evidence projection. It returns `harness.show/v2` with `core_evidence` containing the same bundle as `harness core inspect-evidence --run <run_id>`. `harness tasks inspect <task_id> --output json` returns `harness.tasks_inspect/v2` with `core_evidence` when run or blocked-task evidence exists and a read-only `replay_receipts` projection of any `harness.task_replay_receipt/v1` attempt or retry receipts linked to the task. `harness events <run_id> --output json` returns `harness.events_inspect/v2` with `core_events` from the canonical run-event projection. Text output remains legacy, and run/task listing, task mutation, tailing, JSONL event output, event following, and artifact commands keep their existing contracts.

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
harness protocols list --project . --output json
harness protocols inspect local_server_openapi --project . --output json
harness protocols inspect mcp_tool --project . --output json
harness protocols inspect a2a_remote_agent --project . --output json
harness schemas list --project . --output json
harness schemas inspect agent_handoff_envelope --project . --output json
harness schemas inspect objective_evidence_chain --project . --output json
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

`harness protocols list` returns `harness.external_protocol_catalog/v1`, a read-only compatibility catalog for external protocol surfaces selected from the reference systems. It reports implemented model-provider adapters and local session tools, the local server OpenAPI document as `metadata_only`, cached MCP resource reads as `cached_resource_only`, and MCP tool execution, external OpenAPI tool import, A2A remote-agent interop, and gRPC remote tooling as `fail_closed`. Remote and extension descriptors also expose required telemetry contracts such as W3C trace context propagation, GenAI agent/tool span attributes, and MCP client span semantics before any future execution path can be enabled. Listing or inspecting these descriptors does not initialize projects, start servers or MCP processes, open network channels, execute tools or agents, read credentials, mutate files, grant permissions, or add protocol bodies to model context. `harness protocols inspect <id>` returns `harness.external_protocol_descriptor/v1` for one surface, including blocked reasons, authority flags, reference patterns, telemetry contracts, and next actions needed before a future implementation could be enabled safely.

`harness schemas list` returns `harness.schema_contract_catalog/v1`, a passive compatibility registry for critical orchestration payloads including `harness.agent_contract/v1`, `harness.agent_discovery_catalog/v1`, `harness.agent_handoff_envelope/v1`, `harness.delegate_budget/v1`, `harness.task_replay_receipt/v1`, `harness.external_protocol_catalog/v1`, readiness, efficiency, synthesis, orchestration replay drift audits, reviewed workflow templates, workflow agent-selection requirements, workflow coordination contracts, objective batch plans, objective evidence, checkpoint evidence, trace export, sandbox profile contracts, session tool policy, and local OpenAPI contracts. `harness schemas inspect <id>` returns `harness.schema_contract_descriptor/v1` with owner, producer, consumer, validation surface, compatibility policy, upgrade notes, reference patterns, and non-authority flags. Listing or inspecting schemas does not initialize projects, read artifact bodies, import reference code, start processes, call providers or networks, execute tools or agents, mutate files, add model context, read credentials, or grant permissions.

Model selection validation applies capability metadata before execution. Unsupported context-window requests, output limits, requested reasoning levels, input modalities, and required tool support fail closed before adapter dispatch. Runtime provider events include normalized usage/cost evidence when providers report usage: `normalized_usage` contains input, output, cache-read, cache-write, and total token counts, `provider_reported_cost` is preserved when present, and `estimated_cost` is populated when model cost metadata is available.

Runtime context-overflow recovery retries only against the same selected provider/model. Before that retry, Harness may insert a deterministic local compaction summary as a system message. The compaction receipt is `harness.runtime_compaction/v1` and records retained, dropped, grouped, and summarized session message ids, summary id, retained/dropped group ids, content-policy flags, and `provider_summarization_used=false`; it does not call a hidden summarizer, switch providers, read artifact bodies, call the network, mutate files, or grant permissions.

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
harness agents contract my_agent --project . --output json
harness agents discover --project . --workbench coding --output json
harness agents allocate --project . --workbench coding --task-type security_review --required-kind reviewer --required-tag security --required-tool-policy read_only --max-candidates 1 --output json
harness agents preview-imported my_agent --project . --output json
harness agents remove my_agent --project . --output json
```

Imported agents remain declarative metadata. Importing an agent does not grant new tools, create tasks, create runs, or start background work. `harness agents contract <agent_id>` returns `harness.agent_contract/v1`, a read-only canonical identity contract for built-in or imported project agents. The contract separates agent identity from orchestration policy: it reports source kind, model profile, backend id, tool policy, allowed/approval-required declared tool ids, input/output contract labels, profile preferences, forbidden actions, budget source, trace requirements, contract SHA-256, and explicit authority flags. `harness agents discover` returns `harness.agent_discovery_catalog/v1`, local A2A-AgentCard-inspired metadata for built-in and imported agents. `harness agents allocate` returns `harness.delegate_allocation/v1`, a deterministic Contract-Net-style bid preview for delegate selection. Discovery and allocation do not initialize projects for built-in agents, read project agent source bodies, create task records, call providers, call networks, execute tools or agents, read credentials, mutate files, grant budgets, grant permissions, or make the agent executable. Attached clients can read the same metadata at `GET /agents/discovery` and `GET /agents/allocation`.

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
harness objectives add --title "Draft objective" --draft --project . --output json
harness objectives list --project .
harness objectives inspect objective_abc123 --project . --output json
harness objectives start objective_abc123 --reason "Ready to dispatch" --project . --output json
harness objectives suspend objective_abc123 --reason "Waiting for supervisor input" --project . --output json
harness objectives resume objective_abc123 --reason "Supervisor input received" --project . --output json
harness objectives timeout objective_abc123 --reason "Deadline exceeded" --project . --output json
harness objectives retry objective_abc123 --reason "Retry retryable failed work" --project . --output json
harness objectives complete objective_abc123 --reason "Accepted final evidence" --project . --output json
harness objectives cancel objective_abc123 --reason "Superseded by objective_xyz789" --project . --output json
harness objectives checkpoints create objective_abc123 --label "Supervisor review" --reason "Review before dispatch" --project . --output json
harness objectives checkpoints gate objective_abc123 --project . --output json
harness objectives checkpoints verify objective_abc123 --project . --output json
harness objectives checkpoints approve objective_abc123 ockpt_abc123 --approval-id approval_abc123 --project . --output json
harness objectives run objective_abc123 --project . --autonomy safe-local --timeout-seconds 900 --output json
harness objectives verify-evidence objective_abc123 --project . --output json
harness objectives reconcile-evidence objective_abc123 --project . --dry-run --output json
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
harness tasks status --project .
harness tasks status task_abc123 --project .
harness tasks set-status task_abc123 succeeded --project . --output json
harness tasks cancel task_abc123 --project . --output json
harness tasks retry task_abc123 --project . --output json
harness tasks run-next --project . --output json
```

Task queue commands are manual SQLite control-plane operations. `tasks retry` only requeues failed tasks when the registered adapter replay policy allows it: `safe` and `idempotent_with_key` adapters may retry, `requires_fresh_approval` adapters need a valid fresh scoped approval before the failed task is made selectable again, and `not_replayable` adapters remain failed. Accepted retries write a compact `harness.task_replay_receipt/v1` receipt on the task transition with the replay policy, retry gate, task idempotency key, prior attempt count, approval revalidation state, and active-lease duplicate guard. `tasks run-next` evaluates descriptor approval, runtime controls, open adapter breakers, policy, dependency, and active-lease eligibility before creating a local attempt and lease; new attempts also carry `harness.task_replay_receipt/v1` metadata with the attempt idempotency key and prior attempt count. `tasks inspect --output json` exposes those receipts through a sanitized `harness.task_replay_receipts_projection/v1` summary with legacy-missing counts and malformed-receipt gaps. Approval-required, control-disabled, breaker-open, or policy-forbidden work is returned as `pause_reasons` without a lease, agent execution, backend preflight, run, or artifact.

`objectives add --draft` creates a non-dispatchable `created` objective, and `objectives start` is the explicit `created -> active` lifecycle mutation. `objectives start`, `objectives suspend`, `objectives resume`, `objectives timeout`, `objectives complete`, and `objectives cancel` return `harness.objective_lifecycle/v1`, while retry returns `harness.objective_retry/v1`; both persist redacted lifecycle events in objective metadata, validate allowed status transitions, and report `operator_authority` flags showing that the command did not execute adapters, call providers, call the network, mutate repository files, grant permissions, or create future authority. Created objectives are blocked until started: progress points at `objectives start` and does not advertise `daemon run-once`, while `objectives run` and `daemon run-autonomous` stop with `objective_inactive` before attempts, leases, runs, backend preflight, or dispatch. Suspended objectives are blocked but resumable: progress points at `objectives resume` and does not advertise dispatch. Required objective checkpoints serialize human-in-the-loop waits as objective status `waiting_approval`; progress points at checkpoint gate/list commands and does not advertise dispatch, and approving the last required checkpoint resumes the objective to `active`. `objectives retry` moves an active or timed-out objective through `retrying`, requeues only failed tasks whose registered adapter replay policy permits retry, and returns the objective to `active` without creating attempts, leases, runs, backend preflight, or dispatch. Timed-out objectives are terminal for automatic dispatch unless explicitly retried; cancelled and completed objectives remain terminal inspection states.

`objectives checkpoints` records durable supervisor gates for an objective. Checkpoint events are append-only JSONL evidence under `.harness/autonomy/objectives/`, separate from task execution events. Creating a required checkpoint moves an active objective to `waiting_approval`; required checkpoints block `objectives run` and `daemon run-autonomous` before lease acquisition until they are approved; rejected checkpoints remain blocking. Approving the final required checkpoint moves a waiting objective back to `active`. `objectives checkpoints verify` is read-only and validates checkpoint event JSONL parsing, event envelope fields, event id/index sequence, hash-chain links, timezone-aware timestamp order, objective scope, and create/resolve lifecycle records without reading artifact bodies, executing adapters, calling providers, mutating files, or granting permissions. Corrupt checkpoint evidence makes the checkpoint gate block and makes readiness fail; checkpoint create/approve/reject refuse to append to an untrusted chain. Checkpoint records include `contents_included=false`, `model_context_allowed=false`, `execution_allowed=false`, `network_required=false`, `mutation_allowed=false`, and `permission_granting=false`.

`objectives run` is a bounded autonomous objective runner over existing task graphs. It first verifies the objective is still active, then evaluates the optional wall-clock timeout budget, required objective checkpoints, ready or dependency-unblocked tasks within the objective, and the selected autonomy profile and adapter metadata before acquiring any new lease. If `--timeout-seconds` has already expired, or expires before a later scheduling loop, the objective is marked `timed_out` with lifecycle metadata and the run stops with `stop_reason=timed_out`, no additional attempts, leases, runs, backend preflight, or dispatch. It stops with verifiable `autonomy_stopped` evidence when approval or denial blocks a candidate. If pre-lease autonomy passes but the atomic guarded lease selector catches stale approval, runtime-control, adapter-breaker, dependency, or active-lease state, it stops with `lease_guard_stopped` evidence and `lease_id=null` instead of creating an attempt, lease, run, backend preflight, or dispatch. Only auto-allowed candidates are leased and dispatched through registered adapters owned by the autonomous runner. It writes objective JSONL evidence under `.harness/autonomy/objectives/` and stops on success, timeout, inactive objective status, checkpoint block, blocked state, approval requirement, denial, execution failure, or budget exhaustion. For bounded parallel runs, `batch_planned` events record typed `harness.objective_batch_plan/v1` payloads with scheduler policy, policy sort keys, capacity, candidate task ids, selected task/lease pairs, resumed-vs-new selection source, dependency snapshots, schedule profiles, and autonomy decision ids before dispatch; pre-lease stops have no selected task/lease pair and record `lease_id=null` in the stopped decision. The verifier checks selected decision ids against persisted decision records for run scope, task, lease, dispatch tool, adapter, task type, and decision status. It also recomputes priority/critical-path/downstream schedule profiles from persisted task state, verifies candidate ordering by policy, verifies fresh selections are the policy prefix after resumed active leases, verifies resumed leases are ordered by acquisition time and lease id, and requires each selected task/lease pair to have exactly one terminal `adapter_dispatched` or `execution_error` event in that batch. `batch_completed` events record batch-local dispatch count, cumulative dispatch count, and execution-error count. Worker-level `execution_error` events carry the same autonomy decision, approval, outcome, adapter, policy, task, and lease ids as normal dispatch evidence, with `ok=false` outcome records under `.harness/autonomy/outcomes.jsonl`. It does not create new tasks, expand graphs, call arbitrary tools, bypass approvals, lease inactive, timed-out, or approval-blocked work, or mutate the active repo.

`objectives verify-evidence` is read-only. It parses the objective JSONL evidence and verifies that objective run event ids/indexes, event-type payload schemas including checkpoint-blocked stops, lease-guard stops, linked execution-error outcomes, and explicit reconciliation records, event hash-chain integrity, event timestamps, selected leases, batch lifecycle records, batch-local and cumulative dispatch counts, execution-error counts, dispatch events, reconciled run records, run records, artifact ids, stopped summaries, and autonomy decision/approval/outcome records still link to persisted SQLite state. Dispatch events must agree with persisted run and lease terminal state: `ok=true` requires a completed run status, `ok=false` with a run requires a failed run status, and the event decision must match the released lease decision metadata. Dispatch and execution-error evidence must also have internally consistent autonomy authority records: the referenced decision, approval, and outcome must agree on dispatch tool identity, decision status, task type, and derived authority payload fields. Non-dispatch `autonomy_stopped` and `lease_guard_stopped` evidence must reference a persisted decision record, and any embedded stop decision copy must match that persisted record instead of acting as an authority fallback. Text output highlights payload schema, event identity, hash-chain, timestamp, event-count, and chain-head status before the per-check table. It does not read artifact bodies, create new records, execute adapters, grant approvals, or repair state. `objectives reconcile-evidence` is the explicit repair path for objectives that already have persisted run records but no objective JSONL chain. `--dry-run` previews the write; the real command writes only `.harness/autonomy/objectives/<objective_id>.jsonl` with `started`, `reconciled_existing_run`, and `stopped` records, then verifies the chain. Reconciliation does not mutate objectives, tasks, runs, sessions, artifacts, repository files, approvals, providers, network state, or permissions, and it does not claim historical runs were autonomous dispatches. The bearer-auth local server exposes the same passive attached-client surfaces as `GET /objectives/{objective_id}/evidence` and `GET /objectives/{objective_id}/trace`; both report no execution, no provider call, no filesystem mutation, no network call, and no permission grant.

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

`daemon run-once` is lease-only. It pauses tasks whose registered adapter descriptor requires approval, whose adapter/task/backend/hosted-boundary control is disabled, or whose adapter breaker is open before creating a lease or task attempt. It renews only coherent daemon-owned active leases; inconsistent active leases are released with `release_inconsistent_lease` evidence instead of being kept alive indefinitely. `daemon adapters` lists registered adapter descriptors without preflighting backends or executing anything. `daemon inspect-lease` is read-only and reports generic `execution_eligibility`. `daemon execute` is a registered-adapter dispatcher for already-leased tasks only and executes under the lease owner recorded in SQLite: no adapter means no execution, unknown adapter means fail closed, unresolved or unknown sandbox profile means fail closed before dispatch, and adapter descriptors are documentation and validation metadata rather than permission grants. Registered-adapter run manifests derive sandbox evidence and the serialized delegate-budget snapshot from the selected adapter descriptor, not from ad hoc task-type inference. Registered-adapter no-run rejections release the active lease and mark the linked attempt/task `failed` or `waiting_approval`; `duplicate_run` and `lease_owner_mismatch` decisions remain non-mutating because existing run evidence or another owner may be authoritative. Registered-adapter boundary failures after dispatch also finalize failed run evidence and feed adapter breaker telemetry. `daemon recover` reconciles existing completed/failed linked-run evidence without creating a second run, fails expired leases with missing or non-terminal linked runs for operator inspection, and expires inconsistent active leases so stale locks do not block later eligible work.

`daemon run-autonomous` runs the next active objective that already has runnable work using the graph-driven objective runner and the selected autonomy profile. It resumes only leases owned by the autonomous runner; active leases owned by another runner become visible `active_lease` pause reasons instead of being executed. Bounded parallel scheduling writes `batch_planned` JSONL evidence before each batch and `batch_completed` count evidence after each batch, so operators can inspect selected leases, resumed-vs-new selection source, dependency state, scheduler-policy ordering, and dispatch totals without reading SQLite manually. It is still bounded by leases, adapter descriptors, approval profiles, runtime controls, adapter breakers, budgets, and evidence requirements.

`daemon inspect-lease` and `daemon execute` include `blocked_state_explanations` in JSON and print `Blocked state` rows in text output. These explanations normalize missing approvals, disabled adapters, unsafe metadata, unknown adapters, sandbox profile evidence gaps, breaker-open state, and forbidden path or secret-like blocks without changing the underlying decision.

## Runtime Controls

```bash
harness controls list --project . --output json
harness controls disable --target-kind adapter --target-id dry_run --reason "pause dry run" --project . --output json
harness controls enable --target-kind adapter --target-id dry_run --project . --output json
harness controls breaker-status --project . --output json
harness controls breaker-reset dry_run --reason "operator reviewed failures" --project . --output json
```

Runtime controls are local kill switches and adapter breakers. They only narrow execution authority: a disabled control or open breaker can pause guarded queue selection, deny generic registered dispatch, hide capability availability, or stop autonomous objective scheduling, but enabling a control cannot bypass lease, policy, approval, sandbox, or adapter validation. Descriptor-bound controls for `adapter`, `task_type`, `backend`, and `hosted_boundary` use the same registered-adapter matcher in daemon/manual queue selection, daemon execution, capability projection, and objective-runner autonomy evaluation, so a `backend:codex_cli` pause blocks Codex-backed planning/edit adapters before guarded lease acquisition as well as at direct `daemon execute`.

## v1.8 Local App Surfaces

```bash
harness capabilities list --project . --output json
harness capabilities inspect dry_run --project . --output json
harness memory save-note "Local operator note" --scope project --project . --output json
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

`harness capabilities list` returns `harness.capability_catalog/v1`, a read-only view over registered execution adapters, required approvals, sandbox/readiness notes, serialized delegate budgets, safety notes, runtime controls, and equivalent commands. `harness capabilities inspect` returns one capability or a schema-stable fail-closed JSON error. Shared registered-task validation and registered-adapter dispatch both fail closed if the selected descriptor's `harness.delegate_budget/v1` budget is invalid or does not align with its sandbox profile. Task metadata may narrow runtime limits, but known numeric budget fields must be valid floor-respecting numbers: runtime/model/tool/token/cost ceilings are non-negative, branch fan-out must be at least one, and requested ceilings above a descriptor budget are rejected before task creation or adapter dispatch.

Unavailable capabilities include structured `blocked_state_explanations` alongside existing readiness reasons so operators can see whether a capability is paused by a runtime control, breaker, approval requirement, or other local policy evidence.

`harness memory save-note`, `save-derived`, `list`, `inspect`, and `forget` return `harness.memory_record/v1` or `harness.memory_records/v1`. Memory records are explicit local operator notes or artifact-derived working memory, scoped by project/workbench/agent/objective/task, redacted before persistence when secret-looking content appears, and forgotten by replacing retained content with `[FORGOTTEN]`. Derived memory source kinds include `artifact_summary`, `objective_state`, `run_review`, and `failed_attempt_summary`; they must link to source ids and remain non-authoritative for permissions, policy, or approvals.

`harness progress --objective` returns `harness.orchestration_progress/v1`, a read-only objective/task/lease/run state summary with mode, blockers, active lease/run ids, task rows, checkpoint gate status, deterministic next commands, and an `objective_evidence` summary when objective JSONL evidence exists. The summary reports checkpoint blockers and evidence verification status, event count, head hash, and key check statuses without reading artifact bodies or repairing evidence; equivalent commands include read-only checkpoint gate, checkpoint list, objective evidence verification, and objective trace export commands when relevant.

The chat aliases `/capabilities`, `/memory`, `/remember <text>`, `/forget <memory_id>`, `/progress [objective_id]`, “show capabilities”, “what can Harness do here?”, “show memory”, “show progress”, and “where are we” render these same local surfaces. The TUI right panel prefers capability rows and adds a Progress section. None of these surfaces create tasks, acquire leases, create runs, dispatch adapters, call providers, preflight backends, touch Docker, invoke shell commands, or mutate active repository files. `/tools` and `harness session tools` show the complete tool catalog plus `policy.exposure`; provider-native model schemas use `policy.exposure.model_visible` so approval-gated shell, write, network, extension, task-spawning, and internal invalid-call recovery tools are not frontloaded into the default model-visible set. Default model-visible object schemas must reject unspecified top-level arguments.

## Registered Execution Adapters

```bash
harness daemon execute-dry-run task_lease_abc123 --project . --output json
harness daemon execute-read-only task_lease_abc123 --project . --output json
harness daemon execute task_lease_abc123 --project . --output json
```

`execute-dry-run` and `execute-read-only` are compatibility commands with their original JSON contracts. The generic `daemon execute` command dispatches the same already-leased tasks through the registered-adapter registry and returns `harness.daemon_execute/v1`. `execute-read-only` uses the same no-run rejection finalization policy as generic registered dispatch: missing approval marks the attempt/task `waiting_approval`, deny/unavailable paths mark them `failed`, and the active lease is released without creating a run.

The read-only adapter requires an existing active daemon lease, exact metadata `execution_adapter=read_only_summary` plus `task_type=read_only_repo_summary`, and a valid hosted-boundary Codex approval profile for `read_only_repo_summary`. It uses the supervised `codex_cli` subscription backend with ChatGPT auth, `gpt-5.5`, low reasoning effort, and Codex read-only sandbox mode. It does not use the local model backend as a fallback.

The Codex isolated adapter requires exact metadata `execution_adapter=codex_isolated_edit` plus `task_type=codex_code_edit`, a valid hosted-boundary Codex approval profile, and a safe `codex_cli` backend. Hosted-boundary approval is not apply-back approval: active repo mutation remains denied by default unless the explicit apply-back approval path approves the inspected diff.

The repo planning adapter requires exact metadata `execution_adapter=repo_planning` plus `task_type=repo_planning`, a valid hosted-boundary Codex approval profile, and a safe `codex_cli` backend. It uses Codex read-only sandbox mode to produce planning evidence and fails the task if the read-only policy check detects active repository changes.

The session child-task adapter uses exact metadata `execution_adapter=session_child_task` plus `task_type=session_delegate` only for record-only tasks created by the governed session `task` tool. It persists a compact `harness.agent_handoff_envelope/v1` id, payload hash, W3C-style `traceparent`, and embedded `harness.agent_contract/v1` id/hash on the task metadata. `harness handoffs inspect-task <task_id> --output json` can reconstruct the full read-only envelope with delegate budget, idempotency key, parent/child session ids, allowed tools, agent contract, authority flags, and validation errors. The envelope is schema compatibility metadata: it validates parent/child session linkage, task evidence, and resolved agent identity, but daemon dispatch for this adapter is denied by policy because execution remains inside explicit session-tool paths. Inspecting the envelope does not execute adapters, start processes, call networks, read credentials, read artifact bodies, read project agent source bodies, add model context, mutate files, or grant permissions.

The session read-tools adapter uses exact metadata `execution_adapter=session_read_tools` and task types `session_operator`, `session_plan`, or `session_read_only_research`. When task metadata omits `allowed_tools`, the adapter advertises only the default read-only inspection set `read`, `glob`, `grep`, and `artifact-read`, after applying the central model-visible exposure policy. Explicit `allowed_tools` metadata is the governed path for broader session tools such as `shell`; those calls still go through exact session permission records. Explicit allowlists must be non-empty lists of known enabled and project-policy-enabled session tool ids, and malformed, unknown, disabled, config-blocked, capability-blocked, or internal-only ids are rejected before run creation as unsafe metadata. The internal `invalid` recovery tool is never advertised as a native schema.

The TUI command palette and right-panel context include copy-only templates for repo-planning task creation and generic registered dispatch. Displaying these commands does not execute them, acquire leases, call providers, run Docker, or grant adapter permissions.

Registered adapters do not authorize Docker-from-queue, generic shell access, hosted fallback, paid fallback, OpenAI API usage, MCP/A2A, browser/email/calendar tools, broker actions, live trading, order placement, or unmanaged daemon loops.

## Runtime Evidence

```bash
harness runs --project .
harness runs prune --keep 20 --project .
harness show run_abc123 --project . --output json
harness artifacts list run_abc123 --project .
harness artifacts inspect artifact_abc123 --project .
harness policy explain --project .
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
harness evals run --suite orchestration-readiness --project . --output json
harness evals run --suite orchestration-efficiency --project . --output json
harness evals run --suite orchestration-microbenchmarks --project . --output json
harness evals run --suite orchestration-replay --project . --output json
harness evals run --suite orchestration-workflows --project . --output json
harness evals run --suite orchestration-synthesis --project . --output json
harness orchestration audit --project . --reference-root ../harness-references --output json
harness orchestration replay --project . --output json
harness orchestration workflows --project . --output json
harness orchestration synthesis --project . --reference-root ../harness-references --output json
harness security audit --project . --output json
harness security check --project . --output json
harness integrity check --project . --output json
harness traces export run_abc123 --format otel-json --project . --output json
harness traces export-objective objective_abc123 --format otel-json --project . --output json
```

Evidence commands report metadata, manifests, hashes, verification status, policy decisions, autonomy profiles, local security findings, local integrity checks, security-layer audit checks, orchestration-readiness checks, orchestration-efficiency measurements, orchestration microbenchmarks, orchestration synthesis, passive orchestration replay drift audits, and trace/export envelopes.
The security check is metadata-only: it inspects persisted local records and manifests without reading artifact bodies, calling providers, touching Docker, or creating new runtime evidence.
The integrity check is package/local metadata-only: it hashes built-in specs, adapter descriptors, workflow templates, security docs when present, and static TUI assets without initializing project state or running adapters.
The security-layer audit verifies the local-first completion scope, including run trace exportability/provenance, run-event payload metadata coverage, registered-adapter delegate-budget trace evidence, linked lease/queue trace evidence for dispatched runs, autonomous objective JSONL linkage, objective trace exportability/provenance, and objective-event payload metadata coverage when objective evidence is present, without remediation or hidden execution.
The orchestration-readiness audit returns `harness.orchestration_readiness_audit/v1` and maps reference-informed orchestration patterns to current Harness surfaces: durable supervisor state, typed child-task delegation, serialized `harness.delegate_budget/v1` limits for timeout, CPU, memory, model/tool/cost, filesystem, egress, and fan-out, supervisor checkpoints, bounded parallel scheduling, workflow coordination contracts, append-only objective evidence, OTEL-shaped traces, pending chat action recovery/audit/cleanup projections, sandboxed adapters, runtime controls, progress observability, default tool exposure, passive external-protocol compatibility, schema compatibility contracts, passive replay-drift detection, agentic security controls for memory poisoning/insecure inter-agent communication/cascading failure risks, apply-back governance, and reference-repository hygiene.
The orchestration-efficiency audit returns `harness.orchestration_efficiency/v1`; it is a read-only security-versus-complexity gate that checks adapter complexity against sandbox/approval/autonomy/replay controls, verifies delegate budget ceilings, verifies delegate-budget-to-sandbox-profile alignment, verifies retry/idempotency policy and `harness.task_replay_receipt/v1` attempt receipt shape, runs a deterministic in-process bounded critical-path scheduler probe, exposes `harness.orchestration_microbenchmark_contracts/v1` for the report-recommended benchmark matrix, exposes `harness.orchestration_live_benchmark_permits/v1` for the approval/budget/boundary contracts required by live-only benchmark rows, and measures existing objective/run evidence-to-trace projection cost when local runtime state already exists.
The `orchestration-microbenchmarks` suite returns `harness.orchestration_microbenchmarks/v1`; it executes only bounded in-process/passive timings for handoff projection, fan-out/fan-in scheduling, checkpoint verification when runtime state exists, tool-adapter projection, retry policy validation, trace projection when runtime evidence exists, and verification-gate projection.
The `orchestration-replay` suite and `harness orchestration replay` command return `harness.orchestration_replay_audit/v1`; they run synthetic replay cases for happy paths, duplicate dispatch, slow-branch barriers, approval rejects, and missing terminal events, then passively reduce existing objective JSONL evidence when project state exists.
The `orchestration-workflows` suite and `harness orchestration workflows` command return `harness.workflow_coordination_catalog/v1`; they describe the adopted workflow-pattern contracts and state classes without executing orchestration. Pattern rows cover durable supervisor, sequential steps, bounded fan-out/fan-in barriers, typed handoffs, human approval pauses, append-only replay, external protocol boundaries, and memory context boundaries. State-class rows separate session state, workflow state, long-term memory state, and artifact/evidence state so Microsoft-style workflow patterns, Temporal-style durability, LangGraph-style state graphs, and ADK/OpenAI handoff ideas remain inspectable contracts rather than imported runtimes.
The `orchestration-scenarios` suite and `harness orchestration scenarios` command return `harness.orchestration_scenario_catalog/v1`; they make the report-recommended layered test strategy release-visible without executing live work. Rows cover duplicate dispatch/redelivery, slow branch barriers, approval reject pauses, checkpoint reject stops, missing terminal events, unsafe memory-to-hosted-model propagation, remote protocol fail-closed boundaries, retry/idempotency policy, and live benchmark explicit permits across unit, contract, replay, scenario, security, and benchmark layers.
Captured evidence replay compares event semantics and verification status only; it does not dispatch adapters, call tools or providers, read artifact bodies, import reference code, include captured payloads as model context, mutate files, or grant permission.
The `orchestration-synthesis` suite and `harness orchestration synthesis` command return `harness.orchestration_synthesis/v1`; they combine reference repository metadata, readiness summaries, efficiency summaries, microbenchmark summaries, replay drift summaries, adopted reference-pattern decisions, deliberate non-adoptions, and the current security-versus-complexity posture into one report.
Timed rows include a non-blocking `harness.orchestration_microbenchmark_guardrail/v1` local threshold envelope; provider-backed or sandbox-backed rows such as sandbox startup and shared model contention are marked `skipped`/`explicit_live_required` instead of starting them, and their `measurements.live_permit` fields report `harness.orchestration_live_benchmark_permit/v1` with `automated_execution_allowed=false` and `release_blocking=false`.
The external-protocol compatibility check verifies that model-provider protocols are registered, local OpenAPI remains metadata-only, cached MCP resources stay cached-resource-only, and MCP tool execution, external OpenAPI import, A2A remote agents, and gRPC remote tools stay fail-closed and non-model-visible by default. It also verifies that remote and extension protocol descriptors declare W3C trace context propagation and the relevant OpenTelemetry GenAI/MCP semantic span contract before they can move out of metadata-only or fail-closed status.
The tool-exposure readiness check also reports the `session_read_tools` default native schema set and fails if it drifts from `read`, `glob`, `grep`, and `artifact-read`, includes a non-model-visible tool, or advertises the internal `invalid` recovery tool.
The runtime-control readiness check reports active control targets, open breakers, and stale descriptor-bound controls whose target no longer maps to any registered adapter descriptor.
The `agentic_security_controls` readiness check aggregates existing passive controls into three release-visible risk rows: memory context remains local/passive unless an explicit future hosted path exists, typed handoff envelopes stay read-only with trace and payload hashes while remote agent protocols fail closed, and cascading-failure controls require bounded scheduling, safe replay policy for auto-allowed adapters, breaker visibility, and replay probes that detect duplicate or blocked dispatch.
The append-only objective-evidence check verifies every existing objective JSONL chain and warns when an objective already has run evidence but no objective JSONL chain, so missing provenance is visible without treating draft-only objectives as incomplete.
These audits, microbenchmarks, replay checks, and synthesis projections are read-only; they do not import reference code, include reference source bodies as model context, replay captured logs by executing adapters/tools/providers, clear pending action metadata, backfill objective evidence, call providers, call the network, mutate the filesystem, read artifact bodies, or grant permission.
The TUI cockpit Evidence section shows bounded passive readiness, efficiency, microbenchmark, and synthesis summaries for fast refreshes; its readiness row includes `deep_audit_required=true` and the explicit `harness orchestration audit --project . --output json` command for full evidence. The bearer-auth local server exposes the full passive readiness projection at `GET /orchestration/readiness` with reference metadata opt-in via `include_references=true`, the workflow coordination catalog at `GET /orchestration/workflows` with `harness.workflow_coordination_summary/v1`, the scenario conformance catalog at `GET /orchestration/scenarios` with `harness.orchestration_scenario_summary/v1`, the efficiency projection at `GET /orchestration/efficiency`, the passive microbenchmark suite at `GET /orchestration/microbenchmarks` with `harness.orchestration_microbenchmarks_summary/v1`, and the combined synthesis at `GET /orchestration/synthesis` with `harness.orchestration_synthesis_summary/v1`.
Run trace export emits run/event/artifact/backend/approval/policy/sandbox spans, and registered-adapter runs also emit `harness.delegate_budget` evidence plus `harness.queue` and `harness.lease` timing spans when the run is linked to a persisted lease attempt.
Objective trace export emits objective/objective-run/objective-event spans from persisted objective JSONL evidence, verification metadata, and the current objective evidence hash-chain head; its `ok` field follows objective evidence verification.
Event spans carry sanitized payload metadata, including payload SHA-256, byte size, and key list; secret-like payload values and keys are redacted before projection.
The bearer-auth local server mirrors trace export for attached clients with `GET /runs/{run_id}/trace`, `GET /objectives/{objective_id}/evidence`, and `GET /objectives/{objective_id}/trace`, all as read-only projections.
Evidence commands and server projections must not print artifact file contents, secret-like data, backend settings, API keys, environment variables, or provider configuration.

Autonomy policy inspection returns `harness.autonomy_policy_inspect/v1`. It is an explanation surface for built-in profiles such as `manual`, `safe-local`, `supervised-codex`, and `daemon-safe`; it does not execute tools, create approvals, mutate project state, or grant authority outside existing policy, sandbox, approval, lease, adapter, runtime-control, budget, and evidence gates.

Line-oriented chat defaults to `--autonomy supervised-codex`; `--autonomy manual` preserves interactive confirmation, and `--autonomous` remains shorthand for `--autonomy safe-local`. Non-manual autonomy affects only validated action contracts. It does not let the model call shell, mutate the active repo, apply back isolated changes, or bypass policy. For `supervised-codex` isolated-edit contracts, Harness can create the scoped hosted-provider authority record itself after policy evaluation so the workflow starts without a live approval prompt. Autonomous contract decisions are recorded under `.harness/autonomy/`.

`safe-local` can auto-create only local Harness control-plane records that pass the autonomy policy, including objectives, dry-run tasks, dry-run task graphs, and explicit project memory notes. Chat-created tasks and task graphs use stable idempotency keys to avoid duplicate objective, task, and checkpoint records for repeated equivalent requests. Pending chat action proposals are stored as inert active-session metadata so an interrupted session can recover the same visible draft or action contract and still require `/confirm` or `/decline`. Dashboard/session projections, right-pane attention rows, `/sessions`, `/api/session`, `/sessions/status`, `/sessions/{id}`, and `/sessions/{id}/status` show only compact recoverable pending-action summaries and active-run reference health; the metadata and projections do not execute, lease, dispatch, call providers, mutate files, repair stale pointers, or grant authority. Malformed or stale pending-action metadata is surfaced as an invalid/stale audit state rather than a confirmable action; inspect it with `harness sessions pending-action <session_id> --output json` or `GET /sessions/{session_id}/pending-action`, and clear only the proposal metadata with `harness sessions clear-pending-action <session_id>` or `DELETE /sessions/{session_id}/pending-action`. Stale session `active_run_id` references are surfaced as `harness.session_active_run_reference/v1`; clear only the missing-run pointer with explicit `harness doctor --repair`. These cleanup paths do not mutate objectives, tasks, leases, runs, approvals, artifacts, messages, active repository files, provider state, or permissions. Memory records remain scoped, hashed, redacted when needed, and non-authoritative for permissions or approvals.

Scoped hosted approval profiles can constrain autonomous Codex use by task type, adapter id, workbench id, objective id, autonomy scope, run count, total runtime, and context byte budget. These profiles still satisfy explicit autonomous adapter dispatch and objective-run hosted-boundary checks only inside their exact stored scope. Chat-routed `edit_isolated` contracts under `supervised-codex` use a narrower Toloclaw-style auto-transition: Harness creates scoped internal authority for Codex planning/edit execution, keeps writes in the isolated workspace, and leaves active-repo apply-back outside the auto path. Legacy hosted approvals without `--autonomy-scope supervised-codex` remain manual-flow approvals and do not satisfy strict autonomous Codex dispatch. They do not authorize apply-back, active repo writes, arbitrary network, shell commands, approval extension, or task type expansion.

`harness act` returns `harness.autonomous_read_loop/v1`. It runs a bounded autonomous act loop: read tools may run within budget, and side-effecting tool requests become Harness action contracts evaluated by the selected autonomy profile. Auto-allowed local control-plane contracts can create objectives, tasks, task graphs, and memory notes. When an auto-created task graph produces an objective, `harness act` can immediately run that objective through the autonomous objective runner and return task/lease/run/artifact evidence to the model loop.

Under `supervised-codex`, chat-routed `edit_isolated` requests auto-transition into the reviewed coding workflow without a live confirmation prompt. Reviewed workflow templates record and approve a required objective checkpoint from the explicit chat/action confirmation before any objective-run lease is acquired; this checkpoint is append-only supervision evidence for the confirmed graph, not hosted-provider authority or apply-back authority. Direct autonomous dispatch of `repo_planning` or `codex_isolated_edit` still requires a scoped hosted approval profile for the exact task type, adapter, objective/workbench scope, and autonomy scope. Isolated edits run in isolated workspaces, reviewer/final-synthesis tasks run as local evidence-producing tasks, and apply-back remains a separate higher boundary that is denied unless an explicit apply-back policy later permits it.

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
harness governance references-audit --project . --root ../harness-references --output json
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
- `harness.reference_repositories_audit/v1` for external reference repository audits.
- `harness.governance_network_policy_check/v1`, `harness.governance_network_check_url/v1`, and `harness.governance_download_quarantine/v1` for network policy evidence.
- `harness.governance_applyback_verdict/v1` for apply-back and promotion verdicts.

`governance merge-check` is a fail-closed evidence command. It runs local checks, writes local evidence under `.harness/governance/`, and exits nonzero for blocking verdicts. It does not merge branches, push commits, comment on pull requests, call providers, start adapters, execute arbitrary shell commands, or mutate the active repository.

`governance references-audit` inventories external reference repository checkouts, defaulting to a sibling `<project>-references` directory unless `--root` is supplied. It reports only Git metadata such as repo name, sanitized origin URL, branch, HEAD SHA, dirty counts, the curated expected repository set, missing or extra repository names, and local Git LFS materialized/unmaterialized file counts. It also reports static reference-profile metadata for the curated set: upstream project label, integration role, implementation guidance, repository pattern tags, required pattern coverage, covered patterns, and missing required patterns. This profile matrix explains why a checkout is useful for Harness orchestration without importing the source. It does not read source bodies, include reference contents as model context, run reference code, call the network, pull, fetch, clone, mutate repositories, or grant execution authority. Every repository row is marked `manual_review_required=true`, `license_review_required=true`, `contents_included=false`, `model_context_allowed=false`, `execution_allowed=false`, and `mutation_allowed=false`. Missing curated references, missing required reference-pattern coverage, or unmaterialized LFS files are readiness warnings, not authority grants.

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

The wheel smoke confirms console-script wiring, packaged built-in YAML availability, security-layer model availability, registered adapter descriptor integrity, workflow template contract integrity, and packaged security-sensitive docs. It remains local-only and non-executing.
