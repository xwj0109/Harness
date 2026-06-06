# Operator Guide

This guide covers the currently implemented operator flows:

- supervised isolated `codex_code_edit`;
- supervised read-only `repo_planning`;
- direct Docker-sandboxed test execution;
- model-visible Docker `run_tests` for `simple_code_edit`.

The harness does not commit or push changes for these flows. Paid/hosted model execution is available only through explicitly configured providers, selected model refs, provider/data-boundary approvals, credential resolution, and registered protocol adapters; it is never fallback. Generic shell execution, unconstrained autonomous workflow engines, runtime plugin execution, MCP, browser/email/calendar integrations, hosted fallback, and local fallback are outside the implemented scope.

Active model/provider behavior is tracked in `docs/plans/model_provider_completion_execution_plan.md`. The implemented registry surfaces can list, validate, inspect, select, favorite, default, discover, cache, and execute explicit model refs through registered protocol adapters. Provider accounts, local secret storage, OAuth/manual-token storage, shared CLI/server/TUI/runtime provider state, default-model resolution evidence, usage/cost evidence, retry/abort evidence, and provider-specific protocol adapters are wired. Catalog reads remain metadata-only and must not be treated as runtime provider readiness: runtime still validates the selected provider/model, checks hosted/paid/data-boundary approvals, resolves credentials only after those gates, and constructs only the selected provider adapter.

## Model Provider Concepts

Harness separates provider metadata, model metadata, account state, policy approval, and runtime execution so that catalog inspection stays safe while actual model calls remain explicit. A provider descriptor is the catalog record for a backend such as Codex CLI, a local OpenAI-compatible endpoint, OpenAI Responses, Anthropic Messages, Google Generative AI, Bedrock Converse, or a project-local custom provider. It names the provider id, display name, protocol adapter, endpoint policy, data boundary, billing boundary, discovery behavior, credential policy, and provider-level capabilities. A provider descriptor is not a credential and does not prove that the provider can run.

A model descriptor belongs to one provider and describes a concrete executable model id or aliasable model entry. It records the API model id, canonical `provider/model` ref, optional aliases and variants, protocol override when needed, context window, max output, supported modalities, tool support, reasoning support, cost metadata, source label, status, and blocked reasons. Catalog, CLI, server, TUI, and runtime validation all read the same descriptor data so an operator sees the same capabilities and block reasons everywhere.

Provider account state is the redacted binding between a provider and a credential source. Env-backed accounts store the environment variable name, API-key/static/OAuth/Codex-login accounts store only secret metadata and account status, and runtime credential resolution is the only path that can ask for secret material. Catalog commands, TUI projections, validation events, session evidence, provider stream events, and logs must report credential source and status without including raw credential values.

Data-boundary and approval state are separate from credentials. `local_only` providers can run only after local endpoint validation, hosted providers require hosted-boundary approval before hosted network execution, and external-router or other non-local boundaries require a matching data-boundary approval before credentials are resolved or network clients are built. Configuring a credential never enables a disabled provider, bypasses approval, or creates fallback authority.

Protocol adapters translate the canonical Harness request into provider-native payloads and normalize provider-native stream chunks back into Harness provider events. The runtime path is always: resolve the requested model ref, validate capabilities and limits, check provider/data-boundary policy, resolve credentials, construct the adapter for the already-selected provider/model, and stream normalized events. Retries may repeat that same provider/model after retryable provider failures; they must not switch providers, switch models, or invent hidden fallback.

When a provider reports context overflow, the session runtime may perform one deterministic local compaction before retrying the same selected provider/model. This is not provider-backed summarization: the receipt records `provider_summarization_used=false`, no hidden fallback, no network call, retained/dropped message ids, retained/dropped group ids, a summary id, and content-policy flags showing that source message bodies and artifact bodies were not included. The inserted retry summary is built from sanitized local message previews and remains linked to `harness.runtime.compaction.started`, `harness.runtime.compaction.completed`, and `harness.runtime.retry_scheduled` evidence.

Discovery is an explicit catalog operation, not background readiness probing. `harness models refresh <provider_id>` may call a provider only for that named provider, with local endpoint validation for local providers and explicit hosted approval for hosted providers. Discovered models are cached as source-labeled overlays and can be cleared without deleting built-in, backend-config, or project-local custom descriptors.

## Model Refs And Aliases

The model ref is the operator-facing selector for a model. The canonical form is `provider_id/model_id`, for example `codex_cli/gpt-5.5`, `local_openai_compatible/qwen3-coder:30b`, or `paid_openai_compatible/gpt-5.3-codex`. The provider id is part of the ref because Harness does not infer a provider from a bare model name; a missing, unknown, disabled, or unsupported provider must fail visibly before execution instead of falling through to another provider.

Harness preserves three related fields in catalog and runtime evidence. `raw_model_ref` is exactly the ref the operator supplied or selected, after trimming whitespace. `canonical_model_ref` is the concrete descriptor that will be validated and executed if policy allows it. `alias_used` is populated only when `raw_model_ref` resolved through an alias. For example, `codex/gpt-5.5` records `raw_model_ref=codex/gpt-5.5`, `canonical_model_ref=codex_cli/gpt-5.5`, and `alias_used=codex/gpt-5.5`; the runtime still validates and executes only the canonical Codex CLI descriptor.

Aliases are convenience labels, not fallback rules. Built-in aliases such as `codex/gpt-5.5`, `local/qwen3-coder`, and `openai/gpt-5.3-codex` resolve to explicit targets in the model alias catalog. Alias resolution never searches for a nearby model, enables a disabled provider, grants hosted approval, reads credentials, or switches provider/model during retry. If an alias target is missing, validation fails with `alias_target_unknown`; if the target provider is disabled or blocked, the resolved selection keeps that blocked reason.

Variants select a named option profile on a model descriptor. Prefer `@variant`, such as `codex_cli/gpt-5.5@high` or `local_openai_compatible/qwen3-coder:30b@deterministic`. The parser also accepts `:low`, `:medium`, `:high`, `:xhigh`, `:minimal`, `:fast`, and `:smart` as legacy variant suffixes, but only for those known suffixes. Other colons remain part of the model id, so local/Ollama-style ids like `qwen3-coder:30b` are preserved as model ids rather than being split as variants.

Model refs can be stored as session selections, session defaults, workspace defaults, operator defaults, workbench defaults, favorites, and command arguments. The resolution order is explicit command argument, session override, session default metadata, workspace default ref, operator default preference, then workbench default profile. That order selects a candidate ref only; it is not hidden fallback. The chosen ref still goes through alias resolution, provider/model capability validation, policy approval, credential resolution, and adapter construction before any model execution can start.

## Credential Storage And Redaction

Provider accounts are split into metadata and secret material. Account metadata is stored in the local Harness SQLite store with `account_id`, `provider_id`, description, credential kind, status, active flag, expiry, timestamps, and sanitized metadata. Metadata values whose keys look secret-bearing, such as `secret`, `token`, `password`, `credential`, `api_key`, `apikey`, or `authorization`, are redacted before persistence and again when projected. Account rows are safe to list because they carry state and references, not raw provider secrets.

API-key and OAuth-token credentials are stored separately under the project `.harness/provider_secrets.json` provider secret store, keyed by account id. Writes take an exclusive `.harness/provider_secrets.lock`, write through a temporary file, and set the secret store and lock file to mode `0600`. This is a local file-permission boundary, not encryption; operators must treat the `.harness/` project state as sensitive local state and must not commit, publish, or share provider secret-store files.

Env-backed accounts do not store env values. They store the env var name and derive `configured` or `missing` status from the current process environment. Runtime credential resolution reads the env value only when it is explicitly called with secret material allowed, after model validation and the required provider/data-boundary approvals have passed. Catalog listing, validation, provider status, local-server projections, and the TUI must not read env values.

Credential projections use redacted evidence fields. CLI and local-server provider-account surfaces may show credential kind, credential source, account id, expiry, env var name, status, and header names; they must report `credential_value_included=false` and `credentials_included=false`. TUI dashboard/model-picker projections redact env-var names further, for example as `env:<redacted>`, so terminal UI snapshots do not contain `OPENAI_API_KEY` or similar names. Header env refs are resolved to actual header values only for runtime adapter requests; evidence records `header_names` rather than header values. Runtime events use `redacted_evidence()` for provider credentials so adapter-only secret material does not appear in session validation, provider stream events, TUI state, JSON command output, or logs.

Missing, expired, unsupported, or refresh-required credentials fail before a provider client is constructed or a network call starts. Configuring a credential does not select a model, enable a provider, satisfy hosted/data-boundary approval, or authorize fallback. `harness providers logout <provider_id>` removes provider account rows and attempts to remove the matching account secret from `.harness/provider_secrets.json`; the resulting action output reports whether a credential was removed without printing the removed value.

OAuth support uses the same redaction boundary. Authorization projections can return a manual-code authorization URL and PKCE challenge metadata without opening a browser, calling network, or storing credentials. OAuth callback storage can write access and refresh tokens to the provider secret store, but action output and refresh events keep token values redacted and report only state such as account id, expiry, scopes, write status, and whether network was accessed.

## Provider Connect And Disconnect

Provider connect means recording or activating a local provider account. It does not test the provider, select a model, refresh discovery, grant hosted approval, or start execution. The CLI entry point is `harness providers login <provider_id> --project . --output json`; the local server exposes equivalent bearer-auth routes for UI clients. All connect paths return explicit evidence with `provider_execution_started=false`, `model_execution_started=false`, `network_accessed=false`, `credentials_included=false`, and `no_hidden_fallback=true`.

The TUI provider-connect entry point is the model picker, not a separate provider browser. Open `/models`, `/model`, `ctrl+x m`, or the `/provider` compatibility alias, then select the relevant model/provider row and press `Ctrl+A` to open that provider's account/auth-method panel. Env and API-key methods are interactive TUI prompts: env connect stores only the env var name, and API-key connect masks typed input and writes the key to the local provider secret store without echoing it in the transcript, status line, dialog, JSON evidence, or session events. Local-only methods such as `static_local`, `codex_login`, `aws_env`, and `aws_profile` create provider account metadata without secret entry or provider calls; OAuth methods route to the existing manual-code OAuth authorize/callback flow. Unsupported provider methods are displayed as blocked instead of pretending that a full browser OAuth or provider-specific setup completed. Successful TUI connect records evidence and returns to the model picker filtered to that provider so the operator can choose a model explicitly.

Env connect records an environment binding. Use `harness providers login paid_openai_compatible --credential-kind env --env-var OPENAI_API_KEY --project . --output json`, or `POST /provider/{provider_id}/auth/env` with `env_var`. Harness stores the env var name and marks the account `configured` only when that variable exists in the current process; it does not read or persist the env value during catalog/account listing. If the variable is absent, the account can still be recorded with `status=missing`, and runtime execution will block before network access until the env value exists and all approvals pass.

API-key connect stores a local secret-store credential. Use `harness providers login <provider_id> --credential-kind api_key --api-key <value> --project . --output json`, or `POST /provider/{provider_id}/auth/api-key` with `api_key`. In non-JSON CLI mode, Harness may prompt for the API key with hidden input; in JSON mode the key must be passed explicitly. The output reports account and write metadata only; it does not echo the key and does not call the provider to prove the key works.

OAuth connect is currently a manual-code storage flow. `POST /provider/{provider_id}/oauth/authorize` returns authorization metadata without opening a browser or storing credentials. `POST /provider/{provider_id}/oauth/callback`, or CLI `providers login --credential-kind oauth --access-token ... --refresh-token ...`, stores supplied OAuth tokens in the local provider secret store and records a redacted account. Unsupported OAuth providers fail closed without opening a browser, calling network, or storing credentials.

`harness providers accounts <provider_id>` lists redacted account rows. `harness providers activate-account <provider_id> <account_id>` and `POST /provider/{provider_id}/auth/activate` make one existing account active and deactivate the provider's other accounts; they do not rewrite secrets or validate the credential. `harness providers logout <provider_id>` and `DELETE /provider/{provider_id}/auth` remove all local accounts for that provider and attempt to remove matching secret-store entries. Disconnecting a provider does not remove provider/model descriptors, aliases, favorites, defaults, discovery cache, or approvals; later runtime attempts must resolve credentials again and will block if none are configured.

## Model Picker Behavior

The TUI model picker is a session-scoped metadata editor. Open it with `ctrl+x m`, `/models`, `/models list`, `/model`, or command-palette search; select with Enter in the dialog or `/model <number|search|provider/model>`. The picker is backed by the same model catalog projection as `harness models list` and the local-server `/models` route, so it shows catalog descriptors, cached discovered overlays, provider account state, favorites, recents, and the active session model without probing providers.

Picker rows are de-duplicated by model ref and grouped in this order: current session model, favorites, recents, connected providers, local providers, hosted providers, then disabled or blocked providers. Search matches raw ref, canonical ref, alias, model id, provider id, and provider display name. The dialog virtualizes large catalogs and shows a details panel for the selected row with provider, model id, canonical ref, protocol, context window, max output, reasoning support, variants, modalities, tool support, credential status, boundary, source, cost, blocked reasons, and an inspect command.

