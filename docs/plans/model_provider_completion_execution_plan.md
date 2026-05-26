# Model Provider Completion Execution Plan

Status: active

Date: 2026-05-22

Owner: Harness model/provider wiring

Related plans:

- `docs/plans/model_provider_registry_integration_plan.md`
- `docs/plans/model_provider_flawless_experience_plan.md`

Reference implementations:

- `.harness/reference-code/pi`
- `.harness/reference-code/opencode`

## Goal

Finish Harness model/provider wiring so the app has a complete, inspectable, operator-safe provider system comparable to pi and opencode, while preserving Harness invariants:

- no hidden provider fallback
- no hidden model fallback
- no credential leakage in catalog, events, TUI, or logs
- provider execution only after explicit model selection, validation, policy checks, and approval evidence
- metadata-only catalog and picker reads unless the operator explicitly requests discovery or auth

The intended end state is:

```text
provider catalog + account state + model catalog
  -> explicit model selection
  -> validation resolves provider/model/variant/protocol/credential source
  -> session persists the selected canonical model and validation evidence
  -> runtime builds a canonical provider request
  -> protocol adapter serializes to provider-native payload
  -> provider stream normalizes into Harness ProviderEvent records
  -> transcript, timeline, usage, cost, and errors are durable and redacted
```

## Current Baseline

Already present:

- `src/harness/model_registry.py`
  - provider descriptors
  - model descriptors
  - aliases
  - variants
  - option resolution
  - fail-closed validation
- `src/harness/model_catalog.py`
  - provider/model catalog projections
  - validation projection
- `src/harness/model_discovery.py`
  - explicit OpenAI-compatible `/models` discovery with local endpoint validation and hosted approval flag
- `src/harness/provider_auth.py`
  - provider account record shape and active-account lookup
- `src/harness/protocol_adapters.py`
  - protocol adapters for `codex_cli`, `openai_chat`, `openai_responses`, `openai_codex_responses`, `anthropic_messages`, `google_generative`, and `bedrock_converse`
- `src/harness/session_runtime.py`
  - explicit selected model validation before runtime provider dispatch
- `src/harness/tui.py`
  - model catalog pane, model picker dialog, and session-scoped model selection persistence
- `src/harness/cli/main.py`
  - `providers`, `models`, `models refresh`, `models validate`, `models inspect`, preferences, favorites, and defaults
- Tests currently covering catalog safety, adapters, cross-provider handoff, and TUI backend wiring.

Verified baseline command:

```bash
pytest -q tests/test_model_catalog.py tests/test_protocol_adapters.py tests/test_tui_backend_wiring.py
```

Baseline result observed on 2026-05-22:

```text
69 passed in 40.68s
```

## Gap Summary

The remaining gap is not basic model listing. It is the active provider system:

- provider/account/credential state is not fully resolved at runtime
- OAuth and API-key provider connect flows are mostly placeholders
- discovery is OpenAI-compatible only and does not use provider-specific hooks
- TUI model/provider management is useful but not yet as complete as opencode
- default model resolution is not a first-class audited selection path
- server/API parity is partial
- production provider details such as signing, headers, retries, usage, cost, and compatibility transforms need hardening

## Reference Lessons To Preserve

From pi:

- a model registry should load built-ins, custom models, overrides, auth state, and dynamic providers
- available models should be filtered by configured auth without refreshing credentials unexpectedly
- runtime request auth should be resolved through one API that returns API key and headers or a clear error
- custom providers and model headers need schema validation and reload behavior

From opencode:

- provider state should merge catalog, config, env, auth, plugin hooks, model discovery, enable/disable filters, variants, and runtime loaders
- provider APIs should expose `all`, `connected`, `default`, and auth methods distinctly
- TUI model picker should prioritize current, favorites, recents, available providers, and provider-connect actions
- provider/model errors should include suggestions, not hidden fallback

Harness-specific constraint:

- defaults and closest-match suggestions may help the operator choose, but runtime must never silently substitute them for the selected ref.

## Implementation Phases

### Phase 0: Stabilize The Tracking Surface

Objective: make this plan the active checklist and prevent drift between docs, tests, and implemented behavior.

Tasks:

- [x] Add this plan to `docs/operator_guide.md` or `docs/smoke_checklist.md` as the active model/provider follow-up.
- [x] Mark older completed model/provider plans as historical if they remain in the repo.
- [x] Add a short `Current gaps` note to the operator guide.
- [x] Add a smoke command section for model/provider wiring.

Acceptance:

- A maintainer can find this plan from the operator guide.
- Historical plans do not imply provider auth/runtime credential work is finished.

Suggested tests:

```bash
pytest -q tests/test_docs_phase_3d.py tests/test_cli_smoke.py
```

### Phase 1: Credential Resolver And Runtime Integration

Objective: introduce one credential resolution layer and make every runtime adapter use it.

New or changed module:

- `src/harness/provider_auth.py`
- `src/harness/model_registry.py`
- `src/harness/protocol_adapters.py`
- `src/harness/backends/local_openai.py`
- `src/harness/session_runtime.py`

Tasks:

- [x] Define `ResolvedProviderCredential`.
  - `provider_id`
  - `credential_kind`
  - `status`
  - `source`
  - `env_var`
  - `account_id`
  - `expires_at`
  - `headers`
  - `api_key`
  - `redaction_state`
  - `credential_value_included`
  - `credentials_included`
- [x] Define `ProviderCredentialResolutionError`.
  - `provider_unknown`
  - `credential_missing`
  - `credential_expired`
  - `credential_refresh_required`
  - `credential_kind_unsupported`
  - `credential_source_unavailable`
- [x] Implement `resolve_provider_credential(config, provider, store, *, allow_secret_material: bool)`.
- [x] Make catalog projections call resolver in metadata mode only.
  - No secret material.
  - No network.
  - No refresh.
- [x] Make runtime execution call resolver with `allow_secret_material=True` only after model selection validation and policy/approval checks.
- [x] Replace hardcoded OpenAI-compatible runtime `"api_key": "local"` for hosted providers.
- [x] Preserve local placeholder credentials for known local providers such as Ollama.
- [x] Support `header_env_refs` for custom providers.
- [x] Include redacted credential source evidence in `session.model_validation` and provider `MODEL_STARTED` payloads.
- [x] Add explicit missing-credential failure before any provider network call.

Acceptance:

- Runtime adapters use the same credential resolver.
- Missing hosted provider credentials fail before network access.
- Catalog and TUI never include credential values.
- Local provider placeholder auth remains possible without leaking into hosted providers.

Suggested tests:

```bash
pytest -q tests/test_model_catalog.py tests/test_protocol_adapters.py tests/test_session_runtime.py
```

New tests to add:

- `test_runtime_blocks_missing_env_credential_before_network`
- `test_runtime_uses_env_credential_without_persisting_value`
- `test_custom_provider_header_env_refs_are_resolved_only_at_runtime`
- `test_catalog_metadata_resolution_never_includes_secret_material`
- `test_openai_chat_adapter_does_not_use_local_placeholder_for_hosted_provider`

### Phase 2: Provider Account Secret Storage

Objective: turn provider accounts into usable credentials without exposing secrets.

New or changed module:

- `src/harness/provider_auth.py`
- `src/harness/memory/sqlite_store.py`
- `src/harness/memory/schema.sql`
- `src/harness/cli/main.py`

Tasks:

- [x] Decide storage backend for local secrets.
  - Preferred: OS keychain abstraction if available.
  - Initial acceptable path: file-backed local secret store with `0600` permissions and explicit docs.
- [x] Store account metadata in SQLite.
- [x] Store secret values outside session events and catalog cache.
- [x] Add lock/atomic-write behavior for file-backed secret writes.
- [x] Add redaction helpers for account metadata.
- [x] Support account kinds:
  - `env`
  - `api_key`
  - `oauth`
  - `static_local`
  - `codex_login`
  - `aws_env`
  - `aws_profile`
- [x] Add CLI prompts/flags for API-key account creation.
- [x] Add `providers accounts` JSON shape with no secret value fields.
- [x] Add account activation and deletion evidence events.

Acceptance:

- A provider account can supply runtime credentials.
- Secret values never appear in:
  - catalog JSON
  - session events
  - TUI dashboard
  - logs
  - command output
- Account removal removes or invalidates the secret payload.

Suggested tests:

```bash
pytest -q tests/test_model_catalog.py tests/test_cli_smoke.py tests/test_paths_security.py
```

New tests to add:

- `test_provider_login_api_key_writes_secret_store_and_redacted_account`
- `test_provider_logout_removes_secret_payload`
- `test_provider_account_activation_changes_runtime_resolution`
- `test_provider_accounts_json_never_contains_secret_value`

### Phase 3: Provider Connect And Auth API

Objective: expose real provider connect/disconnect flows across CLI, server, and TUI.

New or changed module:

- `src/harness/local_server.py`
- `src/harness/core_service.py`
- `src/harness/tui.py`
- `src/harness/cli/main.py`
- `src/harness/provider_auth.py`

Tasks:

- [x] Define provider auth method projection.
  - `api_key`
  - `env`
  - `oauth`
  - `aws_profile`
  - `aws_env`
  - provider-specific metadata prompts
- [x] Implement server routes:
  - `GET /provider/auth`
  - `POST /provider/{provider_id}/auth/api-key`
  - `POST /provider/{provider_id}/auth/env`
  - `DELETE /provider/{provider_id}/auth`
  - `POST /provider/{provider_id}/auth/activate`