Selecting a model validates the requested ref and persists only session model metadata plus a `session.model_validation` event. Successful selection updates the active session `raw_model_ref`, provider id, model id, variant, and model-selection preference metadata. Blocked selection records validation evidence and suggestions when applicable but leaves the session model unchanged. Both paths keep `provider_execution_started=false`, `model_execution_started=false`, `network_accessed=false`, `hidden_provider_fallback=false`, `hidden_model_fallback=false`, `permission_granting=false`, and `no_hidden_fallback=true`.

The picker can surface model-management actions without turning display into execution. `F5` toggles favorite, `F6` sets the default model preference, and `F7` renders inspect/validation evidence. `F8` maps to explicit provider refresh, `Ctrl+A` opens provider account/auth-methods for the selected provider when that provider has supported credential actions, and `F10` maps to provider disconnect when the provider is connected or credentialed. Pressing Enter on a credential-blocked model also opens the same auth-method chooser instead of creating a second provider-picker surface. Those provider actions route through the same explicit provider commands described above; they are not performed merely by opening, filtering, or moving through model rows.

Disabled, hosted, paid, missing-credential, and otherwise blocked models remain visible with blocked reasons so the operator can understand the catalog state. Visibility is not executability. A displayed hosted model still needs provider enablement, hosted/data-boundary approval, and credential resolution before runtime can construct a provider client.

## Discovery And Hosted Approval

Model discovery is explicit and provider-scoped. The command is `harness models refresh <provider_id> --project . --output json`; it refreshes only the named provider and never runs a model. The command defaults to `--metadata-only`, and `--no-metadata-only` fails closed because discovery is not allowed to become provider execution. `harness models refresh <provider_id> --clear-cache` removes only cached discovered rows for that provider and performs no network access.

Local discovery validates the provider endpoint before any call. OpenAI-compatible local providers call only the validated `/models` endpoint, with loopback required unless a LAN endpoint is explicitly approved in provider configuration. The resulting discovered rows are persisted as a `source=discovered` overlay and merged into later catalog reads, validation, local-server projections, and the TUI picker without another provider call.

Hosted or otherwise non-local discovery requires the operator to pass `--approve-hosted` on that refresh command. Without it, discovery fails with `hosted_discovery_approval_required` before network access. This flag is a one-command discovery approval only: it allows the model-list request for the named provider, records hosted refresh evidence, and does not create a persistent runtime approval, enable the provider, satisfy paid-provider approval, satisfy external-router/data-boundary approval, or make any model executable.

Credential-backed discovery is opt-in with `--with-credentials`. When requested, Harness resolves credentials only for providers whose discovery adapter uses credentials, and it still applies the hosted/local discovery gate first. Missing credentials fail before the model-list network call. Successful credential-backed discovery may report `credentials_included=true` as evidence that an authenticated model-list request was made, but token, API-key, OAuth, AWS, env, and header values remain out of command output, cache metadata, TUI state, and logs.

Discovery cache entries store metadata, not raw provider bodies. Cache rows include the discovered timestamp, endpoint or discovery endpoint, network and credential evidence, approval evidence, discovered model ids, a 24-hour TTL, freshness/staleness status, and a SHA-256 hash of the raw provider response. Built-in, backend-config, static-catalog, custom-config, and discovered rows remain separate; clearing discovered cache does not remove configured provider/model descriptors, aliases, favorites, defaults, accounts, credentials, or approvals.

Runtime approvals are checked later and separately. Session execution for `hosted_provider` models requires a valid `hosted_provider` approval for the selected provider, task type, adapter, and optional workbench/objective scope. Paid API providers additionally require `paid_provider` approval. Providers with other non-local data boundaries, such as `external_router`, require a matching `data_boundary:<boundary>` approval. These runtime approval checks happen before credential resolution and before provider client construction; failures are recorded as policy-blocked states rather than falling back to another provider.

## Default Model Resolution

Default model resolution is deterministic candidate selection, not provider fallback. When a session prompt does not include an explicit model ref, Harness resolves exactly one candidate in this order: command argument, active session `raw_model_ref`, session default metadata (`session_default_model_ref`, `default_model_ref`, or `default_raw_model_ref`), workspace `chat.default_model_ref` or slash-shaped `chat.default_model_profile`, operator default model preference, then the active workbench default model profile resolved to a concrete catalog ref. The first non-empty candidate is the only ref considered.

Resolution is audited before model validation. The runtime records `session.model_resolution` with `source`, `raw_model_ref`, `canonical_model_ref` when available, provider id, model id, variant, alias used, blocked reasons, and `no_hidden_fallback=true`. If no candidate exists, resolution fails with `model_ref_missing`, no `session.model_validation` event is emitted, no adapter is constructed, and no provider is called. If the chosen default points at an unknown, disabled, missing-credential, unsupported, hosted-unapproved, or otherwise blocked model, Harness records that blocked reason and stops; it does not continue down the default list to find an executable alternative.

Operator defaults are explicit preference records. `harness models default <provider/model> --project . --output json`, TUI `F6`, and `/model default <provider/model>` validate the supplied ref and then update the local preference store. That preference can later be used as the `operator_preference` candidate, but it still has to validate again at runtime against current provider/model state, approvals, credentials, and capability requests. A default preference can become blocked if the provider is later disabled, credentials are removed, or policy requirements change.

This is why default resolution is not hidden fallback: the source of the selected ref is visible, the exact ref is persisted in evidence, failed defaults fail closed, and retries repeat the same resolved provider/model instead of switching to another candidate. `hidden_provider_fallback=false` and `hidden_model_fallback=false` must remain true evidence across catalog, picker, validation, runtime, retry, and failure paths.

## v1.8 Operator Workflow

The v1.8 release is a local-first supervised agent app for declarative agents, manual durable tasks, inspectable evidence, registered adapter dispatch, explicit local memory, capability discovery, orchestration progress, and a unified operator app. The end-to-end read-only path is:

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
harness init --project .
harness agents import agents/my_agent --project . --output json
harness agents inspect my_agent --project . --output json
harness agents preview-imported my_agent --project . --output json
harness tasks add --title "Read-only summary" \
  --agent my_agent \
  --workbench quant \
  --execution-adapter read_only_summary \
  --task-type read_only_repo_summary \
  --project . \
  --output json