- [x] Keep OAuth routes explicit and fail-closed until Phase 4.
- [x] Make TUI provider connect actions call action/service methods, not direct writes.
- [x] Render provider connect results as evidence:
  - account created
  - account activated
  - credential source
  - credential value included: false
  - provider execution started: false
- [x] Add model picker action for provider connect when selected provider is missing credentials.
- [x] Add disabled state text for hosted providers missing approval vs missing credentials.

Acceptance:

- Provider connect is available through CLI, local server, and TUI.
- TUI connect is an explicit action path with persisted evidence.
- Provider connection does not select or execute a model automatically.

Suggested tests:

```bash
pytest -q tests/test_local_server.py tests/test_core_service.py tests/test_tui_backend_wiring.py tests/test_tui_codex_mode.py
```

New tests to add:

- `test_provider_auth_methods_projection_lists_supported_methods`
- `test_server_provider_api_key_connect_redacts_secret`
- `test_tui_provider_connect_persists_evidence_without_provider_execution`
- `test_connect_provider_does_not_change_active_model`

### Phase 4: OAuth And Token Refresh

Objective: add OAuth support without weakening the local-first safety model.

New or changed module:

- `src/harness/provider_auth.py`
- `src/harness/local_server.py`
- `src/harness/cli/main.py`
- `src/harness/tui.py`

Tasks:

- [x] Define OAuth account schema.
  - `provider_id`
  - `account_id`
  - `refresh_token` secret reference
  - `access_token` secret reference
  - `expires_at`
  - scopes
  - redacted metadata
- [x] Add PKCE helper.
- [x] Add OAuth method descriptors.
- [x] Implement authorize/callback flow for one provider first.
  - Candidate: OpenAI/Codex-style OAuth or Anthropic, depending on available stable reference.
- [x] Add refresh path that runs only during runtime credential resolution or explicit status refresh.
- [x] Add a refresh lock to prevent concurrent refresh races.
- [x] Never refresh OAuth during metadata-only catalog listing.
- [x] Add TUI code/manual-code OAuth callback flows.
- [x] Persist token refresh evidence without token values.

Acceptance:

- OAuth can connect one provider end to end.
- Expired tokens refresh before runtime provider call.
- Refresh failure blocks before provider execution.
- Metadata projections never refresh tokens.

Suggested tests:

```bash
pytest -q tests/test_model_catalog.py tests/test_local_server.py tests/test_tui_backend_wiring.py
```

New tests to add:

- `test_oauth_authorize_returns_browser_or_code_method_without_secret_leakage`
- `test_oauth_callback_stores_redacted_account`
- `test_oauth_refresh_happens_only_for_runtime_resolution`
- `test_expired_oauth_token_blocks_when_refresh_fails_before_network`

### Phase 5: Active Provider Registry Service

Objective: add an opencode/pi-style provider state layer without hidden fallback.

New or changed module:

- `src/harness/provider_registry.py` or `src/harness/active_provider_registry.py`
- `src/harness/model_registry.py`
- `src/harness/operator_context.py`
- `src/harness/local_server.py`

Tasks:

- [x] Define `ActiveProviderState`.
  - provider descriptor
  - connected status
  - credential status
  - credential source
  - enabled state
  - catalog source
  - model count
  - default model candidate
  - constraints
- [x] Define `ActiveModelState`.
  - model descriptor
  - availability
  - blocked reasons
  - variant list
  - capabilities
  - cost
  - limits
  - source
- [x] Merge sources in deterministic order:
  - built-in provider metadata
  - `config.backends`
  - `.harness/models.yaml`
  - discovered model cache
  - provider account metadata
  - env availability
  - runtime flags
- [x] Expose methods:
  - `list_all_providers()`
  - `list_connected_providers()`
  - `list_available_models()`
  - `get_provider(provider_id)`
  - `get_model(provider_id, model_id)`
  - `suggest_models(raw_query)`
  - `resolve_session_default(session_id)`
- [x] Use this service for catalog projections instead of each caller recomputing partial state.
- [x] Add clear distinction between:
  - known catalog model
  - available model
  - executable model
  - selected model
- [x] Preserve existing JSON schemas or version any breaking response changes.

Acceptance:

- One service determines provider/model availability.
- CLI, TUI, local server, and session runtime agree on enabled/connected/blocked status.
- Suggestions are visible but never auto-applied.

Suggested tests:

```bash
pytest -q tests/test_model_catalog.py tests/test_local_server.py tests/test_session_runtime.py tests/test_tui_backend_wiring.py
```

New tests to add:

- `test_active_provider_state_merges_config_env_accounts_and_discovery`
- `test_available_models_exclude_missing_credentials_but_catalog_keeps_them_visible`
- `test_suggestions_do_not_change_validation_result`
- `test_cli_tui_server_catalogs_share_active_registry_status`