harness daemon run-once --project . --output json
harness daemon inspect-lease task_lease_abc123def456 --project . --output json
harness daemon execute task_lease_abc123def456 --project . --output json
```

`daemon run-once` remains select-and-lease only. `daemon execute` dispatches only already-leased tasks to registered adapters. No adapter means no execution, unknown adapter means fail closed, and adapter descriptors are documentation and validation metadata, not permission grants. No-run registered-adapter rejections release the active lease and mark the linked attempt/task `failed` or `waiting_approval`; `duplicate_run` and `lease_owner_mismatch` decisions stay non-mutating because existing run evidence or another owner may be authoritative. Imported agents are metadata references; they do not grant new tools or execution permissions.

Session-tool delegated child tasks use the registered `session_child_task/session_delegate` record-only contract. That adapter validates task metadata and parent/child session linkage, but daemon dispatch is denied; actual work still flows through explicit session-tool execution and approval evidence. Created delegated tasks also carry a compact `harness.agent_handoff_envelope/v1` reference in task metadata: envelope id, payload SHA-256, trace id, `traceparent`, and the embedded `harness.agent_contract/v1` id/hash for the delegated agent identity. Inspect the full passive envelope with `harness handoffs inspect-task <task_id> --project . --output json`; it reports delegate budget, idempotency, source run, parent/child sessions, allowed tools, agent contract, trace context, authority flags, and validation errors without executing adapters, starting processes, calling networks, reading credentials, reading artifact bodies, reading project agent source bodies, adding model context, mutating files, or granting permissions.

The execution dispatcher does not authorize automatic task generation, autonomous workflows, Docker-from-queue, generic shell, hosted fallback, paid fallback, OpenAI API usage, MCP/A2A, browser/email/calendar tools, broker actions, live trading, order placement, external messaging, application submission, or unmanaged active repo write automation.

## Operator Cockpit

`harness` is the primary operator application. With no subcommand it starts one Textual terminal app that combines the passive dashboard and the chat/orchestrator prompt:

```bash
harness
harness --project .
harness --project . --output json
harness --project . --plain
harness --project . --plain --codex-like
```

The Textual app is now backed by the shared app-service contract. In the default embedded mode, the TUI calls the in-process service directly for dashboard, session pane, session metadata mutations, prompt submission, runtime status, permissions, questions, settings, messages, and events. This keeps the terminal UI, local server, and runtime manager on the same persisted session state instead of maintaining a separate TUI-only chat path.

For diagnostics or remote local-client testing, the same TUI can attach to an already-running bearer-auth local server. Start the server in one terminal and launch the attached TUI in another:

```bash
export HARNESS_SERVER_TOKEN="$(python - <<'PY'
import secrets
print("harness_" + secrets.token_urlsafe(24))
PY
)"
harness serve --project . --host 127.0.0.1 --port 8765 --token "$HARNESS_SERVER_TOKEN"
harness --project . --server-url http://127.0.0.1:8765 --token "$HARNESS_SERVER_TOKEN"
```

Attached mode is explicit. Missing or rejected tokens fail before rendering session data. Embedded mode remains the default for normal local use. Both modes preserve the same safety contract: opening the app is read-only on uninitialized projects, permission cards require explicit allow/deny/cancel actions, Enter alone does not grant approval, and transcripts render from persisted messages/events without exposing backend tokens, secret-looking text, secret paths, or artifact bodies.

New execution behavior should still go through the headless core loop first:

```bash
harness core run "smoke test core loop" --mode dry_run --project . --output json
harness core inspect-evidence --run <run_id> --project . --output json
harness core inspect-evidence --task <task_id> --project . --output json
harness core inspect-run <run_id> --project . --output json
harness core inspect-events <run_id> --project . --output json
harness core inspect-task <task_id> --project . --output json
```

The core loop is intentionally narrow: goal, session/objective/task records, lease, registered adapter dispatch, run evidence/events/artifacts/manifest when execution is allowed, and a stable final summary. `repo_planning` and `codex_isolated_edit` remain blocked without valid hosted-boundary approval; the core loop does not add hosted fallback, paid fallback, ambient shell, network, Docker, MCP, plugins, browser, email, calendar, or active repository mutation.

`harness core inspect-evidence` is the canonical bundled read-only projection for future surfaces. It accepts exactly one of `--run` or `--task` and returns the run, task, blocked-state, event, artifact-metadata, manifest, error, and next-command projections available for that id without assembling them in a UI layer.

`harness show <run_id> --output json` now wraps that same bundled run evidence under `core_evidence` in a `harness.show/v2` compatibility response. `harness tasks inspect <task_id> --output json` returns `harness.tasks_inspect/v2` with `core_evidence` when a completed run or blocked no-run state exists, plus a sanitized `harness.task_replay_receipts_projection/v1` summary for any attempt or retry `harness.task_replay_receipt/v1` evidence linked to the task. `harness events <run_id> --output json` returns `harness.events_inspect/v2` with `core_events` from the canonical run-event projection. The text forms of `harness show`, `harness tasks inspect`, and `harness events`, plus run/task listing, task mutation, JSONL event output, event following, and artifact commands, are intentionally unchanged.

`harness core inspect-run` is the first canonical read-only projection over persisted run evidence. It reports ids, task/lease/adapter status, manifest path, artifact metadata, policy hash, errors, blocked reasons, and next commands without reading artifact bodies or expanding any UI surface.

`harness core inspect-events` renders sanitized persisted run events through the same projection layer. It is read-only and does not initialize project state, execute work, or read artifact bodies.

`harness core inspect-task` uses the same projection layer for task-first evidence, including no-run blocked states from missing hosted approval. It is read-only: it does not initialize project state, acquire leases, dispatch adapters, call providers, run shell/Docker/network work, or read artifact bodies.

The simplest foreground JSON agent aliases consume that same headless path: `harness "goal" --agent plan --output json` maps to `repo_planning`, and `harness "goal" --agent build --output json` maps to `codex_isolated_edit`. This routing is intentionally narrow during stabilization; text output, direct active-workspace mode, session continuation/forking, model overrides, file attachments, and mention-only native aliases remain on their existing compatibility paths until those surfaces are migrated deliberately.

The default app shows project state, dashboard sections, search and palette context, safety reminders, recent runs, active leases, registered adapters, and a chat prompt in one surface. The dashboard side is passive and read-only. The prompt side is the explicit action layer: it can inspect state, select an orchestrator, auto-transition chat-routed isolated edits under the default `supervised-codex` policy, draft visible objective/task graphs when review is required, acquire daemon run-once leases, and dispatch already-leased tasks only through registered adapters.

`harness --output json` returns the same read-only `harness.chat/v1` context without launching the terminal UI. `harness --plain` starts the line-oriented chat fallback for tests and terminals where Textual is unsuitable. `--codex-like` starts the session in foreground action mode, where one explicit confirmation can create Harness records and drive registered-adapter dispatch for the approved task or graph. Textual is a normal install dependency for the app experience; it is no longer an optional operator path.

The invariant is: user asks, chat proposes an explicit harness action, harness records or leases or dispatches, and evidence returns to chat. Default model-visible session tools such as `read`, `glob`, `grep`, `git-diff`, `cd`, and `pwd` route through the session-tool registry and persist events/artifacts before output is rendered. The full catalog remains inspectable through `/tools`, but provider-native schemas use `policy.exposure.model_visible`, so shell, write, network, extension, task-spawning, and internal invalid-call recovery tools are withheld from default model-visible exposure until an explicit governed path requests them or Harness normalizes to them internally. `session_read_tools` tasks without explicit `allowed_tools` advertise only the default read inspection set; broader tools such as `shell` require explicit task metadata and still pause for exact permission. Default model-visible object schemas reject unspecified top-level arguments. `skill-load` and `mcp-resource` are progressive-disclosure extension-file paths: metadata is visible without content, body/resource reads require exact session permission, and configured paths with symlink components are denied before content is loaded. `cwd` is durable session-local state inside the active project root; `/cd` changes that project-relative cwd without starting a process, while `/project` and `/workspace` switch the active root explicitly. Chat does not call providers directly, preflight backends for context display, run Docker outside Harness adapters, persist history, create hidden background work, or mutate active repository files from model text. Shell is not ambient shell access: it is a permissioned, auditable, bounded session tool with exact cwd/command/timeout/shell/environment/network permission targets. Codex hosted-boundary approval and apply-back approval remain separate; apply-back is denied by default unless the existing inspected-diff approval path approves it.

On first run, the app can initialize project state in place with `/init` or a natural request such as “initialize this project.” Until initialization, deterministic local chat guidance can still explain available Harness actions, while task, lease, run, and dispatch actions offer the in-app initialization path instead of requiring the operator to leave the app. Chat does not call Codex directly; model-backed work is available only through registered adapter dispatch after explicit task and lease state exists.

For chat-first orchestration, natural-language requests draft a visible action contract before creating records. The first-class templates are “summarize this repo” (`read_only_summary/read_only_repo_summary`), “plan how to add X” (`repo_planning/repo_planning`), and “fix the failing test with Codex” (read-only planning, one dependent isolated Codex edit, local sandbox-test evidence, implementation review, security review, and final synthesis). Reviewed workflow templates declare per-task `harness.workflow_agent_selection/v1` requirements. Drafts use those requirements with the local `harness.delegate_allocation/v1` bid contract and include a compact `delegate_allocation` receipt in every task metadata payload. That receipt records selected agent, selected bid, requirements, matches, bid terms, and passive safety flags; it does not grant agent, tool, provider, budget, or permission authority. File-write and folder-write requests use the same isolated-edit contract path instead of self-mutating the active workspace from chat text; apply-back remains a later inspected-diff decision. Each draft renders the interpreted intent, proposed action, equivalent CLI commands, supervisor checkpoints, safety boundary, required hosted-boundary approval, and confirmation prompt. Pending task drafts, orchestration drafts, registered-adapter dispatch confirmations, hosted-approval prompts, and action contracts are persisted as metadata on the active Harness session so an interrupted operator process can resume the same pending action by session id. The dashboard recent-session rows, session pane, active-session preview, right-pane attention rows, `/sessions`, `/api/session`, `/sessions/status`, `/sessions/{id}`, and `/sessions/{id}/status` expose a compact recoverable pending-action projection with `/confirm` and `/decline` as the next commands, plus passive `harness.session_active_run_reference/v1` health for persisted active-run pointers. Malformed or stale pending-action metadata is shown as an invalid/stale audit state, not as a confirmable action; operators can inspect it with `harness sessions pending-action <session_id> --output json` or `GET /sessions/{session_id}/pending-action`, then clear only the proposal metadata with `harness sessions clear-pending-action <session_id>` or `DELETE /sessions/{session_id}/pending-action`. Stale session `active_run_id` references are shown as stale active-run reference health with the explicit `harness doctor --repair` command; they are not repaired from passive dashboard or server views. This metadata is a proposal/reference record only; passive projections, pending-action cleanup, and stale active-run projection do not execute, lease, dispatch, call providers, grant authority, mutate objectives, mutate tasks, mutate leases, mutate runs, mutate approvals, mutate artifacts, mutate messages, or mutate repository files. Confirmed reviewed workflow templates create append-only objective checkpoint evidence and immediately approve the required checkpoint from that explicit confirmation, so the objective checkpoint gate is durable and inspectable before any lease is acquired. Confirmed graph creation is replay-safe: retries of the same pending chat draft or normalized action contract reuse the existing objective, task ids, and checkpoint evidence instead of creating duplicates. The checkpoint approval proves the graph was confirmed; it does not grant hosted-provider authority, shell access, Docker access, network access, active-repo mutation, or apply-back. The default chat orchestrator is `coding_orchestrator`; operators can switch to another built-in orchestrator with `/use quant_orchestrator` or `/use personal_orchestrator`. In normal mode, the chat keeps draft-before-confirm behavior. In codex-like mode, after one explicit foreground run approval, chat creates or reuses the objective and tasks, records or reuses the approved checkpoint evidence for reviewed templates, then repeatedly uses the existing daemon run-once lease path and registered adapters until the graph is terminal, blocked, rejected, or stopped. This is a bounded foreground loop, not a hidden daemon or generic executor.

After dispatch, chat renders a compact evidence block: task status, adapter, lease, run id, artifact paths, and next commands such as `harness show <run_id> --output json`, `harness artifacts list <run_id>`, and `harness progress --objective <objective_id> --output json`. For Codex isolated edits, the result also states that apply-back was not approved by the foreground run; active repository mutation still requires the separate inspected-diff apply-back path.

Within the unified app, slash commands such as `/help`, `/init`, `/tools`, `/pwd`, `/cd`, `/project`, `/workspace`, `/mode`, `/home`, `/dashboard`, `/orchestrators`, `/use`, `/agents`, `/tasks`, `/runs`, `/leases`, `/capabilities`, `/adapters`, `/memory`, `/remember`, `/forget`, `/progress`, `/lease`, `/execute`, `/plan`, `/plan-mode`, `/browse`, `/research`, `/run`, `/stop`, `/reset`, and `/quit` operate through the same control-plane APIs as the non-interactive commands. `/plan-mode on [reason]` enters session-local planning mode through the `plan-enter` session tool, and `/plan-mode off <summary>` exits with persisted planning evidence through `plan-exit`; it does not start providers, shell, Docker, web requests, filesystem mutation, or permission grants. `/browse <url>` and `/research <query>` route through the governed `web-fetch` and deep `web-search` session tools. They validate `.harness/config.yaml` `web_tools` policy first, then pause for exact external-network approval before any network request, and any approved result is persisted as session/run/artifact evidence before display. Natural aliases such as “show capabilities”, “what can Harness do here?”, “show memory”, “show progress”, “enter plan mode”, “deep research <query>”, and “browse <url>” route to deterministic local renderers or the same governed session-tool paths. The UI keeps dashboard context next to the transcript in stable sections for project overview, queue and daemon state, agents and specs, planning and research, capabilities, memory, progress, runtime evidence, command palette, and safety. Operators can use `ctrl+p` or `F2` to toggle palette-only search focus. When the prompt is not focused, `c` collapses or expands the current section and `shift+c` expands all sections. These preferences are in-memory for the running app session only.

After backend/TUI wiring changes, run the non-interactive smoke commands before manual TUI checks:

```bash
python -m pytest tests/test_core_service.py tests/test_local_server.py tests/test_session_runtime.py
python -m pytest tests/test_orchestration_cockpit.py tests/test_operator_chat_path.py
tmp_project="${TMPDIR:-/tmp}/harness-tui-smoke"
rm -rf "$tmp_project"
mkdir -p "$tmp_project"
harness --project "$tmp_project" --output json
harness init --project "$tmp_project"
harness --project "$tmp_project" --output json
harness serve --project "$tmp_project" --openapi --output json
```

Manual TUI smoke should cover both embedded and attached modes. In embedded mode, launch `harness --project "$tmp_project"` and verify stable session rail selection, `n` session creation, rename/archive/restore/fork actions, prompt submission that does not block the UI, live runtime status, final persisted assistant messages after restart, explicit permission card actions, model selection validation without provider execution, degraded polling if event subscription fails, and no backend token or artifact body leakage. In attached mode, start `harness serve` with an explicit bearer token, launch `harness --project "$tmp_project" --server-url http://127.0.0.1:8765 --token "$HARNESS_SERVER_TOKEN"`, and verify session listing plus live event updates from the server.

Model catalog surfaces are operator metadata, not fallback machinery. `harness models list`, `harness models providers`, `harness providers list`, `harness providers status`, `harness models inspect <provider/model>`, `harness models validate <provider/model>`, and `harness models protocols` show the configured provider/model registry, canonical refs, aliases, protocol adapters, context and max-output limits, reasoning support, modalities, tool support, cost metadata, provider enablement, and blocked reasons without calling providers or reading credentials. `harness models refresh <provider>` is the only model catalog command that may access a provider endpoint; local refresh first validates the local-only endpoint, while hosted refresh requires explicit `--approve-hosted` approval and otherwise fails before network access. The TUI model picker renders the same metadata: disabled or hosted/paid models remain visible with blocked reasons, aliases show their canonical refs, selected rows expose details and an inspect command, and filtering/moving the picker remains metadata-only with no provider call, credential read, execution, or hidden fallback.

Provider account actions are explicit and redacted. `harness providers login <provider_id>` records an account metadata row for supported credential kinds and can be scoped with `--credential-kind env|static_local|api_key|codex_login|oauth|aws_env|aws_profile`, `--env-var NAME`, `--api-key VALUE`, `--access-token VALUE`, `--refresh-token VALUE`, `--expires-at TIMESTAMP`, `--scopes TEXT`, and `--description TEXT`. Env-backed accounts store the env var name and report configured status only when that variable exists in the current process; raw env values are not printed or persisted by the account action. API-key and OAuth login write secret values only to the local provider secret store. `harness providers accounts <provider_id>` lists redacted account records, `harness providers activate-account <provider_id> <account_id>` switches the active account, and `harness providers logout <provider_id>` removes the provider account records and matching secret-store entries. These commands do not call provider endpoints, start model execution, include credential values, grant authority, or create hidden fallback. The TUI model picker routes env/API-key/OAuth/local-account/disconnect affordances through these explicit provider actions, persists evidence, masks API-key entry, and returns to the model picker after a successful connect; unsupported methods remain blocked instead of pretending a full browser OAuth flow completed.

Operators can add project-local OpenAI-compatible providers without changing code by creating `.harness/models.yaml` and running `harness models config validate --project . --output json`. The file is reloaded by normal catalog reads through `load_config`, so `harness models list`, `harness models providers`, `harness models inspect`, and the TUI picker see valid custom providers immediately. Custom config is still metadata and policy, not authority: local providers must use loopback URLs unless a LAN endpoint is explicitly approved in the file, hosted or router providers remain disabled unless `approved: true`, and credentials/headers must be env or account references rather than raw secret values. Invalid files fail closed with stable error codes such as `provider_local_url_not_loopback_or_approved_lan:<provider>` or `credential_value_not_allowed:<provider>:<field>`.

Explicit discovery is durable but remains an overlay. A successful `harness models refresh <provider_id>` stores discovered model rows separately from built-in, backend-config, and custom-config rows. Later catalog reads merge those cached discovered rows into `harness models list`, `harness models inspect`, validation, local-server projections, and the TUI picker without calling providers again. Each discovered row records `source=discovered`, discovery timestamp, endpoint, network and credential evidence, approval evidence for hosted refreshes, discovered model ids, and a hash of the raw provider response rather than the raw response body. `harness models refresh <provider_id> --clear-cache` removes only that provider's discovered overlay and does not touch built-in or custom configuration.

Provider adapters use a canonical internal message/content shape before rendering protocol-specific payloads. Canonical parts include text, reasoning, image input, tool calls, tool results, refusal/error, and provider metadata, with fields for opaque provider ids and signatures. Current `codex_cli`, `openai_chat`, `openai_responses`, `openai_codex_responses`, `anthropic_messages`, `google_generative`, `bedrock_converse`, and legacy chat-model bridges support text request parts and preserve provider metadata; unsupported request parts fail visibly with `UnsupportedCanonicalPartError` and no hidden fallback instead of being dropped. Provider stream events can be mapped back to canonical parts for text deltas, reasoning summaries, tool calls, usage, completion, and provider errors while raw event payloads remain available as evidence. Cross-provider handoff fixtures cover OpenAI chat, OpenAI Responses, Anthropic Messages, Google Generative, and Bedrock Converse serialization without live provider calls, including preservation of provider-native reasoning signatures and tool-call ids.

Model selection metadata is operational. Selection validation blocks unsupported context-window requests, output limits, requested reasoning levels, modalities, and tool requirements before provider execution. Runtime provider usage events normalize provider-specific token shapes into `normalized_usage` with input, output, cache-read, cache-write, and total token counts; provider-reported cost is preserved when present, and estimated cost is added when model metadata includes token rates. Session/runtime evidence stores requested versus resolved reasoning and the normalized usage/cost payloads without credential values.

The `openai_chat` protocol adapter streams OpenAI-compatible `/chat/completions` responses with `stream=true` by default. SSE chunks are normalized into provider events for text deltas, tool-call deltas, token usage, finish reasons, and provider errors. Request options such as `temperature`, `max_tokens`, and `timeout_seconds` come from resolved provider/model options; unsupported canonical request parts still fail visibly before provider execution. Non-streaming completion remains available only through an explicit `stream: false` backend setting and is reported as a non-streaming fallback in backend events.

The `openai_responses` and `openai_codex_responses` protocol adapters stream `/responses` with `stream=true`. They serialize canonical text messages into Responses `input`, preserve the provider `response_id`, and normalize output text, refusal deltas, reasoning-summary/reasoning deltas, function-call argument deltas, completed tool calls, token usage, completion, and provider failures into Harness provider events. The built-in paid Codex API model is cataloged under `openai_codex_responses` but remains disabled until the provider is explicitly enabled and credentials are configured; metadata listing and validation still do not call OpenAI or read secret values.

The `anthropic_messages` protocol adapter streams Anthropic `/messages` with `stream=true`. It serializes canonical user/assistant text, assistant reasoning, assistant tool-use blocks, and user-visible tool-result blocks into Anthropic message content; unsupported cross-provider parts fail before provider execution instead of being dropped. Stream chunks normalize message ids, text deltas, thinking deltas, tool-use start/delta/completion, usage, stop reasons, and provider errors into Harness provider events. Built-in Anthropic metadata is visible as a disabled hosted provider using `ANTHROPIC_API_KEY`; it remains non-executable until the provider is explicitly enabled and credentials are configured.

The `google_generative` protocol adapter streams Gemini-compatible `:streamGenerateContent?alt=sse` responses. It serializes canonical system text, user text/images, assistant reasoning with thought signatures, assistant function calls, and tool/function results into Google `contents`; unsupported parts fail before provider execution. Stream chunks normalize usage metadata, thought text with `thoughtSignature`, model text, function calls, finish reasons, and provider errors into Harness provider events. Built-in Google Generative AI metadata is visible as a disabled hosted provider using `GOOGLE_API_KEY`; it remains non-executable until explicitly enabled and configured.

The `bedrock_converse` protocol adapter normalizes Bedrock Converse stream-shaped events. It serializes canonical system text, user text/images, assistant tool-use blocks, and tool-result blocks into Converse `messages`, and it normalizes text deltas, tool-use start/delta/completion, usage/metrics, stop reasons, and provider errors into Harness provider events. Built-in Bedrock metadata is visible as a disabled hosted provider using AWS profile/env metadata only; AWS secret values are not projected into catalog, validation, session, or protocol evidence.

External protocol compatibility is tracked through a passive catalog:

```bash
harness protocols list --project . --output json
harness protocols inspect local_server_openapi --project . --output json
harness protocols inspect mcp_tool --project . --output json
harness protocols inspect a2a_remote_agent --project . --output json
```

The catalog JSON wrapper is `harness.external_protocol_catalog/v1`; single-row inspect output is `harness.external_protocol_descriptor/v1`. The catalog makes protocol adoption explicit without widening authority: model-provider protocols and local session tools are implemented through existing provider/tool gates, local OpenAPI is metadata-only, cached MCP resources remain cached-resource-only with explicit permission, and MCP tool execution, external OpenAPI import, A2A remote agents, and gRPC remote tools remain fail-closed. Remote and extension descriptors declare the telemetry contracts they must satisfy before enablement, including W3C trace context propagation, OpenTelemetry GenAI agent/tool attributes, and MCP client span semantics. These commands do not initialize projects, start servers or MCP processes, call networks, execute tools or agents, read credentials, mutate files, grant permissions, or include protocol/source bodies in model context.

Schema compatibility is tracked through a separate passive registry:

```bash
harness schemas list --project . --output json
harness schemas inspect agent_handoff_envelope --project . --output json
harness schemas inspect objective_evidence_chain --project . --output json
```

The registry JSON wrapper is `harness.schema_contract_catalog/v1`; single-row inspect output is `harness.schema_contract_descriptor/v1`. It registers critical orchestration payloads such as `harness.agent_contract/v1`, `harness.agent_handoff_envelope/v1`, `harness.delegate_budget/v1`, `harness.task_replay_receipt/v1`, `harness.external_protocol_catalog/v1`, readiness, efficiency, synthesis, orchestration replay drift audits, scenario conformance catalogs, reviewed workflow templates, workflow agent-selection requirements, workflow coordination contracts, objective batch plans, objective evidence, checkpoint evidence, trace export, sandbox profile contracts, session tool policy, and local OpenAPI contracts. Each row records owner, producer, consumer, validation surface, compatibility policy, upgrade notes, reference patterns, and non-authority flags so rolling-upgrade and migration decisions do not depend on scattered constants. These commands do not initialize projects, read artifact bodies, import reference code, start processes, call providers or networks, execute tools or agents, mutate files, add model context, read credentials, or grant permissions.

`harness home` remains a read-only snapshot command for scripts and diagnostics:

```bash
harness home --project .
harness home --project . --output json
```

The dashboard reports initialization state, imported-agent count, objective and task counts, active leases, active daemons, recent runs, and local-first safety reminders. On an uninitialized project it recommends `harness init`, but does not create `.harness/` or any runtime state.

`harness home` is read-only. It does not initialize projects, import agents, create tasks, create runs, create artifacts, acquire leases, mutate daemon state, execute adapters, preflight backends, inspect backend settings, run Docker, invoke shell tools, call providers, or expose secrets.

To replace the homepage art with an explicit local image, regenerate the static render data:

```bash
harness tui-home set-image ~/Pictures/home.png --width 80
harness tui-home set-image ~/Pictures/home.png --width 80 --output json
```

This command imports only the provided image path, stores a local source copy in `assets/tui/home_source.png`, and regenerates `src/harness/tui_assets/pixel_art.py`. It does not initialize projects, mutate `.harness/`, create tasks, create runs, acquire leases, start daemon work, execute adapters, preflight backends, run Docker, invoke shell tools, call providers, or expose image contents in command output.

The CLI/TUI, registered dispatcher, Codex isolated adapter, repo planning adapter, capability catalog, explicit local memory notes, orchestration progress, and unified app stabilization are packaged together as release `1.8.0`. The unified app keeps read-only dashboard refinements and routes prompt actions through the real chat/orchestration engine; it does not broaden execution permissions beyond registered, approved adapters.

## v1.8 Local App Surfaces

Capability catalog commands expose the registered adapter set as Harness-native local capabilities:

```bash
harness capabilities list --project . --output json
harness capabilities inspect dry_run --project . --output json
```

The JSON wrapper is `harness.capability_catalog/v1`. Capability rows include supported task types, required approvals, sandbox/readiness notes, serialized `harness.delegate_budget/v1` limits, runtime control availability, and equivalent commands. They are read-only display and dispatch metadata; they do not preflight Codex, local model endpoints, Docker, shell, network, providers, or execute adapters. Shared registered-task validation checks the selected descriptor's delegate budget against its sandbox profile before durable task creation, and adapter dispatch repeats the same contract before execution, so a code drift that makes the descriptor budget invalid or more permissive than the profile fails closed with `reason_code=delegate_budget_mismatch`. Task metadata may narrow runtime limits, but numeric timeout/CPU/memory/model/tool/cost/fan-out metadata that exceeds the descriptor budget is rejected before task creation or adapter dispatch.

Explicit memory commands manage local operator notes:

```bash
harness memory save-note --scope project --summary "Remember this local preference" --project . --output json
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
```

The JSON wrappers are `harness.memory_record/v1` and `harness.memory_records/v1`. Memory records are scoped, local-only, redacted before persistence when secret-looking content appears, and never grant tools, approvals, backend access, hosted-boundary permission, apply-back permission, or execution authority. Derived memory can be captured from `artifact_summary`, `objective_state`, `run_review`, or `failed_attempt_summary` sources and must retain source ids, source artifact ids where applicable, redaction state, hashes, and non-authoritative lineage. `/reset` clears session-local chat references only; it does not delete explicit memory records.

Progress inspection exposes objective/task/lease/run state without doing work:

```bash
harness progress --objective obj_abc123 --project . --output json
```

The JSON wrapper is `harness.orchestration_progress/v1`. It reports objective mode, task rows, active lease/run ids, blocked reasons, checkpoint gate status, and deterministic next commands. Chat `/progress [objective_id]` and the TUI right-panel Progress section render the same read-only payload. Progress inspection does not create tasks, acquire leases, create runs, dispatch adapters, call providers, touch Docker, or mutate active repository files.

Supervisor checkpoints are durable objective gates:

```bash
harness objectives add --title "Draft objective" --draft --project . --output json
harness objectives start obj_abc123 --reason "Ready to dispatch" --project . --output json
harness objectives suspend obj_abc123 --reason "Waiting for supervisor input" --project . --output json
harness objectives resume obj_abc123 --reason "Supervisor input received" --project . --output json
harness objectives timeout obj_abc123 --reason "Deadline exceeded" --project . --output json
harness objectives retry obj_abc123 --reason "Retry retryable failed work" --project . --output json
harness objectives complete obj_abc123 --reason "Accepted final evidence" --project . --output json
harness objectives cancel obj_abc123 --reason "Superseded by obj_xyz789" --project . --output json
harness objectives checkpoints create obj_abc123 --label "Supervisor review" --reason "Review before dispatch" --project . --output json
harness objectives checkpoints gate obj_abc123 --project . --output json
harness objectives checkpoints verify obj_abc123 --project . --output json
harness objectives checkpoints approve obj_abc123 ockpt_abc123 --approval-id approval_abc123 --project . --output json
```

The lifecycle JSON wrapper is `harness.objective_lifecycle/v1`, and the retry wrapper is `harness.objective_retry/v1`. `objectives add --draft` creates a non-dispatchable `created` objective, and `objectives start` is the explicit `created -> active` lifecycle mutation. `objectives start`, `objectives suspend`, `objectives resume`, `objectives timeout`, `objectives complete`, and `objectives cancel` validate allowed status transitions, persist a redacted lifecycle event in objective metadata, and report `operator_authority` flags showing that the command did not execute adapters, call providers, call the network, mutate repository files, grant permissions, or create future authority. Created objectives are blocked until started: progress points at `objectives start` and does not advertise `daemon run-once`, while `objectives run` and `daemon run-autonomous` stop with `objective_inactive` before attempts, leases, runs, backend preflight, or dispatch. Suspended objectives are blocked but resumable; progress points at `objectives resume` and does not advertise dispatch. Required objective checkpoints serialize human approval waits as objective status `waiting_approval`; progress points at checkpoint gate/list commands and approving the last required checkpoint resumes the objective to `active`. `objectives retry` moves an active or timed-out objective through `retrying`, requeues only failed tasks whose registered adapter replay policy permits retry, and returns the objective to `active` without creating attempts, leases, runs, backend preflight, or dispatch. Timed-out objectives are terminal for automatic dispatch unless explicitly retried; cancelled and completed objectives remain terminal inspection states.

The checkpoint JSON wrappers are `harness.objective_checkpoint/v1`, `harness.objective_checkpoints/v1`, `harness.objective_checkpoint_gate/v1`, and `harness.objective_checkpoint_evidence_verification/v1`. Creating a required checkpoint moves an active objective to `waiting_approval`; required checkpoints block `harness objectives run` and `harness daemon run-autonomous` before lease acquisition until approved; rejected checkpoints remain blocking. Approving the final required checkpoint moves a waiting objective back to `active`. Checkpoint verification is read-only and validates checkpoint event parsing, event envelope fields, event id/index sequence, hash-chain links, timezone-aware timestamp order, objective scope, and create/resolve lifecycle records. Corrupt checkpoint evidence makes the checkpoint gate block, makes orchestration readiness fail, and makes checkpoint create/approve/reject refuse to append to the untrusted chain. Checkpoints are append-only objective supervision evidence and do not read artifact bodies, add context to the model, execute adapters, call the network, mutate active repository files, or grant permission by themselves.

When objective JSONL evidence exists, progress also includes a read-only `objective_evidence` summary with verification status, event count, head hash, and key check statuses, plus follow-up commands for `harness objectives verify-evidence` and `harness traces export-objective` so the operator can audit the same persisted run chain without hunting for paths. If readiness reports an objective that has persisted run evidence but no objective JSONL chain, use `harness objectives reconcile-evidence <objective_id> --dry-run --output json` to preview the explicit reconciliation and rerun without `--dry-run` only when the operator wants to write a provenance chain for those existing runs.

Bounded autonomous objective execution is available for existing task graphs:

```bash
harness objectives run obj_abc123 --project . --autonomy safe-local --timeout-seconds 900 --output json
harness objectives verify-evidence obj_abc123 --project . --output json
harness daemon run-autonomous --project . --autonomy daemon-safe --timeout-seconds 900 --output json
```