### Phase 6: Explicit Default Model Resolution

Objective: support useful defaults while keeping runtime selection explicit and auditable.

New or changed module:

- `src/harness/model_registry.py`
- `src/harness/session_runtime.py`
- `src/harness/memory/sqlite_store.py`
- `src/harness/cli/main.py`
- `src/harness/tui.py`

Tasks:

- [x] Define `ModelSelectionSource`.
  - `command_arg`
  - `session_override`
  - `session_default`
  - `workspace_default`
  - `operator_preference`
  - `workbench_default`
- [x] Add `resolve_model_for_session(session_id, requested_ref=None)`.
- [x] Resolution order:
  - explicit command/request ref
  - session selected ref
  - session default ref
  - workspace configured default ref
  - operator default preference
  - workbench default profile only if it resolves to a concrete catalog ref
- [x] Emit `session.model_resolution` before execution.
- [x] Include:
  - source
  - raw ref
  - canonical ref
  - alias used
  - blocked reasons
  - no hidden fallback flags
- [x] Block when no concrete ref can be resolved.
- [x] Add TUI display for default source and active selected model.

Acceptance:

- Runtime can use explicit defaults when configured.
- Every default is recorded as a selection source.
- Missing default does not fall through to a provider.

Suggested tests:

```bash
pytest -q tests/test_session_runtime.py tests/test_model_catalog.py tests/test_tui_backend_wiring.py
```

New tests to add:

- `test_session_runtime_uses_session_selected_model_with_resolution_event`
- `test_workspace_default_model_resolution_is_audited`
- `test_missing_default_model_blocks_without_hidden_fallback`
- `test_default_preference_must_validate_before_execution`

### Phase 7: Provider-Specific Runtime Hardening

Objective: make the existing protocol adapters production-ready.

New or changed module:

- `src/harness/protocol_adapters.py`
- `src/harness/provider_content.py`
- `src/harness/provider_events.py`
- `src/harness/backends/local_openai.py`

Tasks:

- [x] OpenAI Chat:
  - use credential resolver
  - support provider/model headers
  - support `max_completion_tokens` vs `max_tokens`
  - support tool call streaming shape variants
  - support OpenRouter-compatible reasoning fields where configured
- [x] OpenAI Responses:
  - support background/refusal/error details
  - support function-call argument completion
  - preserve response ids
  - normalize usage and cache fields
- [x] OpenAI Codex Responses:
  - separate model options from normal OpenAI Responses when needed
  - include Codex-specific reasoning/approval metadata
- [x] Anthropic Messages:
  - configurable beta headers
  - thinking budget support
  - tool input streaming
  - cache control and long-cache retention metadata
- [x] Google Generative:
  - API-key and OAuth credential paths
  - image/file parts
  - thought signature preservation
  - safety/block reason mapping
- [x] Bedrock Converse:
  - real AWS SigV4 signing
  - profile/env/bearer resolution
  - region and cross-region model handling
  - Bedrock error classification
- [x] Add abort/timeout propagation to all streaming adapters.
- [x] Normalize provider errors:
  - auth
  - rate limit
  - context overflow
  - invalid request
  - server unavailable
  - provider policy block

Acceptance:

- Each adapter has tests for auth, payload, streaming, tool calls, usage, and errors.
- Bedrock no longer sends placeholder Harness AWS headers as a substitute for signing.
- Unsupported provider-native content fails visibly.

Suggested tests:

```bash
pytest -q tests/test_protocol_adapters.py tests/test_cross_provider_handoff.py tests/test_provider_adapters.py
```

New tests to add:

- `test_openai_responses_function_call_arguments_accumulate`
- `test_anthropic_beta_headers_from_model_metadata`
- `test_google_safety_block_maps_to_provider_policy_error`
- `test_bedrock_request_is_sigv4_signed`
- `test_all_protocols_propagate_abort_before_next_chunk`

### Phase 8: Discovery And Catalog Enrichment

Objective: move from sparse OpenAI-compatible discovery to provider-aware model catalogs.

New or changed module:

- `src/harness/model_discovery.py`
- `src/harness/model_registry.py`
- `src/harness/memory/sqlite_store.py`
- `src/harness/cli/main.py`

Tasks:

- [x] Define `ProviderDiscoveryAdapter`.
  - `provider_id`
  - `supports(provider)`
  - `discover(provider, credential, policy)`
- [x] Keep OpenAI-compatible `/models` as one adapter.
- [x] Add static catalog ingest from a local generated file for broad model metadata.
- [x] Add provider-specific discovery where safe:
  - [x] OpenAI-compatible routers
  - [x] OpenRouter-compatible metadata
  - [x] Google model list
  - [x] Bedrock foundation model list
  - [x] Anthropic static list unless API has a stable list endpoint