The JSON wrapper is `harness.autonomous_objective_run/v1`. The runner is graph-driven, not free-form chat-driven: it loads an existing objective, verifies the objective is still active, evaluates the optional wall-clock timeout budget, required supervisor checkpoints, ready or dependency-unblocked tasks for that objective, and the selected autonomy policy and registered adapter metadata before acquiring any new lease. If `--timeout-seconds` has already expired, or expires before a later scheduling loop, the objective is marked `timed_out` with lifecycle metadata and the run stops with `stop_reason=timed_out`, no additional attempts, leases, runs, backend preflight, or dispatch. Approval-required or denied candidates stop before task attempts, leases, runs, backend preflight, or adapter dispatch; their objective evidence records an `autonomy_stopped` decision with `lease_id=null`. If pre-lease autonomy passes but the guarded lease selector catches stale approval, runtime-control, adapter-breaker, dependency, or active-lease state, it records `lease_guard_stopped` evidence with the same no-lease/no-dispatch boundary. Active leases owned by another runner become `active_lease` pause reasons instead of being executed. Runtime controls use the same registered-adapter descriptor matcher as direct daemon execution and capability projection, so `adapter`, `task_type`, `backend`, and `hosted_boundary` kill switches stop autonomous scheduling before new lease acquisition or adapter dispatch. It stops on objective success, timeout, inactive objective status, checkpoint block, terminal failure state, blocked state, approval requirement, runtime-control denial, adapter breaker, execution failure, or budget exhaustion. Objective-level JSONL evidence is written under `.harness/autonomy/objectives/`; bounded parallel runs add typed `harness.objective_batch_plan/v1` `batch_planned` records with scheduler policy, sort-key evidence, capacity, selected task/lease pairs, resumed-vs-new selection source, dependency snapshots, schedule profiles, and autonomy decision ids before dispatch, then `batch_completed` records with batch-local dispatch count, cumulative dispatch count, and execution-error count. Batch-plan verification checks selected decision ids against persisted decision records for run scope, task, lease, dispatch tool, adapter, task type, and decision status; recomputes priority, critical-path depth, and downstream counts from durable task state; verifies candidate ordering by policy; verifies fresh selections are the policy prefix after resumed active leases; verifies resumed leases are ordered by acquisition time and lease id; and requires each selected task/lease pair to have exactly one terminal `adapter_dispatched` or `execution_error` event in that batch. Each autonomous adapter dispatch and worker-level execution error also records decision, approval, and outcome evidence under `.harness/autonomy/`; execution-error outcomes are explicit `ok=false` records rather than unlinked diagnostics.

Use `harness objectives verify-evidence` after autonomous runs when you need a read-only audit of the JSONL chain. It returns `harness.objective_evidence_verification/v1` and verifies the common event envelope, event-type payload schemas including checkpoint-blocked stops, lease-guard stops, and linked execution-error outcomes, event lifecycle, event id/index integrity, event hash-chain integrity, event timestamp integrity, objective scope, batch-plan selected leases, batch lifecycle consistency, batch-local and cumulative dispatch counts, execution-error counts, stopped-summary consistency, dispatch task/run/artifact links, and autonomy decision/approval/outcome records without reading artifact bodies or executing adapters. For dispatch events, it checks persisted terminal state too: `ok=true` must point to a completed run status, `ok=false` with a run must point to a failed run status, and the event decision must match the released lease decision metadata. For dispatch and execution-error events, it also checks that the referenced decision, approval, and outcome records agree on dispatch tool identity, decision status, task type, and derived authority payload fields. For `autonomy_stopped` and `lease_guard_stopped` events, it requires the referenced persisted decision and checks any embedded event decision copy against that record. Text output highlights payload schema, event identity, hash-chain, timestamp, event-count, and chain-head status before the per-check table.

Attached clients can inspect the same objective evidence through the bearer-auth local server with `GET /objectives/{objective_id}/evidence` and `GET /objectives/{objective_id}/trace`. These routes return the same verifier and trace schemas with explicit no-execution, no-provider, no-filesystem-mutation, no-network, and no-permission-grant flags.

In this phase, objective autonomy does not ask a model to expand the graph, does not create tasks, does not mint hosted authority beyond predeclared scoped profiles, does not apply back isolated changes, does not call shell or arbitrary tools, and does not mutate the active repo.

Scoped hosted approval profiles can be predeclared for autonomous Codex planning and isolated editing:

```bash
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
```

These approvals are still hosted-boundary approvals only. They can satisfy `supervised-codex` autonomy checks when task type, adapter, workbench, objective, and autonomy scope match the stored profile and the profile is not expired, revoked, or over its run/runtime/context budget. Legacy hosted approvals without `--autonomy-scope supervised-codex` remain valid for manual hosted-boundary flows, but they do not satisfy strict autonomous Codex dispatch. They do not permit apply-back, active repository writes, arbitrary shell, arbitrary network, paid fallback, task type expansion, or approval renewal.

## Governance Authority Layer

Governance is the Harness authority layer for scoped work, context packs, test evidence, protected paths, network quarantine, apply-back, promotion, and merge readiness. It is not a convenience wrapper around Git, CI, or provider execution. Governance commands record or inspect local evidence; they do not grant hosted-boundary approval, execute adapters, call providers, merge branches, push commits, comment on pull requests, or mutate active repository files unless another explicit Harness mutation path has independently approved that action.

Inspect the canonical gate registry and protected apply-back path source:

```bash
harness governance gates --output json
```

The JSON wrapper is `harness.governance.gate_registry/v1`. It names hard gates such as `no_protected_writes`, `allowed_paths_respected`, `segment_context_pack_present`, `test_evidence_fresh`, `applyback_bound_to_segment`, `promotion_not_quarantined`, and `promotion_network_policy_valid`.

Create and inspect a governed task record:

```bash
harness governance tasks create governance-slice \
  --agent repo_inspector \
  --goal "Wire governed change evidence" \
  --base main \
  --project . \
  --output json
harness governance tasks show task_abc123 --project . --output json
harness governance tasks list --project . --output json
```

Governed task records use `harness.governance_task/v1`. They bind a task id to a branch, base SHA, worktree path, agent id, permission profile, sandbox profile, goal, allowed paths, expected artifacts, optional context pack hash, and later test/merge-check evidence. Creating a governed task records control-plane state; it does not start a model, checkout arbitrary work, run tests, or dispatch an adapter.

Build context and test evidence for that task:

```bash
harness governance context build --task task_abc123 --project . --output json
harness governance tests plan task_abc123 --project . --output json
harness governance tests run task_abc123 --project . --output json
```

The JSON wrappers are `harness.governance_context_pack/v1`, `harness.governance_test_plan/v1`, and `harness.governance_test_run/v1`. The context pack records the governed task context hash. The test planner chooses local commands for the task type and records gate ids. The test runner writes local evidence and updates task metadata. These commands do not widen permissions, grant apply-back, or authorize future work by themselves.

Run merge readiness checks:

```bash
harness governance merge-check feature/governed-change --base main --project . --output json
```

The JSON wrapper is `harness.governance.merge_check/v1`. Merge-check requires a clean working tree, compares the requested branch to the base, scans for protected path changes, secret-like added text, dangerous execution strings, authority drift, provider permission widening, sandbox/network widening, deletion-heavy diffs, core deletions, vendored third-party changes, and the governance test command. It writes local evidence under `.harness/governance/merge-check/` and returns a pass, request-changes, or reject verdict.

Merge-check does not merge, push, comment on pull requests, call providers, acquire leases, execute registered adapters, start background work, grant approvals, or mutate the active repository. A passing merge-check is evidence for a human or a later explicit workflow; it is not an instruction to integrate the branch.

Inspect local data and cleanup eligibility:

```bash
harness governance data-audit --project . --output json
```

The JSON wrappers are `harness.data_inventory/v1` and `harness.data_cleanup_proposal/v1`. The command inventories local Harness state and proposes retention cleanup. It does not delete files, prune SQLite rows, modify artifacts, or repair evidence.

Audit external reference repositories without importing them:

```bash
harness governance references-audit --project . --root ../harness-references --output json
```

The JSON wrapper is `harness.reference_repositories_audit/v1`. The command scans only immediate Git checkout metadata under the reference root: repository name, path, sanitized `origin`, branch, HEAD SHA, dirty count, and local Git LFS file count. Curated repositories also include static profile metadata: upstream label, integration role, implementation guidance, pattern tags, and required-pattern coverage across agent runtime, workflow durability, protocols, tool contracts, observability, policy boundaries, and low-level isolation. It does not read source bodies, add files to context, execute reference code, call the network, pull, fetch, mutate repositories, or grant authority. Reference repos remain manual-review material with `manual_review_required=true`, `license_review_required=true`, `contents_included=false`, `model_context_allowed=false`, `execution_allowed=false`, and `mutation_allowed=false`.

Validate network policy and quarantine evidence:

```bash
harness governance network validate --policy /tmp/network-policy.json --project . --output json
harness governance network check-url https://docs.example.com/page --policy /tmp/network-policy.json --project . --output json
harness governance network quarantine https://docs.example.com/report.pdf --policy /tmp/network-policy.json --project . --output json
```

The JSON wrappers are `harness.governance_network_policy_check/v1`, `harness.governance_network_check_url/v1`, and `harness.governance_download_quarantine/v1`. Network policy evidence requires a task id, allowlist, request log path, quarantine path, approval id, expiration, request logging, quarantine, and metadata-service blocking. Network artifacts remain unpromoted until separate review evidence approves them.

Validate apply-back and promotion:

```bash
harness governance applyback validate --input /tmp/applyback-request.json --project . --output json
```

The JSON wrapper is `harness.governance_applyback_verdict/v1`. The request must bind the proposed promotion to a `task_id`, `segment_id` or `objective_id`, `context_pack_hash`, `approval_id`, `allowed_paths`, `changed_files`, `diff_summary`, and fresh passing `test_evidence`. Protected path hits require matching exception evidence. Quarantined artifacts are rejected unless visual, security, or quality review evidence has promoted them. The output includes `policy_hash`, `approval_id`, `diff_summary`, `changed_files`, `gate_ids`, hard-gate results, and explicit `operator_authority` fields showing that the command wrote durable evidence only and did not grant permission, future authority, or active repo mutation.

Blocked-state explanations are normalized across CLI, chat, and the TUI. `daemon inspect-lease`, `daemon execute`, `capabilities inspect`, `progress`, chat prompts such as “why is this blocked?” and “security blockers”, and the TUI right panel can show stable codes including `missing_approval`, `disabled_adapter`, `unsafe_metadata`, `unknown_adapter`, `sandbox_profile_mismatch`, `breaker_open`, and `forbidden_path_or_secret_like_content`. These explanations are read-only summaries of existing evidence; they do not create approvals, tasks, leases, runs, memory, artifacts, or execution.

`harness quickstart agent` prints the exact command sequence for the MVP agent path:

```bash
harness quickstart agent --project .
harness quickstart agent --project . --output json
```

The quickstart output covers scaffold, validate, preview, init, import, inspect, task creation, daemon lease, lease inspection, and bounded read-only execution. It is command composition only: the operator must run each command explicitly.

`harness quickstart agent` does not create files, initialize projects, import agents, create tasks, acquire leases, create runs, execute adapters, preflight backends, inspect backend settings, or start daemon work.

The `home` and `quickstart agent` text views use simple section headings for project state, next actions, steps, and safety reminders. Their JSON forms remain schema-stable for scripts.

Common list/status commands use compact tab-separated text headers for operator readability:

```bash
harness runs --project .
harness tasks list --project .
harness agents list --project .
harness daemon status --project .
```

Common inspect/explain commands use small section headings so operators can scan them without losing JSON stability:

```bash
harness agents inspect my_agent --project .
harness tasks inspect task_abc123def456 --project .
harness daemon inspect-lease task_lease_abc123def456 --project .
harness policy explain --subject-kind task --subject-id task_abc123def456 --project .
harness artifacts inspect artifact_abc123def456 --project .
```

The JSON forms of these commands remain unchanged for scripts and tests. A grouped command reference is available in [command_catalog.md](command_catalog.md).

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

## Registered Execution Dispatcher

The daemon execution layer is a registered-adapter dispatcher, not a generic executor:

```text
daemon run-once leases only
daemon execute dispatches only already-leased tasks to allowlisted adapters
no adapter means no execution
unknown adapter means fail closed
adapter descriptors are documentation and validation metadata, not permission grants
```

Inspect registered adapters without touching backends:

```bash
harness daemon adapters --project . --output json
harness daemon inspect-lease task_lease_abc123def456 --project . --output json
```

`daemon adapters` lists descriptor metadata only. `daemon inspect-lease` reports generic `execution_eligibility` without preflighting Codex, local model backends, Docker, or providers.

Dispatch an already-leased task:

```bash
harness daemon execute task_lease_abc123def456 --project . --output json
```

The dispatcher currently registers:

- `dry_run` for `phase_1a_test`, which writes metadata-only evidence.
- `read_only_summary` for `read_only_repo_summary`, which uses the supervised Codex CLI subscription backend with ChatGPT auth, `gpt-5.5`, low reasoning effort, and Codex read-only sandbox mode.
- `codex_isolated_edit` for `codex_code_edit`, which requires a valid hosted-boundary Codex approval before run creation and uses the supervised isolated Codex edit runner.
- `repo_planning` for `repo_planning`, which requires a valid hosted-boundary Codex approval before run creation and uses the supervised Codex CLI read-only sandbox to produce planning artifacts.

Codex queued execution uses the same safety split as direct Codex editing: hosted-boundary approval allows sending the isolated task context to Codex, but it is not apply-back approval. Apply-back remains denied by default unless an explicit apply-back approval provider approves the inspected diff. Denied apply-back is a successful safe outcome: the isolated edit completed, the diff was inspected, mutation was denied, and the active project stayed unchanged.

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
harness specs workbench quant --output json
harness specs agent quant_orchestrator --output json
```

Built-in inspection reads only the in-memory built-in registry. JSON output is schema-versioned:

- `harness.spec_registry/v1`
- `harness.agent_spec/v1`
- `harness.workbench_spec/v1`

## v0.6 Agent Declarations

The v0.6 Quant Workbench and agent profiles are declarative spec metadata. They make roles and customization points inspectable, but they do not create tasks, schedule workflows, execute agents, run Docker, call backends, connect to brokers, place orders, send messages, submit applications, or trade.

Inspect the quant workbench and built-in quant agents:

```bash
harness specs workbench quant --output json
harness specs agent quant_orchestrator --output json
harness specs agent quant_researcher --output json
harness specs agent commodities_researcher --output json
harness specs agent equities_researcher --output json
harness specs agent volatility_researcher --output json
harness specs agent data_engineer --output json
harness specs agent backtest_engineer --output json
harness specs agent low_level_optimizer --output json
harness specs agent risk_reviewer --output json
harness specs agent leakage_reviewer --output json
harness specs agent statistical_validity_reviewer --output json
harness specs preview agent commodities_researcher --output json
```

Agent previews include inherited group context and attached profile metadata where present. Profiles are customization metadata only: knowledge domains, preferred outputs, review responsibilities, forbidden actions, tags, and simple metadata. They do not change permissions or authorize execution.

The built-in `quant` workbench forbids live trading, broker actions, capital allocation, order placement, hosted fallback, and paid fallback. It also inherits the spec inspection safety boundary: no secret reads, no backend settings output, no environment inspection, and no `.harness/` state access.

Built-in specs are packaged as repo-tracked YAML files under `src/harness/builtin_specs/` and loaded through the typed registry. The folder layout mirrors the roadmap workbench tree for maintainability, but it is not runtime auto-discovery. Custom operator bundles remain explicit-path only through `harness specs validate/export/diff/preview`.

## v0.7 Agent Authoring

The v0.7 authoring commands let operators scaffold, validate, preview, and inspect one custom declarative agent bundle from an explicit local path. Custom authoring is metadata only. It does not execute agents, create tasks, create objectives, create runs, start daemon work, call model backends, preflight providers, run Docker, invoke shell tools, mutate active repo files, or authorize new tools. `harness agents contract <agent_id> --project . --output json` returns `harness.agent_contract/v1`, a read-only identity contract for built-in or imported project agents; it records model profile, backend id, tool policy, declared tool posture, input/output labels, budget source, trace requirements, contract hash, and authority flags without loading project agent source bodies or making the agent executable. `harness agents discover --workbench <id> --output json` and `harness agents allocate --workbench <id> ... --output json` add local AgentCard-style discovery and Contract-Net-style bid previews on top of those contracts without creating tasks, starting agents, or granting budgets.

Scaffold a custom bundle:

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
```

The scaffold command creates:

```text
agents/my_agent/
  agent.yaml
  profiles/
    default.yaml
```

Validate and preview the explicit bundle:

```bash
harness agents validate agents/my_agent --output json
harness agents preview agents/my_agent --output json
```

The JSON wrappers are:

- `harness.agent_scaffold/v1`
- `harness.agent_bundle_validation/v1`
- `harness.agent_bundle_preview/v1`

Agent bundles use `schema_version: harness.agent_bundle/v1` in `agent.yaml`. The authoring loader merges the custom agent and profiles with built-ins in memory only, validates the result through `SpecRegistry`, and rejects built-in id shadowing, missing references, parent cycles, forbidden paths, and policy broadening. Profiles are customization metadata only; they do not change permissions.

Built-ins remain immutable packaged YAML. Custom bundles are explicit-path only, are not auto-discovered, and are not persisted into `.harness/`, SQLite, or a runtime registry cache. Bundle paths, profile paths, and scaffold destinations must not include symlinks or hard-forbidden path targets. Profile files must be YAML. Importing custom agents into project state is a later milestone.

## v0.8 Project-Local Agent Registry

The v0.8 project-local registry imports a validated v0.7 agent bundle into initialized harness persistence. Imported agents remain declarative metadata. Importing an agent does not execute it, schedule work, call backends, create tasks automatically, create runs, create artifacts, start daemon work, or change immutable built-ins.

Import, list, and inspect a custom agent:

```bash
harness init --project .
harness agents import agents/my_agent --project . --output json
harness agents list --project . --output json
harness agents inspect my_agent --project . --output json
harness agents discover --project . --workbench coding --output json
harness agents allocate --project . --workbench coding --task-type security_review --required-kind reviewer --required-tag security --required-tool-policy read_only --max-candidates 1 --output json
```

Preview imported-agent effective metadata and drift:

```bash
harness agents preview-imported my_agent --project . --output json
```

Remove an unused imported agent:

```bash
harness agents remove my_agent --project . --output json
```

The JSON wrappers are:

- `harness.project_agent/v1` for import and inspect.
- `harness.project_agents/v1` for list.

Imported agents include the parsed agent declaration, attached profiles, source path, import timestamp, and deterministic content hash. Built-in ids cannot be shadowed, and duplicate project-local agent ids are rejected. The import command uses the same explicit-path, no-symlink, no-forbidden-path validation as `harness agents validate`.

Tasks may reference imported project-local agents:

```bash
harness tasks add --title "Use custom agent" --agent my_agent --workbench quant --project . --output json
```

The task record preserves `spec_source_kind: project` and the imported agent source path. This reference is still metadata only; it does not authorize execution or grant tools.

`preview-imported` recomputes the current source bundle hash and reports drift as `verified`, `changed`, `missing`, or `unavailable`; it does not rewrite the import record. `remove` applies only to project-local imported agents and rejects built-in ids, unknown ids, and imported agents referenced by any task. Refresh/replace remains intentionally deferred so imported-agent lifecycle changes stay explicit.

`agents discover` returns `harness.agent_discovery_catalog/v1`, local discovery cards derived from built-in specs and imported-agent metadata. `agents allocate` returns `harness.delegate_allocation/v1`, a deterministic bid preview that filters by workbench, agent kind, tool policy, outputs, tags, knowledge domains, review responsibilities, and explicit exclusions. These commands are planning surfaces only: they do not auto-discover code folders, read source bodies, create task records, start providers, execute agents or tools, mutate project state, grant delegate budgets, or grant permission. Attached clients can use `GET /agents/discovery` and `GET /agents/allocation` for the same projections.

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

The JSON wrapper is `harness.spec_effective_preview/v1`. Agent previews include the agent declaration plus resolved model profile, tool policy, memory scope, parent chain, effective agent fields, and attached profiles. Workbench previews include the workbench declaration, default model profile, allowed agents with resolved references, forbidden actions, and workbench-local declarative policy maps.

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

Run manifests are written as `harness.manifest/v1.1` and include additive runtime policy evidence such as `effective_policy`, `effective_policy_sha256`, and backend descriptor hash when a backend descriptor exists. Registered-adapter manifests also include a `delegate_budget` snapshot with the selected adapter id, serialized `harness.delegate_budget/v1` limits, sandbox alignment metadata, and validation gaps, so post-run audits can prove which budget was enforced. Manifest evidence does not include backend settings, API keys, environment variables, or secret-like metadata.

## Autonomy Profile Inspection

Inspect built-in autonomy profiles:

```bash
harness autonomy policy inspect --project . --profile manual --output json
harness autonomy policy inspect --project . --profile safe-local --output json
harness autonomy policy inspect --project . --profile supervised-codex --output json
```

The JSON wrapper is `harness.autonomy_policy_inspect/v1`. Autonomy profiles do not grant arbitrary authority. They describe whether a validated action request can proceed without live confirmation inside the current effective policy, sandbox, approval scope, leases, budgets, adapter allowlists, runtime controls, and evidence requirements. The default chat/app profile is `supervised-codex`, while `--autonomy manual` preserves interactive confirmation. Non-manual profiles such as `safe-local`, `supervised-codex`, and `daemon-safe` are policy inputs for bounded autonomous authorization; they cannot satisfy apply-back approval or broaden active repo write permissions by themselves. `supervised-codex` may auto-authorize only chat-routed isolated-edit Codex planning/edit execution, with active repo mutation still outside the auto path.

Line-oriented chat can select a non-manual profile explicitly:

```bash
harness --project . --plain --autonomous
harness --project . --plain --autonomy safe-local
harness --project . --plain --autonomy manual
```

`--autonomous` is shorthand for `--autonomy safe-local`. In non-manual profiles, side-effecting chat tool requests still become Harness action contracts first. Harness then evaluates the contract with the selected autonomy profile. Auto-allowed contracts execute through the same executor used by manual confirmation and write `.harness/autonomy/decisions.jsonl` plus `.harness/autonomy/approvals.jsonl` evidence. Approval-required contracts remain pending for confirmation, and denied contracts do not execute.

Under `safe-local`, auto-allowed control-plane writes are limited to local Harness records such as objectives, dry-run task records, dry-run task graphs, and explicit project memory notes. Chat-created tasks and task graphs receive stable idempotency keys so repeated equivalent autonomous task or graph requests return the existing objective/task records and checkpoint evidence instead of creating duplicates. Memory writes keep their project scope, source id, redaction state, hash, and non-authoritative lineage; memory cannot grant permissions or satisfy approvals.

Run a bounded autonomous act loop:

```bash
harness act "summarize this repo" --project . --autonomy safe-local --output json
```

The JSON wrapper is `harness.autonomous_read_loop/v1`. This command lets the chat model call read-only Harness chat tools within the selected profile's budgets, writes JSONL evidence under `.harness/autonomy/`, and stops on a final answer, budget exhaustion, tool failure budget exhaustion, model unavailability, policy denial, approval-required boundary, or objective-run boundary.

`harness act` is no longer read-only only. Side-effecting model tool requests become Harness action contracts first, and Harness evaluates those contracts with the selected autonomy profile. Under `safe-local`, auto-allowed control-plane contracts can create local objectives, dry-run tasks, dry-run task graphs, and memory notes. When an auto-created task graph yields an objective, `harness act` can immediately run that objective through the autonomous objective runner and return objective/task/lease/run/artifact evidence to the model loop.

Under `supervised-codex`, chat-routed isolated-edit requests auto-transition into the reviewed coding workflow without a live approval prompt. Harness creates scoped internal hosted-provider authority for Codex planning/edit execution, records an approved supervisor checkpoint for the confirmed reviewed workflow graph, dispatches `repo_planning`, `codex_isolated_edit`, sandbox-test evidence, implementation review, security review, and final synthesis, and records autonomy/approval/run/artifact evidence. Requests that target external filesystem locations such as Downloads or Desktop fail closed before orchestration with a visible boundary decision; they do not create approval prompts or repo tasks. Dirty active Git repositories use an isolated copy from the current workspace state for supervised Codex edits, with the dirty status and warning recorded in run evidence. Direct autonomous adapter dispatch still needs a scoped hosted approval profile. Active repo apply-back remains a separate higher boundary and is denied unless a separate explicit apply-back policy later permits it.

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

## Compare and Baseline Evidence

Compare two local run evidence snapshots:

```bash
harness compare run_abc123def456 run_def456abc123 --project . --output json
```

Save and compare against a named local baseline:

```bash
harness baseline set run_abc123def456 --name local-green --project . --output json
harness baseline compare run_def456abc123 --baseline local-green --project . --output json
```

The JSON wrappers are `harness.compare/v1`, `harness.baseline/v1`, and `harness.baseline_compare/v1`. Compare output reports drift across run status, runtime policy hash, backend descriptor hash, sandbox profile, approval evidence, task/objective linkage, artifact checksum/status metadata, and test-result evidence when present.

Baselines are local evidence snapshots stored through the harness runtime. They are not artifact-content copies and do not export file contents. Compare and baseline commands report evidence drift; they do not repair artifacts, execute tools, preflight backends, run Docker, create runs, create artifacts, mutate tasks, or start schedulers.

## Safety Evals and Trace Export

Run the local safety-smoke evidence suite:

```bash
harness evals run --suite safety-smoke --project . --output json
```

Export a local run trace in OTEL-shaped JSON:

```bash
harness traces export run_abc123def456 --format otel-json --project . --output json
harness traces export-objective obj_abc123 --format otel-json --project . --output json
```

The JSON wrappers are `harness.evals.safety_smoke/v1` and `harness.trace_export/v1`. Safety-smoke checks runtime policy evidence, backend boundaries, sandbox network settings, artifact drift, and task queue non-execution using existing local persistence. Run trace export links run, event, artifact, backend, approval, policy, and sandbox metadata where present. Registered-adapter run traces also include the selected adapter's serialized `harness.delegate_budget/v1` limits and validation-gap count, plus queue-wait and lease-lifecycle timing spans when the run is linked to a persisted lease attempt. Objective trace export links objective runs, scheduler/batch events, dispatch evidence, schedule profiles, objective evidence verification metadata, and the current objective evidence hash-chain head from persisted JSONL without executing adapters or reading artifact bodies. Trace envelopes declare OpenTelemetry Trace plus GenAI agent/tool and MCP semantic-convention families, carry W3C trace-context propagation requirements, and stamp compatible root spans with `gen_ai.operation.name`, `gen_ai.agent.*`, and workflow identity attributes where Harness has equivalent passive evidence. Event spans expose sanitized payload SHA-256, byte size, and key-list metadata; secret-like payload values and keys are redacted before trace projection. For objective traces, the trace envelope `ok` field follows objective evidence verification, and text output prints the evidence verification and hash-chain status. Attached clients can read the same OTEL-shaped projections through `GET /runs/{run_id}/trace` and `GET /objectives/{objective_id}/trace`; these local-server routes are read-only and include no-execution safety flags.

The security-layer completion audit is available through:

```bash
harness evals run --suite security-layer --project . --output json
harness security audit --project . --output json
```

The audit returns `harness.security_layer_audit/v1` and verifies the local-first security-layer completion scope: typed decisions, adapter sandbox profiles, manifest evidence, controls, detections, integrity checks, context/memory authority boundaries, blocked-state explanations, run trace exportability/provenance plus run-event payload metadata coverage, registered-adapter delegate-budget trace evidence, linked lease/queue trace evidence for dispatched runs, and autonomous objective JSONL linkage plus objective trace exportability/provenance and objective-event payload metadata coverage when objective evidence is present. Registered-adapter execution fails closed before dispatch when the adapter sandbox profile is missing, unknown, or schema-incompatible, and registered-adapter run manifests derive sandbox evidence from the selected adapter descriptor rather than task-type inference. The audit is read-only and does not create runtime records, execute adapters, preflight backends, call providers, run Docker, or remediate state.

The reference-informed orchestration readiness audit is available through:

```bash
harness evals run --suite orchestration-readiness --project . --output json
harness evals run --suite orchestration-replay --project . --output json
harness evals run --suite orchestration-workflows --project . --output json
harness evals run --suite orchestration-scenarios --project . --output json
harness orchestration audit --project . --reference-root ../harness-references --output json
harness orchestration replay --project . --output json
harness orchestration workflows --project . --output json
harness orchestration scenarios --project . --output json
```

The audit returns `harness.orchestration_readiness_audit/v1`.
It checks whether Harness has the production orchestration pieces selected from the reference systems: durable supervisor state, typed child-task delegation with `harness.agent_handoff_envelope/v1` plus `harness.agent_contract/v1`, local agent discovery and deterministic delegate allocation, serialized `harness.delegate_budget/v1` limits for each registered adapter, explicit objective lifecycle controls, supervisor checkpoints, bounded parallel scheduling, workflow coordination contracts, orchestration scenario conformance, append-only objective evidence, OTEL-shaped trace export, pending chat action recovery/audit/cleanup projections, sandboxed registered adapters, runtime controls and breakers, progress observability, protocol/tool exposure, external protocol compatibility, schema compatibility contracts, passive replay-drift detection, apply-back governance gates, and metadata-only reference repository hygiene.
It may validate existing runtime evidence if the project is already initialized, including warning when invalid or stale pending-action proposal metadata exists, warning when an objective has persisted run evidence but no objective JSONL chain, warning when an active descriptor-bound runtime control no longer maps to any registered adapter descriptor, warning when a curated reference repository is missing, and warning when Git LFS reference files are tracked but not materialized locally.
The external-protocol compatibility check verifies that model-provider protocols are registered while local OpenAPI stays metadata-only, cached MCP resources stay cached-resource-only, and MCP tool execution, external OpenAPI import, A2A remote agents, and gRPC remote tools stay fail-closed and non-model-visible by default. It also fails if those remote or extension descriptors omit the relevant W3C trace context and OpenTelemetry GenAI/MCP telemetry contracts.
The schema-compatibility check verifies that critical orchestration payloads are registered with explicit versions, producers, consumers, validation surfaces, upgrade policies, and passive authority flags, including agent contracts, agent discovery catalogs, handoff envelopes, delegate budgets, external protocols, readiness, efficiency, synthesis, orchestration replay audits, scenario conformance catalogs, reviewed workflow templates, workflow agent-selection requirements, workflow coordination catalogs, objective batch plans, objective evidence, checkpoints, traces, sandbox profiles, session tool policy, and local OpenAPI.

The `workflow_coordination_contracts` check and `harness orchestration workflows` command return `harness.workflow_coordination_catalog/v1`. They turn useful Microsoft Agent Framework, Temporal, LangGraph, Google ADK, and OpenAI Agents workflow ideas into Harness-owned contracts: durable supervisor, sequential steps, bounded fan-out/fan-in with batch barriers, typed handoffs, human approval pauses, append-only replay, external protocol boundaries, and memory context boundaries. The same catalog separates session state, workflow state, long-term memory state, and artifact/evidence state. It is passive metadata; it does not import reference source, execute agents, call providers, start protocol adapters, read artifact bodies, mutate files, or grant permission. Attached clients can read it at `GET /orchestration/workflows`, which includes `harness.workflow_coordination_summary/v1`.

The `orchestration_scenario_conformance` check and `harness orchestration scenarios` command return `harness.orchestration_scenario_catalog/v1`. They make the report's layered testing strategy inspectable as passive conformance evidence: unit, contract, replay, scenario, security, and benchmark rows cover duplicate dispatch/redelivery, slow branch fan-in barriers, approval reject pauses, checkpoint reject stops, missing terminal events, unsafe memory-to-hosted-model propagation, fail-closed remote protocols, retry/idempotency policy, and explicit live benchmark permits. It does not run adapters, providers, tools, live benchmarks, fault injection, or reference runtimes, and it does not instantiate the approval store or initialize uninitialized projects. Attached clients can read it at `GET /orchestration/scenarios`, which includes `harness.orchestration_scenario_summary/v1`.

The `agent_discovery_and_allocation` check verifies that Harness exposes A2A-AgentCard-inspired local discovery and Contract-Net-style delegate bidding without making discovery an execution path. It requires `harness.agent_discovery_catalog/v1` cards for the coding workbench, a `harness.delegate_allocation/v1` bid preview that deterministically selects the read-only `security_reviewer` for security review, and passive safety flags across catalog, cards, announcements, bids, and allocation output. It does not create task records, start agents, call providers, read source bodies, grant budgets, or grant permission.

The `agentic_security_controls` check makes the OWASP-style risks from the orchestration research report release-visible without adding a new runtime path. It aggregates existing controls into three rows: `memory_poisoning` confirms memory/context warnings are preserved, hosted and remote-vector transmission fail closed by default, and secret-like context is denied; `insecure_inter_agent_communication` confirms typed handoff envelopes, read-only handoff authority, traceparent propagation, payload hashes, and fail-closed remote agent protocols; `cascading_failures` confirms bounded parallel scheduling, adapter breaker visibility, safe replay policy for auto-allowed adapters, and replay probes for duplicate or blocked dispatch. The check is passive and reports the dependent readiness check statuses so a lower-level drift is visible in one place.
The replay-drift check returns `harness.orchestration_replay_audit/v1` through `harness evals run --suite orchestration-replay` or `harness orchestration replay`.
It always runs bounded synthetic cases for the expected happy path plus duplicate dispatch, slow-branch barrier, approval-reject, and missing-terminal drift conditions; when initialized objective JSONL exists, it also passively reduces captured objective evidence and compares semantic/verification outcomes without reading artifact bodies.
Draft-only objectives without runs are not treated as missing objective evidence.
The audit does not initialize projects, import reference code, read reference source bodies, include reference contents as model context, execute adapters, replay captured logs by executing side effects, clear pending action metadata, backfill objective evidence, call providers, call the network, mutate repositories, or grant permission.
The explicit `harness objectives reconcile-evidence <objective_id>` path can write a new objective JSONL chain from existing run metadata after an operator names the objective; it writes only objective evidence JSONL and does not modify existing objectives, tasks, runs, artifacts, sessions, approvals, repository files, providers, network state, or permissions.

The TUI cockpit shows a bounded passive readiness sample in its Evidence section for fast dashboard refreshes. That row carries `deep_audit_required=true`, passive safety flags, and the `harness orchestration audit --project . --output json` inspection command for full evidence. Attached clients can read the full projection at `GET /orchestration/readiness`; reference repository metadata remains opt-in through `include_references=true` and is still Git/LFS metadata only, including expected/missing/extra repository names and materialized/unmaterialized LFS file counts. These surfaces do not start providers, adapters, shells, Docker, filesystem mutation, or permission grants.

The orchestration efficiency audit is available through:

```bash
harness evals run --suite orchestration-efficiency --project . --output json
harness evals run --suite orchestration-microbenchmarks --project . --output json
harness evals run --suite orchestration-synthesis --project . --output json
harness orchestration synthesis --project . --reference-root ../harness-references --output json
```

The audit returns `harness.orchestration_efficiency/v1`.
It is the security-versus-complexity companion to readiness: it checks whether registered adapter complexity is paired with resolvable sandbox profiles, approval boundaries, autonomy defaults, and replay controls; verifies `harness.delegate_budget/v1` ceilings for runtime invocations, model calls, tool calls, branch fan-out, filesystem scope, network policy, active-repo write policy, and cost policy; verifies descriptor delegate-budget alignment with the selected sandbox profile; and confirms task metadata can only narrow those limits with sane numeric floors: runtime/model/tool/token/cost ceilings must be non-negative and branch fan-out must be at least one before task creation or registered-adapter dispatch.
It verifies retry/idempotency policy so redelivery cannot duplicate side effects, including `harness.task_replay_receipt/v1` receipts on accepted retry transitions and new task attempts; verifies daemon, manual queue, and foreground core pre-lease descriptor approval gating so approval-required registered adapters pause before attempts, leases, runs, backend preflight, or dispatch; verifies runtime-control and adapter-breaker pre-lease gating so disabled controls and open breakers pause guarded queue selection before attempts or leases; verifies no-run and adapter-boundary registered-adapter rejection finalization so denied, approval-required, or crashing dispatches do not leave active leases or stale attempts behind; verifies inconsistent active-lease renewal guards so daemon ticks do not keep invalid leases alive indefinitely; verifies expired-lease recovery guards so completed/failed linked runs are reconciled while missing or non-terminal linked runs fail for inspection instead of being requeued; verifies daemon stop/stale linked-run guards so daemon shutdown cannot duplicate still-running work; and verifies objective-runner pre-lease autonomy gating so autonomous scheduling cannot lease approval-required or denied candidates. It runs a deterministic in-process critical-path scheduler probe against the bounded `max_parallel` contract; exposes `harness.orchestration_microbenchmark_contracts/v1` for the report-recommended microbenchmarks covering handoff overhead, fan-out/fan-in, checkpoint latency, sandbox startup, tool adapter overhead, retry safety, trace overhead, shared model contention, and verification-stage ROI; and, when local runtime state already exists, measures objective/run evidence event counts and trace span counts without reading artifact bodies.
The audit also includes `harness.orchestration_live_benchmark_permits/v1`, a read-only permit projection for the live-only sandbox startup and shared-LLM contention rows.
Those permits show the exact approval backend, data boundary, task type, adapter scope, autonomy scope, runtime/model/tool budget, filesystem boundary, provider/sandbox requirement, and non-release-gate status required before a later manual live benchmark is allowed.
The companion `orchestration-microbenchmarks` suite returns `harness.orchestration_microbenchmarks/v1` and records bounded timing samples for the passive/synthetic parts of that same matrix: descriptor handoff projection, fan-out/fan-in scheduling, checkpoint evidence verification when runtime state exists, tool-adapter projection, retry policy validation, trace projection when evidence exists, and verification-gate projection.
The `orchestration-synthesis` suite and `harness orchestration synthesis` command return `harness.orchestration_synthesis/v1`, which combines reference repository metadata, readiness summaries, efficiency summaries, microbenchmark summaries, replay drift summaries, scenario conformance, adopted reference-pattern decisions, deliberate non-adoptions, and the current security-versus-complexity posture into one report.
It explains which ideas were adopted from systems such as Microsoft Agent Framework, Temporal, LangGraph, OpenAI Agents, Google ADK, MCP, A2A, OpenAPI, gRPC, OpenTelemetry, containerd, runc, gVisor, Firecracker, Bubblewrap, nsjail, and Kata, while also stating that pulled source is not imported, ambient tool/provider execution is not adopted, remote protocol execution does not fail open, active-repo apply-back is not hidden, captured-log replay is not a side-effect execution path, and live benchmarks are not automatic release gates.
The TUI cockpit Evidence section includes compact readiness, efficiency, microbenchmark, and `harness.orchestration_synthesis_summary/v1` rows; the synthesis row is derived from the already-computed passive summaries and shows source statuses, adopted/non-adopted pattern counts, posture, and the full `harness evals run --suite orchestration-synthesis --project . --output json` inspection command.
Attached clients can read the same passive suite at `GET /orchestration/microbenchmarks`, which includes `harness.orchestration_microbenchmarks_summary/v1` as a compact summary projection, can read layered scenario conformance at `GET /orchestration/scenarios`, and can read the combined synthesis at `GET /orchestration/synthesis`, which includes both the full `harness.orchestration_synthesis/v1` report and `summary_projection.schema_version=harness.orchestration_synthesis_summary/v1`.
Timed rows include a `harness.orchestration_microbenchmark_guardrail/v1` envelope with local mean/p95 thresholds and `release_blocking=false`, so operators can spot regressions without turning wall-clock variance into a hard release gate.
Provider-backed and sandbox-backed rows such as shared model contention and sandbox startup remain `skipped` with `measurement_mode=explicit_live_required`; their `measurements.live_permit` fields expose `harness.orchestration_live_benchmark_permit/v1` and stay `automated_execution_allowed=false`.
These commands, TUI rows, and attached-client projections do not initialize projects, execute adapters, replay captured logs by executing side effects, call providers, call the network, run Docker, import reference code, mutate files, read artifact bodies, repair state, or grant permissions.