- [x] Add cache TTL metadata.
- [x] Keep provider refresh explicit through `harness models refresh <provider_id>` and add provider-specific discovery behind that command.
- [x] Add `--metadata-only` and `--with-credentials` behavior where applicable.
- [x] Store:
  - context limits
  - max output
  - modalities
  - tool support
  - reasoning support
  - cost
  - status
  - release date/family
- [x] Add stale-cache and last-refresh display.

Acceptance:

- Catalog can be enriched without provider execution.
- Hosted refresh requires explicit approval and credential resolution.
- Stale or failed discovery never deletes static known model metadata silently.

Suggested tests:

```bash
pytest -q tests/test_model_catalog.py tests/test_model_registry.py
```

New tests to add:

- `test_provider_discovery_adapter_registry_is_metadata_only_until_refresh`
- `test_hosted_discovery_uses_credentials_only_with_explicit_approval`
- `test_discovery_cache_merge_preserves_static_metadata_on_refresh_failure`
- `test_stale_discovery_cache_is_visible_in_catalog_projection`

### Phase 9: Custom Provider And Plugin Hooks

Objective: support pi/opencode-style custom providers without letting them bypass Harness policy.

New or changed module:

- `src/harness/config.py`
- `src/harness/model_registry.py`
- `src/harness/plugin` or existing plugin surfaces
- `src/harness/protocol_adapters.py`

Tasks:

- [x] Extend `.harness/models.yaml` schema for:
  - provider name
  - provider endpoint
  - provider protocol
  - credential kind
  - env vars
  - headers from env refs
  - per-model api id
  - model variants
  - compatibility options
  - whitelist/blacklist
  - disabled models
- [x] Add schema validation with actionable errors.
- [x] Support hot reload for model picker/catalog reads.
- [x] Add plugin provider hook interface.
- [x] Require plugin providers to declare:
  - protocol
  - data boundary
  - credential behavior
  - model list
  - safety notes
- [x] Reject plugin/custom providers that lack endpoint or credential policy.
- [x] Add allowlist for protocol adapter registration.

Acceptance:

- Custom providers can be added without code changes for supported protocols.
- Custom provider credentials are never embedded directly in config except local static placeholders.
- Plugin provider hooks cannot execute during metadata-only catalog reads unless explicitly classified as safe and local.

Suggested tests:

```bash
pytest -q tests/test_model_registry.py tests/test_model_catalog.py tests/test_packaging_v1_2.py
```

New tests to add:

- `test_custom_provider_requires_boundary_and_endpoint`
- `test_custom_provider_header_values_must_use_env_refs`
- `test_custom_model_override_merges_capabilities_and_variants`
- `test_plugin_provider_hook_cannot_run_network_during_catalog_projection`

### Phase 10: TUI Model And Provider Management

Objective: make the app workflow complete and truthful.

New or changed module:

- `src/harness/tui.py`
- `src/harness/operator_context.py`
- `src/harness/right_pane.py`
- `src/harness/core_service.py`

Tasks:

- [x] Picker sections:
  - current session model
  - favorites
  - recents
  - connected providers
  - local providers
  - hosted providers
  - disabled/blocked providers
- [x] Model detail panel:
  - provider
  - model id
  - canonical ref
  - source
  - context
  - max output
  - modalities
  - tools
  - reasoning
  - variants
  - cost
  - data boundary
  - credential status
  - blocked reasons
  - inspect command
- [x] Provider detail panel:
  - display name
  - connected status
  - auth methods
  - enabled state
  - model count
  - refresh status
  - connect/disconnect command
- [x] Actions:
  - select model
  - favorite/unfavorite
  - set default
  - connect provider
  - disconnect provider
  - refresh provider models
  - inspect model
- [x] Every action routes through core service or CLI-equivalent logic.
- [x] Add keyboard hints only after actions work.
- [x] Add visible evidence status after each action.
- [x] Keep picker navigation side-effect free.

Acceptance:

- Operators can connect a provider, refresh models, pick a model, set favorite/default, and inspect blocked reasons from the TUI.
- Picker never starts provider/model execution.
- Disabled or unavailable models are visible but cannot be selected without a blocked evidence result.

Suggested tests:

```bash
pytest -q tests/test_tui_backend_wiring.py tests/test_tui_codex_mode.py tests/test_orchestration_cockpit.py
```

New tests to add:

- `test_model_picker_orders_current_favorites_recent_connected_then_blocked`
- `test_model_detail_panel_shows_boundary_credentials_and_inspect_command`
- `test_provider_connect_action_routes_through_core_service`
- `test_unavailable_model_selection_persists_blocked_evidence`
- `test_unimplemented_shortcuts_are_not_rendered`