Manual queue pre-lease gating, runtime-control and breaker pre-lease gating, adapter rejection finalization, inconsistent active-lease renewal guards, expired-lease recovery guards, and daemon stop/stale linked-run guards are included in the efficiency audit so `tasks run-next`, daemon scheduling, daemon recovery, foreground core execution, registered dispatch, the read-only compatibility dispatcher, and autonomous objective scheduling share the same approval/control-before-lease and no-stale-lease posture.

`harness doctor --release --output json` includes the same readiness contract as `orchestration_readiness_release_gates` with reference metadata disabled, including `agent_discovery_and_allocation`, `orchestration_scenario_conformance`, `replay_drift_detection`, and `agentic_security_controls`, plus `orchestration_efficiency_release_gates` for the security-versus-complexity audit summary, and `orchestration_synthesis_release_gates` for the combined no-reference release posture. The synthesis gate reports adopted reference-pattern ids, deliberate non-adoption ids such as `no_replay_side_effect_execution`, source report statuses, and the balanced/needs-review security-versus-complexity posture without importing reference code or including reference contents. It also includes `extension_config_path_safety`, a metadata-only check that fails release when configured `skill-load` or cached MCP resource paths contain symlink components; it does not read skill bodies or MCP resource bodies. Release doctor includes `session_transcript_health`, which fails release on malformed session transcript JSONL while reporting only paths, line numbers, and error classes, not transcript event bodies or malformed line contents. `harness session inspect`, `harness resume`, attached HTTP `/sessions`, `/api/session`, `/sessions/status`, `/sessions/{id}`, and `/sessions/{id}/status`, plus the dashboard/session-pane/right-pane projections, expose the same compact `harness.session_events_read/v1` health envelope without transcript bodies. A failing readiness, efficiency, or synthesis source check fails the release doctor; invalid or stale pending-action metadata is surfaced as a warning with the session inspect/cleanup command, and the release doctor does not clear it. Stale session `active_run_id` references are also warning-only unless the operator runs `harness doctor --repair`; session projections expose them as `harness.session_active_run_reference/v1` with the same repair command, and repair clears only the missing-run pointer and records `session.active_run_repaired` evidence.

These commands are evidence-only. They do not execute tools, preflight backends, run Docker, create runs, create artifacts, mutate tasks, inspect environment variables, or export artifact contents.

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
harness objectives add --title "Draft queue hardening" --workbench coding --draft --project . --output json
harness objectives list --project . --output json
harness objectives inspect obj_abc123def456 --project . --output json
harness objectives start obj_abc123def456 --reason "Ready to dispatch" --project . --output json
harness objectives suspend obj_abc123def456 --reason "Waiting for supervisor input" --project . --output json
harness objectives resume obj_abc123def456 --reason "Supervisor input received" --project . --output json
harness objectives timeout obj_abc123def456 --reason "Deadline exceeded" --project . --output json
harness objectives retry obj_abc123def456 --reason "Retry retryable failed work" --project . --output json
harness objectives complete obj_abc123def456 --reason "Accepted final evidence" --project . --output json
harness objectives cancel obj_abc123def456 --reason "Superseded by obj_xyz789" --project . --output json
```

Objective commands use stable JSON wrappers:

- `harness.objective/v1` for add and inspect output.
- `harness.objectives/v1` for list output.
- `harness.objective_lifecycle/v1` for suspend, resume, timeout, complete, and cancel output.
- `harness.objective_retry/v1` for retry output.

Objectives are metadata only in v0.3. They do not create tasks automatically and do not imply planning, routing, backend execution, scheduling, or autonomy. Suspending, resuming, timing out, completing, cancelling, or retrying an objective changes only objective/task control-plane records; required checkpoints can also move active objectives into `waiting_approval` and approved checkpoint gates can move them back to `active`. Suspended, retrying, and waiting-approval objectives are not eligible for autonomous dispatch until resumed, completed, or approved. Timed-out objectives require explicit retry before autonomous dispatch; completed and cancelled objectives are not eligible for retry or autonomous dispatch.

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

- `harness.task/v1` for add and status updates.
- `harness.tasks_inspect/v2` for single-task JSON inspection; `core_evidence` contains the canonical evidence bundle when available.
- `harness.tasks/v1` for list output.
- `harness.task_graph/v1` for graph output.

`tasks retry` is replay-policy aware for registered adapter tasks. Failed tasks using `safe` or `idempotent_with_key` adapters can return to `ready` or `blocked` according to dependencies. Failed tasks using `requires_fresh_approval` adapters are not requeued unless a valid scoped approval profile already exists for the adapter/task type. Failed tasks using `not_replayable` adapters remain terminal and require a new explicit task or workflow record instead of a retry.
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

`run-next` evaluates the highest-priority, oldest ready, blocked, or waiting-approval task candidates with the same guarded eligibility logic used by daemon lease acquisition: descriptor-required approvals, runtime controls, adapter breakers, daemon-forbidden policy metadata, dependencies, and active leases are checked before lease creation. Eligible work creates a local task attempt and active lease, marks the task `leased`, and returns `decision=leased_task` with the task, attempt, lease, and any skipped `pause_reasons` for higher-priority blocked candidates. Each newly leased attempt carries `harness.task_replay_receipt/v1` metadata and `tasks inspect --output json` projects it without reading artifact bodies or executing work. If no task is runnable, it returns `ok: true` with `decision=no_eligible_task` or `decision=paused`, `selected_task: null`, `attempt: null`, `lease: null`, and `pause_reasons` when approval, control, breaker, dependency, policy, or active-lease gates blocked candidates. It does not create a run record, create run artifacts, call a backend, execute tools, or mutate repository files outside the harness SQLite database.

Inspect the v0.4 daemon scheduler control plane:

```bash
harness daemon run-once --project . --output json
harness daemon status --project . --output json
harness daemon recover --project . --output json
harness daemon stop --project . --output json
```

`daemon run-once` performs a single local scheduler tick and exits. It may renew a coherent daemon-owned active lease, release an inconsistent active lease instead of renewing it, lease one eligible task, or pause when only dependency-blocked, approval-required, control-disabled, breaker-open, active-leased, or daemon-policy-forbidden tasks are available. Registered adapter descriptor approvals, runtime controls, and adapter breakers are evaluated before lease acquisition, so missing approval produces a `waiting_approval` pause reason and disabled controls or open breakers produce `control_disabled` or `breaker_open` pause reasons without creating a task attempt, lease, run, backend preflight, or adapter dispatch. It returns `harness.daemon_tick/v1` with `decision`, selected task/attempt/lease fields when a lease is acquired, and `pause_reasons` when tasks are paused. Inconsistent active leases get a `release_inconsistent_lease` daemon event and are not allowed to block later eligible work indefinitely.

`daemon status` returns `harness.daemon_status/v1` with active daemon records, recent daemon events, and `paused_tasks` so operators can debug queue state without reading SQLite manually; paused records include descriptor-sourced approval requirements, disabled runtime controls, and open adapter breakers from registered adapters. When `daemon status` marks a daemon stale, and when `daemon stop` expires daemon-owned leases, completed or failed linked-run evidence is reconciled, missing or non-terminal linked runs are failed for operator inspection, and unexecuted leases can still return tasks to `ready`, `blocked`, or `waiting_approval` according to dependencies and approvals. `daemon recover` returns `harness.daemon_recovery/v1` and applies the same linked-run discipline for explicitly expired active leases. Start and finish mutation paths require the caller to own an active lease; stale released/expired leases and wrong-owner finalizers fail without marking task attempts or runs complete. Ambiguous linked-run work is never requeued or retried automatically.

Daemon commands are scheduler-readiness control-plane operations only. They do not execute tasks, bind task attempts to runs, call Codex or local model backends, run Docker, create run artifacts, mutate active repo files, start unmanaged background work, add hosted fallback, add paid fallback, or expose backend settings and secrets.

The v0.4.5 dry-run adapter is the only exception to the no-run-binding daemon rule, and it is explicit:

```bash
harness tasks add --title "Dry-run contract" --execution-adapter dry_run --task-type phase_1a_test --project . --output json
harness daemon run-once --project . --output json
harness daemon inspect-lease task_lease_abc123def456 --project . --output json
harness daemon execute-dry-run task_lease_abc123def456 --project . --output json
```

`daemon execute-dry-run` requires an existing active lease id. It does not select work itself. It links the leased task attempt to a local `phase_1a_test` run, writes metadata-only run evidence through existing harness artifact APIs, marks the task and attempt succeeded, and releases the lease. It returns `harness.daemon_execute_dry_run/v1`. It does not call Codex, preflight a local model backend, run Docker, execute shell commands, access the network, mutate active repo files, or use hosted or paid fallback.

`daemon inspect-lease` is read-only and returns `harness.daemon_lease/v1` with the lease, linked task, linked attempt, linked run/manifest when present, dry-run eligibility, and recovery recommendation. `daemon recover` can reconcile existing dry-run, read-only, or generic registered-adapter evidence, such as a completed run whose task or attempt remained non-terminal, but it must not create another run or retry ambiguous work automatically.

The v0.5 read-only adapter is the first bounded real execution adapter, and it is also explicit:

```bash
harness tasks add --title "Read-only summary" --execution-adapter read_only_summary --task-type read_only_repo_summary --project . --output json
harness daemon run-once --project . --output json
harness daemon inspect-lease task_lease_abc123def456 --project . --output json
harness daemon execute-read-only task_lease_abc123def456 --project . --output json
```

`daemon execute-read-only` requires an existing active lease id and a valid hosted-boundary Codex approval profile for `read_only_repo_summary`. It does not select work itself. It links the leased task attempt to one `read_only_repo_summary` run, uses the configured `codex_cli` subscription backend in Codex read-only sandbox mode, records manifest/artifact/trace evidence through existing harness runtime APIs, marks the task and attempt terminal, and releases the lease. It returns `harness.daemon_execute_read_only/v1`.

The compatibility command name and JSON schema are unchanged, but the backend route is now Codex subscription rather than the local OpenAI-compatible backend. Missing hosted-boundary approval or unavailable Codex CLI fails before run creation. The read-only summary route does not use paid API fallback, hosted fallback outside Codex CLI, `OPENAI_API_KEY`, the local model backend, Docker, generic shell execution, or active repository mutation.

The read-only adapter can use only `list_files`, `read_file`, `git_status`, `git_diff`, and `final_answer`. It does not authorize Codex execution, Docker, shell access, hosted fallback, paid fallback, OpenAI API usage, active repo writes, MCP/A2A, browser/email/calendar tools, generic task execution, or unmanaged daemon loops. `daemon inspect-lease` reports read-only eligibility, and `daemon recover` may reconcile existing read-only linked-run evidence without creating a second run.

Read-only execution troubleshooting:

- Local backend unavailable before run creation: no run is created; the lease is released, the attempt/task are marked `failed`, and the daemon rejection event records the terminal status.
- Missing hosted-boundary approval at direct execution time creates no run; the lease is released and the attempt/task are marked `waiting_approval`.
- Task not eligible: inspect the lease to review status, metadata, approvals, and linked attempt state.
- Attempt already linked to a run: do not execute again; inspect the lease or run `daemon recover`.
- Recovery required: `daemon recover` may reconcile completed or failed linked-run evidence, but it must not retry or create another run automatically.

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