### Phase 11: Server/API Parity

Objective: expose enough provider/model APIs for local clients and future UI work.

New or changed module:

- `src/harness/local_server.py`
- `src/harness/core_service.py`
- `src/harness/model_catalog.py`
- `src/harness/provider_auth.py`

Tasks:

- [x] Stabilize routes:
  - `GET /providers`
  - `GET /providers/{provider_id}`
  - `GET /provider/auth`
  - `POST /provider/{provider_id}/auth/...`
  - `DELETE /provider/{provider_id}/auth`
  - `GET /models`
  - `GET /models/{provider_id}/{model_id}`
  - `GET /models/validate`
  - `POST /sessions/{session_id}/model`
  - `GET /models/preferences`
  - `POST /models/preferences/favorite`
  - `POST /models/preferences/default`
- [x] Provide opencode-compatible aliases only where the shape is clearly mapped.
- [x] Include `all`, `connected`, `default`, and `blocked` distinctions.
- [x] Include auth methods and OAuth support flags.
- [x] Return suggestions for unknown providers/models.
- [x] Add OpenAPI updates.
- [x] Add route-level auth tests.

Acceptance:

- Local API consumers can build a model/provider settings screen without scraping CLI text.
- Server response schemas preserve safety flags.
- Unknown/blocked responses do not mutate session state unless explicitly requested.

Suggested tests:

```bash
pytest -q tests/test_local_server.py tests/test_core_service.py
```

New tests to add:

- `test_models_get_returns_one_model_or_suggestions`
- `test_provider_list_exposes_all_connected_default_without_secrets`
- `test_session_model_post_validates_and_records_selection`
- `test_provider_auth_routes_require_local_server_auth`

### Phase 12: Usage, Cost, Limits, And Policy Enforcement

Objective: make model execution observable and governable after it starts.

New or changed module:

- `src/harness/provider_events.py`
- `src/harness/protocol_adapters.py`
- `src/harness/live_artifacts.py`
- `src/harness/session_runtime.py`
- `src/harness/context_budget.py`

Tasks:

- [x] Normalize token usage for all protocols.
- [x] Estimate cost using model descriptor pricing.
- [x] Record provider-reported cost separately.
- [x] Track cache read/write usage.
- [x] Validate input modalities before execution.
- [x] Validate tool support before passing tools.
- [x] Validate context budget before provider call where possible.
- [x] Add policy gates:
  - [x] max cost per run
  - [x] max tokens per turn
  - [x] hosted provider approval
  - [x] paid API approval
  - [x] data-boundary approval
- [x] Add runtime blocked states for policy failures.

Acceptance:

- Session timeline shows usage/cost evidence when available.
- Provider cost is never invented as exact when estimated.
- Unsupported modalities/tools fail before provider execution.
- Policy blocks happen before network access.

Suggested tests:

```bash
pytest -q tests/test_protocol_adapters.py tests/test_session_runtime.py tests/test_core_projection.py
```

New tests to add:

- `test_usage_normalization_all_protocols`
- `test_estimated_cost_is_marked_estimated`
- `test_paid_provider_cost_policy_blocks_before_network`
- `test_image_input_blocks_for_text_only_model`
- `test_tool_request_blocks_when_model_tool_support_false`

### Phase 13: Reliability, Retry, Abort, And Recovery

Objective: make provider execution resilient without hiding failures.

New or changed module:

- `src/harness/session_runtime.py`
- `src/harness/protocol_adapters.py`
- `src/harness/provider_adapters.py`
- `src/harness/progress.py`

Tasks:

- [x] Classify provider errors into retryable/non-retryable categories.
- [x] Preserve context-overflow compaction path.
- [x] Add provider-specific rate-limit retry hints.
- [x] Add abort propagation into streaming HTTP clients.
- [x] Add partial-response evidence when a stream fails after tokens.
- [x] Add retry schedule events with:
  - attempt
  - delay
  - category
  - retryable
  - no hidden fallback
- [x] Ensure retries never switch providers/models.

Acceptance:

- Retry never changes model/provider.
- Abort stops the stream and records evidence.
- Context overflow can compact and retry only against the same selected model.

Suggested tests:

```bash
pytest -q tests/test_session_runtime.py tests/test_protocol_adapters.py tests/test_provider_adapters.py
```

New tests to add:

- `test_retry_preserves_provider_model_and_variant`
- `test_abort_stops_stream_and_records_event`
- `test_partial_stream_failure_preserves_partial_text_and_error`
- `test_context_overflow_retry_keeps_same_model`

### Phase 14: Documentation And Operator Smoke

Objective: make the feature usable and maintainable.

Docs to update:

- `docs/operator_guide.md`
- `docs/smoke_checklist.md`
- `docs/command_catalog.md`
- this plan

Tasks:

- [x] Document provider concepts.
- [x] Document model refs and aliases.
- [x] Document credential storage and redaction guarantees.
- [x] Document provider connect/disconnect.
- [x] Document model picker behavior.
- [x] Document discovery and hosted approval requirements.
- [x] Document default model resolution and why it is not hidden fallback.
- [x] Add smoke steps:
  - [x] list providers
  - [x] list models
  - [x] validate known model
  - [x] validate unknown model
  - [x] connect env provider
  - [x] select model
  - [x] run session with selected model
  - [x] verify missing credential block
  - [x] refresh local models
  - [x] inspect TUI picker
- [x] Add release-readiness checklist.

Acceptance:

- A new maintainer can configure a local provider and a hosted provider safely.
- Smoke checklist catches credential leakage, hidden fallback, and TUI action drift.

Suggested tests:

```bash
pytest -q tests/test_cli_smoke.py tests/test_docs_phase_3d.py
```

## Milestone Gates

### Milestone A: Runtime Credentials

Includes phases:

- Phase 1
- Phase 2

Required proof:

```bash
pytest -q tests/test_model_catalog.py tests/test_protocol_adapters.py tests/test_session_runtime.py
```

Manual smoke:

```bash
harness providers status --project . --output json
harness models validate codex_cli/gpt-5.5 --project . --output json
harness models validate paid_openai_compatible/gpt-5.3-codex --project . --output json
```

Exit criteria:

- hosted missing credential blocks before network
- local placeholder credentials still work for local-only provider
- no secret in JSON output

### Milestone B: Provider Connect

Includes phases:

- Phase 3
- Phase 4, at least one OAuth provider if feasible

Required proof:

```bash
pytest -q tests/test_local_server.py tests/test_core_service.py tests/test_tui_backend_wiring.py tests/test_model_catalog.py
```

Manual smoke:

```bash
harness providers login paid_openai_compatible --credential-kind env --env-var OPENAI_API_KEY --project .
harness providers accounts paid_openai_compatible --project . --output json
harness providers status --project . --output json
```

Exit criteria:

- account appears configured
- secret value does not appear
- no provider execution starts

### Milestone C: Active Registry

Includes phases:

- Phase 5
- Phase 6

Required proof:

```bash
pytest -q tests/test_model_catalog.py tests/test_local_server.py tests/test_session_runtime.py tests/test_tui_backend_wiring.py
```

Manual smoke:

```bash
harness models list --project . --output json
harness models preferences --project . --output json
harness sessions model <session_id> codex_cli/gpt-5.5 --project . --output json
```

Exit criteria:

- CLI/TUI/server agree on provider and model availability
- default model resolution is audited
- no hidden fallback is introduced

### Milestone D: Provider Production Readiness

Includes phases:

- Phase 7
- Phase 8
- Phase 9

Required proof:

```bash
pytest -q tests/test_protocol_adapters.py tests/test_cross_provider_handoff.py tests/test_model_registry.py tests/test_model_catalog.py
```

Manual smoke:

```bash
harness models protocols --project . --output json
harness models refresh local_openai_compatible --project . --output json
harness models inspect local_openai_compatible/qwen3-coder:30b --project . --output json
```

Exit criteria:

- provider-specific auth and headers are real
- discovery cache is durable and clearly sourced
- custom provider validation is strict

### Milestone E: App Experience

Includes phases:

- Phase 10
- Phase 11

Required proof:

```bash
pytest -q tests/test_tui_backend_wiring.py tests/test_tui_codex_mode.py tests/test_local_server.py tests/test_core_service.py
```

Manual smoke:

```bash
harness tui --project .
```

Operator checks:

- open model picker
- inspect blocked provider
- connect provider
- select model
- favorite model
- set default
- refresh provider
- validate no provider execution during picker navigation

Exit criteria:

- TUI can complete provider/model management without misleading shortcuts
- server API supports the same workflow

### Milestone F: Release Readiness

Includes phases:

- Phase 12
- Phase 13
- Phase 14

Required proof:

```bash
pytest -q
```

Manual smoke:

```bash
harness providers list --project .
harness models list --project .
harness models validate codex_cli/gpt-5.5 --project .
harness models validate missing/gpt-5.5 --project .
harness evals run --suite safety-smoke --project .
```

Exit criteria:

- full test suite passes
- smoke checklist is updated
- docs are current
- no known credential leakage path remains

## Cross-Cutting Test Matrix

Each phase should consider this matrix.

| Area | Required invariant |
| --- | --- |
| Catalog | metadata-only unless explicit discovery |
| TUI | navigation has no provider execution |
| CLI | JSON responses include safety flags |
| Server | routes require local-server auth where applicable |
| Runtime | validates model before provider construction |
| Credentials | secret values never appear in output/events |
| Hosted providers | require approval before network execution |
| Local providers | endpoint must be loopback or approved LAN |
| Defaults | explicit and auditable, never hidden fallback |
| Suggestions | advisory only, never auto-selected |
| Retry | same provider/model/variant only |
| Discovery | explicit, cached, source-labeled |
| Custom providers | strict schema, no raw secrets in config |

## File Ownership Map

| Surface | Primary files |
| --- | --- |
| Provider/model descriptors | `src/harness/model_registry.py`, `src/harness/model_catalog.py` |
| Auth/accounts/credentials | `src/harness/provider_auth.py`, `src/harness/memory/sqlite_store.py`, `src/harness/memory/schema.sql` |
| Discovery | `src/harness/model_discovery.py` |
| Protocol execution | `src/harness/protocol_adapters.py`, `src/harness/provider_content.py`, `src/harness/provider_events.py` |
| Session runtime | `src/harness/session_runtime.py` |
| CLI | `src/harness/cli/main.py` |
| Local server/core service | `src/harness/local_server.py`, `src/harness/core_service.py` |
| TUI/operator context | `src/harness/tui.py`, `src/harness/operator_context.py`, `src/harness/right_pane.py` |
| Docs | `docs/operator_guide.md`, `docs/smoke_checklist.md`, `docs/command_catalog.md`, `docs/plans/model_provider_completion_execution_plan.md` |

## Definition Of Done

The overall model/provider wiring is done when:

- [x] A local provider can be configured, discovered, selected, and executed from the app.
- [x] A hosted API-key provider can be connected, selected, and executed with approval evidence.
- [x] At least one OAuth provider can be connected, refreshed, selected, and executed with redacted evidence.
- [x] Provider/model catalog state is identical across CLI, local server, TUI, and session runtime.
- [x] Runtime credential resolution is centralized.
- [x] Missing credentials block before network access.
- [x] Unknown or disabled providers/models block before provider construction.
- [x] Default model resolution is visible and auditable.
- [x] TUI model/provider management has no advertised dead controls.
- [x] Provider-specific protocol adapters have payload, streaming, usage, tool, and error tests.
- [x] Discovery is explicit, cached, source-labeled, and approval-gated for hosted providers.
- [x] Custom providers are schema-validated and cannot include raw secrets.
- [x] Usage/cost evidence is recorded when available.
- [x] Full test suite passes.
- [x] Operator guide and smoke checklist match implemented behavior.

## Recommended Execution Order

Use this order unless a dependency forces a split:

1. Phase 0: Stabilize tracking docs.
2. Phase 1: Credential resolver and runtime integration.
3. Phase 2: Provider account secret storage.
4. Phase 3: Provider connect/auth API for API-key/env flows.
5. Phase 5: Active provider registry service.
6. Phase 6: Explicit default model resolution.
7. Phase 10: TUI model/provider management pass.
8. Phase 11: Server/API parity.
9. Phase 7: Provider-specific runtime hardening.
10. Phase 8: Discovery and catalog enrichment.
11. Phase 9: Custom provider/plugin hooks.
12. Phase 4: OAuth, if not completed earlier.
13. Phase 12: Usage/cost/policy enforcement.
14. Phase 13: Reliability/retry/abort.
15. Phase 14: Docs and release smoke.

Reasoning:

- Credential resolution must land before provider connect and runtime hardening.
- Active provider state should land before larger TUI and server work so all surfaces share one source of truth.
- OAuth is deliberately isolated because it is easy to overexpand; API-key/env flows should work first.
- Usage/cost and retry are late because they depend on stable provider event shapes.

## Open Decisions

- [ ] Secret storage backend:
  - OS keychain
  - encrypted local file
  - restricted-permission local file
  - hybrid
- [ ] First OAuth provider:
  - OpenAI/Codex-style OAuth
  - Anthropic OAuth if stable
  - GitHub Copilot-style OAuth
- [ ] Catalog enrichment source:
  - generated local metadata file
  - vendored model metadata snapshot
  - explicit online refresh
- [ ] Whether to expose opencode-compatible routes as stable public API or compatibility aliases.
- [ ] Whether command-backed config values are allowed; if allowed, they need explicit approval and command evidence.
- [ ] How much provider plugin execution is allowed during catalog reads.

## Notes For Implementers

- Preserve current safety flags in existing JSON schemas unless deliberately versioning a schema.
- Do not silently make disabled hosted providers executable because credentials are configured.
- Do not resolve a “closest” model into execution. Suggestions are UI/help only.
- Do not refresh OAuth or discovery from metadata-only catalog calls.
- Do not let TUI shortcuts appear before their action path and tests exist.
- Prefer adding a single service API over repeating catalog/provider/account merge logic in CLI, server, TUI, and runtime.
- Add tests before broad refactors when touching credential or runtime paths.
