# Model Provider Flawless Experience Plan

Status: historical complete

Historical note: this plan records the completed model/provider experience baseline. Follow the active remaining-work checklist in `docs/plans/model_provider_completion_execution_plan.md` for the gaps that still remain, especially runtime credential resolution, secret-backed provider accounts, full provider connect/OAuth, shared active-provider state, and provider-specific production hardening.

Follow-on to: `docs/plans/model_provider_registry_integration_plan.md`

Reference sources:

- `.harness/reference-code/opencode`
- `.harness/reference-code/pi`

Goal: take the completed Harness provider/model registry foundation and evolve it into a polished, end-to-end model experience comparable to opencode and pi: real provider connect/auth flows, broad protocol adapters, custom provider/model configuration, durable discovery, cross-provider message normalization, and a fast model picker that remains truthful about authority, credentials, billing, and data boundaries.

## Progress

- [x] Slice 1: picker truthfulness and immediate UX cleanup.
- [x] Slice 2: model preference state for recents, favorites, defaults, and last-used reasoning.
- [x] Slice 3: provider auth/account store and credential status plumbing.
- [x] Slice 4: provider connect/disconnect CLI and TUI action contracts.
- [x] Slice 5: custom provider/model config file and hot reload into the catalog.
- [x] Slice 6: durable discovered model cache promoted into normal catalog reads.
- [x] Slice 7: canonical message/content model for provider adapters.
- [x] Slice 8: true streaming OpenAI-compatible chat adapter.
- [x] Slice 9: OpenAI Responses and OpenAI Codex Responses adapters.
- [x] Slice 10: Anthropic Messages adapter.
- [x] Slice 11: Google Generative adapter.
- [x] Slice 12: Bedrock Converse adapter.
- [x] Slice 13: reasoning, context, cache, usage, and cost normalization.
- [x] Slice 14: cross-provider handoff fixtures and adapter conformance tests.
- [x] Slice 15: model picker management/details polish.
- [x] Slice 16: docs, smoke checklist, and release readiness gates.

## Current Baseline

Harness now has a local-first provider/model registry:

- `src/harness/model_registry.py` defines provider descriptors, model descriptors, aliases, variants, reasoning support, resolution, and fail-closed validation.
- `src/harness/model_catalog.py` projects provider/model descriptors into stable CLI/TUI catalog rows.
- `src/harness/protocol_adapters.py` has a protocol adapter registry.
- `src/harness/session_runtime.py` routes selected session models through resolved descriptors.
- `src/harness/model_discovery.py` supports explicit OpenAI-compatible `/models` refresh with local endpoint validation and hosted approval.
- `src/harness/tui.py` has a model picker that renders concise model rows and uses a cached dashboard snapshot.

The baseline is safe, but incomplete:

- `codex_cli`, `openai_chat`, `openai_responses`, `openai_codex_responses`, `anthropic_messages`, `google_generative`, and `bedrock_converse` protocol adapters execute.
- Provider login/logout intentionally fail closed.
- The TUI advertises provider connect and favorite controls that are not implemented.
- Custom provider/model configuration is not yet a user-facing hot-reload path.
- Discovery returns sparse OpenAI-compatible metadata and does not become a first-class durable catalog overlay.
- Provider streams do not yet normalize real deltas, tool calls, reasoning, multimodal parts, usage, cache data, retry signals, or aborts.
- Cross-provider replay/handoff compatibility is not tested.

## Reference Lessons

### opencode

Useful patterns:

- Provider and model records are distinct and rich.
- Provider records include enabled/auth state, endpoint type, environment variables, custom data, and protocol options.
- Model records include provider id, API id, endpoint, capabilities, options, variants, release time, cost, status, enabled state, context/input/output limits.
- Model selection UX includes provider grouping, search, connect provider, manage models, current model, free/latest tags, and model details.
- Auth is durable and account-based, supporting API-key and OAuth credentials with active account selection.

Do not copy directly:

- Hidden default fallback.
- Broad hosted provider enablement without Harness policy evidence.
- UI flows that imply provider access before Harness has a registered action path.

### pi

Useful patterns:

- Provider runtime dispatch is protocol/API based.
- Built-in API providers are registered lazily.
- Supported APIs include OpenAI completions, OpenAI responses, OpenAI Codex responses, Anthropic messages, Google, Google Vertex, Mistral, and Bedrock Converse.
- Model metadata includes context window, max output tokens, cost, modalities, reasoning support, thinking-level maps, and compatibility options.
- Custom providers/models are configured through a user-editable file and reloaded when opening the model menu.
- OAuth utilities support provider-specific PKCE flows and token refresh.
- Cross-provider handoff tests catch message transformation bugs.

Do not copy directly:

- Ungoverned arbitrary credential command execution.
- Silent clamping or hidden provider fallback without visible validation evidence.
- Provider-specific shortcuts that bypass Harness session/runtime records.

## Non-Negotiable Safety Contract

Every slice must preserve these invariants:

- Listing providers/models is metadata-only unless the operator explicitly requested discovery.
- TUI picker navigation must not call providers, write credentials, run shell, mutate files, or grant authority.
- Provider connect/disconnect must be explicit action paths with durable evidence.
- Unknown providers, unknown models, disabled providers, missing credentials, and unsupported protocols must fail before provider execution.
- Hosted discovery and hosted execution require explicit approval evidence.
- Local providers must validate loopback or explicitly approved LAN endpoints before network access.
- No hidden provider fallback or model fallback.
- Alias resolution, reasoning mapping, protocol routing, and credential source must be visible in validation/runtime evidence.
- Actual credential values must never be printed, persisted in session events, or exposed through TUI/catalog projections.
- Cross-provider transformations must fail visibly when unsupported rather than dropping message parts.

## Target Runtime Shape

```text
operator selects model/provider
  -> catalog resolves provider/model/variant/account/protocol
  -> validation checks enabled state, credentials, policy, endpoint, capabilities
  -> session stores canonical selection and evidence
  -> runtime builds canonical provider request
  -> protocol adapter serializes canonical request to provider-native payload
  -> provider stream is normalized into Harness ProviderEvent records
  -> transcript/session timeline receives canonical message parts, usage, cost, and errors
```

## Slice 1: Picker Truthfulness And Immediate UX Cleanup

Objective: remove misleading controls and finish the low-risk picker polish before deeper backend work.

Tasks:

- Audit `src/harness/tui.py` model dialog footer and key handling.
- Remove `Connect provider ctrl+a` and `Favorite ctrl+f` hints until implemented, or route them to explicit non-side-effecting “not available yet” action evidence.
- Keep the concise row format:
  - model name
  - context window
  - reasoning support
  - disabled/missing credential/blocked reason
- Add display tests for:
  - concise row format
  - no hidden protocol/source clutter in default row
  - disabled provider still visible but clearly blocked
  - no unimplemented shortcut hints
- Ensure opening and moving inside the model dialog uses cached dashboard/model rows.

Acceptance:

- `/model` dialog does not advertise any action that cannot be performed.
- Picker rendering remains metadata-only.
- Focused TUI tests pass.

Suggested tests:

```bash
pytest tests/test_tui_codex_mode.py -q
pytest tests/test_tui_backend_wiring.py -q
```

## Slice 2: Model Preference State

Objective: support real recents/favorites/defaults without side effects.

Tasks:

- Add a persisted model preference record, likely in the existing SQLite store:
  - `raw_model_ref`
  - `provider_id`
  - `model_id`
  - `variant`
  - `last_selected_at`
  - `favorite`
  - `selection_count`
  - `last_reasoning_effort`
  - `source`
  - no credential values
- Add store methods:
  - `record_model_selection(...)`
  - `set_model_favorite(...)`
  - `list_model_preferences(...)`
  - `get_default_model_preference(...)`
- Record preference updates only after explicit session model selection.
- Project preferences into `operator_context.model_catalog`.
- Update picker ordering:
  - current model
  - favorites
  - recent models
  - enabled local providers
  - enabled hosted providers
  - disabled/blocked providers
- Add CLI inspection:
  - `harness models preferences --output json`
  - `harness models favorite <raw_model_ref>`
  - `harness models unfavorite <raw_model_ref>`

Acceptance:

- Favorites and recents are real persisted state.
- Preference writes happen only through explicit model selection or explicit favorite commands.
- The picker can show a favorites/recent section without provider calls.

Suggested tests:

```bash
pytest tests/test_model_catalog.py tests/test_tui_codex_mode.py -q
```

## Slice 3: Provider Auth And Account Store

Objective: replace login/logout stubs with a safe credential/account layer.

Tasks:

- Add `src/harness/provider_auth.py`.
- Add account schema:
  - `account_id`
  - `provider_id`
  - `description`
  - `credential_kind`: `env`, `api_key`, `oauth`, `static_local`, `codex_login`
  - `status`
  - `active`
  - `created_at`
  - `updated_at`
  - `expires_at`
  - redacted metadata only
- Store secrets outside catalog/session projections.
- Use file permissions or OS keychain integration where available; if starting simple, use local encrypted/redacted store with `0600` permissions and document limitations.
- Add credential resolution object that can return:
  - `configured`
  - `missing`
  - `expired`
  - `refresh_required`
  - `not_required`
- Update `ProviderDescriptor.credential` from account/env/static state.
- Add tests proving credentials are redacted from:
  - provider catalog JSON
  - model catalog JSON
  - session events
  - TUI dashboard projections

Acceptance:

- Provider status can distinguish missing credentials from configured credentials without exposing values.
- Existing metadata-only provider/model listing remains side-effect free.
- No provider auth command writes credentials without explicit command invocation.

Suggested tests:

```bash
pytest tests/test_model_registry.py tests/test_model_catalog.py -q
```

## Slice 4: Provider Connect/Disconnect Flows

Objective: make provider onboarding real and inspectable.

Tasks:

- Implement CLI:
  - `harness providers login <provider_id>`
  - `harness providers logout <provider_id>`
  - `harness providers accounts <provider_id>`
  - `harness providers activate-account <provider_id> <account_id>`
- Start with API-key/env/static-local flows.
- Keep OAuth as a capability-gated follow-up within this slice if too large.
- Add action metadata:
  - `credential_written`
  - `credential_removed`
  - `permission_granting`
  - `network_accessed`
  - `credentials_included`
  - `no_hidden_fallback`
- TUI:
  - `Connect provider` should open an explicit provider-connect dialog.
  - Dialog should show available methods and the equivalent CLI command.
  - It must not write credentials until the user confirms.
  - If interactive secret entry is not implemented yet, the TUI should route to the CLI command and avoid pretending it completed.
- Update docs:
  - `docs/operator_guide.md`
  - `docs/command_catalog.md`
  - `docs/smoke_checklist.md`

Acceptance:

- `providers login/logout` no longer fail as stubs for supported credential kinds.
- TUI connect affordance either performs a real explicit action or shows the exact supported command.
- Provider credential status changes after login/logout and is visible in `providers status`.

Suggested tests:

```bash
pytest tests/test_cli.py tests/test_model_catalog.py tests/test_tui_codex_mode.py -q
```

## Slice 5: Custom Provider/Model Config

Objective: give operators a pi-like custom model file while preserving Harness policy.

Tasks:

- Add a user/project config file, for example:
  - `.harness/models.yaml` for project-local configuration
  - optional user-level `~/.harness/models.yaml` only if product policy allows it
- Define schema:
  - provider id
  - display name
  - enabled
  - data boundary
  - base URL/endpoint
  - protocol
  - credential reference
  - headers as redacted/credential references
  - compatibility flags
  - models
  - model overrides
- Model fields:
  - id
  - display name
  - api id
  - protocol override
  - context window
  - max output tokens
  - input/output modalities
  - tool support
  - reasoning support
  - reasoning map
  - cost
  - status
  - variants
- Compatibility fields:
  - `supports_developer_role`
  - `supports_reasoning_effort`
  - `supports_parallel_tool_calls`
  - `tool_call_id_policy`
  - `system_prompt_role`
  - `cache_control`
- Merge custom providers/models into descriptors with `source="custom_config"`.
- Validate:
  - no duplicate provider/model refs unless explicit override
  - local URLs are loopback or approved LAN
  - hosted providers disabled by default unless configured and approved
  - credential references do not expose values
- Add `harness models config validate`.

Acceptance:

- A user can add Ollama, LM Studio, vLLM, SGLang, or a proxy without code changes.
- Opening/listing the model catalog reloads the custom config.
- Invalid config fails with actionable stable errors.

Suggested tests:

```bash
pytest tests/test_model_registry.py tests/test_model_catalog.py -q
```

## Slice 6: Durable Discovery Overlay

Objective: make explicit discovery useful beyond one command output.

Tasks:

- Extend catalog cache schema to distinguish:
  - built-in metadata
  - backend config
  - custom config
  - discovered metadata
  - operator overrides
- `harness models refresh <provider_id>` should persist discovered models as an overlay.
- Catalog reads should include discovered models after refresh.
- Add `harness models refresh --clear-cache <provider_id>` or equivalent.
- Discovery metadata should include:
  - discovery timestamp
  - endpoint
  - network accessed
  - credential included or not
  - approval evidence for hosted refresh
  - model ids
  - raw provider response hash, not raw secrets
- Enrich OpenAI-compatible discovery where possible:
  - model owner/family if provided
  - created timestamp if provided
  - fallback context window from provider defaults
- Keep discovery protocol-specific and explicit.

Acceptance:

- Refreshed local models appear in `harness models list`, the TUI picker, and `models inspect`.
- Hosted refresh remains blocked without explicit approval.
- Cache clear removes discovered models without touching built-ins/custom config.

Suggested tests:

```bash
pytest tests/test_model_registry.py tests/test_model_catalog.py tests/test_tui_backend_wiring.py -q
```

## Slice 7: Canonical Message And Content Model

Objective: create the stable internal message shape needed for multiple protocol adapters.

Tasks:

- Add canonical provider request/response content types:
  - text
  - reasoning/thinking
  - image input
  - tool call
  - tool result
  - refusal/error
  - provider metadata
- Preserve signatures/opaque ids where needed:
  - OpenAI reasoning item ids
  - Anthropic thinking signatures
  - Google thought signatures
  - provider-native response ids
- Add transformer interfaces:
  - `CanonicalMessage -> provider payload`
  - `provider stream event -> CanonicalMessagePart/ProviderEvent`
- Add explicit unsupported-part errors.
- Ensure existing session transcript rendering can ignore unknown future parts safely while preserving raw event evidence.

Acceptance:

- Existing Codex and local OpenAI flows still pass.
- New adapters can share canonical request/response structures.
- Unsupported message parts fail visibly instead of being dropped.

Suggested tests:

```bash
pytest tests/test_protocol_adapters.py tests/test_session_runtime.py -q
```

## Slice 8: True Streaming OpenAI-Compatible Chat

Objective: upgrade `openai_chat` from one-shot completion to real stream normalization.

Tasks:

- Extend `LocalOpenAICompatibleBackend` or introduce a lower-level OpenAI-compatible client for streaming.
- Support `/chat/completions` with `stream=true`.
- Parse SSE chunks:
  - text deltas
  - tool call deltas if provider emits them
  - finish reason
  - usage if provided
  - provider errors
- Map request options:
  - temperature
  - max tokens
  - reasoning effort only when supported
  - timeout
  - headers
- Respect compatibility flags from provider/model descriptors.
- Add abort handling and visible timeout errors.

Acceptance:

- Local OpenAI-compatible providers stream incremental deltas into Harness provider events.
- Non-streaming fallback is explicit and tested only when configured.
- Unsupported tool/reasoning fields are omitted or blocked according to compatibility flags.

Suggested tests:

```bash
pytest tests/test_protocol_adapters.py tests/test_core_service.py -q
```

## Slice 9: OpenAI Responses And OpenAI Codex Responses

Status: completed.

Objective: add first-class Responses API execution rather than treating modern OpenAI/Codex models as chat completions.

Tasks:

- Add protocol literals if needed:
  - `openai_responses`
  - `openai_codex_responses`
- Implement adapters:
  - request serialization
  - streaming parser
  - response id handling
  - reasoning item handling
  - tool call handling
  - usage/cost handling
  - cache metadata where available
- Add credential support:
  - API key
  - OAuth Codex account if implemented
  - explicit missing/expired statuses
- Add built-in model metadata for supported OpenAI/Codex models only when provider is disabled or credentials missing by default unless configured.
- Add tests with recorded or fake streams.

Acceptance:

- `harness models protocols` includes registered Responses adapters.
- A configured local/fake Responses provider can execute through `session_runtime`.
- Reasoning and tool calls are represented in canonical events.

Suggested tests:

```bash
pytest tests/test_protocol_adapters.py tests/test_session_runtime.py tests/test_model_registry.py -q
```

## Slice 10: Anthropic Messages Adapter

Status: completed.

Objective: support Anthropic-style message streaming with thinking/tool normalization.

Tasks:

- Implement `anthropic_messages` protocol adapter.
- Serialize canonical messages to Anthropic payload.
- Parse:
  - message start
  - content block start/stop
  - text deltas
  - thinking deltas
  - tool use
  - tool result compatibility
  - usage
  - errors
- Support cache control only through explicit descriptor options.
- Normalize tool call ids and names.
- Add built-in/custom metadata support for Anthropic models.

Acceptance:

- Fake/recorded Anthropic stream drives text, thinking, tool, usage, and error events.
- Unsupported cross-provider parts fail with a clear blocked reason.

Suggested tests:

```bash
pytest tests/test_protocol_adapters.py tests/test_session_runtime.py -q
```

## Slice 11: Google Generative Adapter

Status: completed.

Objective: support Google/Gemini-compatible generation with thought signature preservation.

Tasks:

- Implement `google_generative` adapter.
- Serialize canonical text/image/tool messages.
- Preserve and replay thought signatures when present.
- Normalize Google tool call/result shapes.
- Support model-level thinking controls when configured.
- Add compatibility flags for models/providers that differ from standard Gemini behavior.

Acceptance:

- Fake/recorded Google stream produces canonical text/tool/reasoning events.
- Thought signatures are retained in provider metadata without leaking secrets.

Suggested tests:

```bash
pytest tests/test_protocol_adapters.py tests/test_session_runtime.py -q
```

## Slice 12: Bedrock Converse Adapter

Status: completed.

Objective: support Bedrock Converse without weakening credential or endpoint policy.

Tasks:

- Implement `bedrock_converse` adapter.
- Add AWS credential descriptor support:
  - env/profile metadata only
  - no secret projection
- Serialize canonical messages to Converse format.
- Parse Bedrock stream events.
- Normalize usage and tool calls.
- Add region/model id config fields.
- Require explicit hosted approval/policy where appropriate.

Acceptance:

- Bedrock models can be described and validated without credentials leaking.
- Fake/recorded Converse streams normalize to Harness events.

Suggested tests:

```bash
pytest tests/test_protocol_adapters.py tests/test_model_catalog.py -q
```

## Slice 13: Reasoning, Context, Cache, Usage, And Cost Normalization

Objective: make model metadata operational, not decorative.

Tasks:

- Add validation for:
  - context limit
  - output limit
  - requested reasoning level
  - unsupported modalities
  - tool support
- Add provider/model reasoning policy:
  - `none`
  - `effort`
  - `tokens`
  - `native`
  - `unknown`
- Decide per-provider behavior:
  - block unsupported reasoning by default
  - allow explicit mapped values from descriptor
  - record exact mapping in events
- Add usage model:
  - input tokens
  - output tokens
  - cache read
  - cache write
  - total tokens
  - estimated cost
  - provider-reported cost if available
- Add cache-retention options where supported.
- Surface usage/cost in session/runtime evidence and optional TUI details.

Acceptance:

- Runtime evidence shows requested vs resolved reasoning.
- Cost/usage is captured when providers report it and estimated when metadata allows.
- Unsupported modality/reasoning/tool requests block before provider execution.

Suggested tests:

```bash
pytest tests/test_model_registry.py tests/test_protocol_adapters.py tests/test_session_runtime.py -q
```

Status: completed.

Implementation notes:

- Model resolution now rejects unsupported context, output, input modality, tool, and reasoning requests before provider execution.
- Reasoning support includes `native` alongside `none`, `effort`, `tokens`, and `unknown`; explicit descriptor maps remain authoritative.
- Anthropic cache retention can be requested through resolved model options and is serialized as provider-native ephemeral cache control.
- Provider usage events now include normalized input/output/cache/total token counts across OpenAI chat, OpenAI Responses, Anthropic Messages, Google Generative, and Bedrock Converse.
- Runtime evidence stores estimated cost when model metadata includes rates and preserves provider-reported cost when present.

## Slice 14: Cross-Provider Handoff Tests

Objective: prevent provider-specific message shapes from corrupting future model switches.

Tasks:

- Add adapter conformance fixtures:
  - simple text context
  - tool call + tool result context
  - reasoning/thinking context
  - image input context
  - provider-native opaque signature context
- For each registered adapter:
  - serialize canonical messages
  - parse fake provider stream
  - replay generated canonical context into another adapter serializer
- Add matrix tests:
  - OpenAI chat -> Anthropic
  - Anthropic -> OpenAI responses
  - OpenAI responses -> Google
  - Google -> OpenAI chat
  - unsupported parts produce explicit blocked reason
- Avoid live provider calls in default test suite. Keep live tests opt-in.

Acceptance:

- Cross-provider handoff tests run offline by default.
- Failures identify the adapter and unsupported canonical part.
- No transformer silently drops reasoning, tool ids, or signatures.

Suggested tests:

```bash
pytest tests/test_protocol_adapters.py tests/test_cross_provider_handoff.py -q
```

Status: completed.

Implementation notes:

- Added offline cross-provider handoff coverage for OpenAI chat -> Anthropic, Anthropic -> OpenAI Responses, OpenAI Responses -> Google, and Google -> OpenAI chat.
- Added preservation checks for provider-native reasoning signatures and tool call IDs when replaying canonical context into another provider serializer.
- Unsupported handoff parts now have a regression test that asserts the failing adapter and canonical part name are visible before any target provider stream is opened.
- Canonical event mapping now keeps reasoning signatures on canonical parts and represents completed provider tool calls as canonical `tool_call` parts for downstream replay.

## Slice 15: Picker Management And Details Polish

Objective: reach the opencode/pi level of model selection usability in Harness’s terminal style.

Tasks:

- Add model details view from picker:
  - provider
  - model id
  - protocol
  - context window
  - max output
  - reasoning support
  - modalities
  - tool support
  - credential status
  - data boundary
  - source
  - cost
  - blocked reasons
  - inspect command
- Add real keyboard actions:
  - toggle favorite
  - provider details
  - model inspect
  - connect provider if implemented
  - refresh provider if explicit and approved
- Add width-aware truncation and stable columns.
- Add fuzzy matching across:
  - model id
  - display name
  - provider id
  - provider display name
  - aliases
- Add virtualized rendering for large catalogs.
- Add tests for:
  - narrow terminal
  - long model names
  - many providers/models
  - disabled providers
  - missing credentials
  - favorites/recents ordering

Acceptance:

- The model picker is fast with large catalogs.
- The picker tells the operator why a model is selectable or blocked.
- Every advertised shortcut has a real implementation and tests.

Suggested tests:

```bash
pytest tests/test_tui_codex_mode.py tests/test_tui_backend_wiring.py -q
```

Status: completed.

Implementation notes:

- Model catalog projections now include provider display names, max output limits, and sanitized cost metadata for picker details.
- The picker renders stable concise rows plus a selected-model details block with provider, model, protocol, context, max output, reasoning, modalities, tool support, credentials, data boundary, source, cost, blocked reasons, and inspect command.
- Search now matches model id, raw/canonical refs, aliases, provider id, and provider display name with multi-term fuzzy matching.
- Large catalogs use a bounded visible window around the selected row, keeping movement and rendering fast while showing the current match range.
- Added renderer coverage for long names, narrow widths, fuzzy search, virtualized catalogs, blocked models, and detail content.

## Slice 16: Docs, Smoke Checklist, And Release Gates

Objective: make the feature set maintainable and verifiable.

Tasks:

- Update:
  - `docs/operator_guide.md`
  - `docs/command_catalog.md`
  - `docs/smoke_checklist.md`
  - `docs/plans/model_provider_registry_integration_plan.md` with a pointer to this follow-on plan
- Add smoke scripts or checklist entries for:
  - list providers/models
  - validate known model
  - validate blocked hosted model
  - add custom local model
  - refresh local provider
  - connect/disconnect provider
  - select favorite/recent model
  - execute through fake protocol adapter
  - inspect usage/cost evidence
- Add release gate:
  - no secret leakage in JSON projections
  - no hidden provider fallback
  - no picker provider calls
  - all registered protocols have adapter conformance tests
  - unregistered protocols block before execution

Acceptance:

- Operator docs match implemented commands.
- Smoke checklist can be followed manually.
- Focused and broad tests pass.

Suggested tests:

```bash
pytest tests/test_model_registry.py tests/test_model_catalog.py tests/test_protocol_adapters.py tests/test_session_runtime.py tests/test_tui_codex_mode.py tests/test_tui_backend_wiring.py -q
```

Status: completed.

Implementation notes:

- Updated operator docs, command catalog, and smoke checklist for provider/model commands, custom local config, preferences, refresh, account lifecycle, adapter conformance, usage/cost evidence, picker details, and release gates.
- Added a pointer from the registry integration plan to this follow-on plan.
- Added a non-preflight `doctor --release` model-provider gate covering metadata-only catalog projections, no secret values, no picker provider calls, no hidden fallback, registered protocol coverage, and unregistered-protocol fail-closed behavior.
- Verified the documented focused suite and a CLI smoke path for protocols, model validation, and `doctor --release`.

## Implementation Notes

### Prefer Small, Verifiable Steps

Do not add all adapters before the canonical message model exists. The stable sequence should be:

1. Finish picker truthfulness.
2. Add preference state.
3. Add credential/account plumbing.
4. Add custom config and durable discovery.
5. Add canonical message model.
6. Add adapters one protocol at a time.
7. Add cross-provider handoff tests.
8. Polish picker details and management.

### Keep Adapters Policy-Neutral

Protocol adapters should transform and execute provider requests, but they should not decide whether a provider/model is allowed. Policy checks belong before adapter dispatch:

- model resolution
- provider enabled state
- credential status
- hosted/local boundary
- approval gates
- reasoning/modality/tool support

### Keep Catalog Reads Passive

These commands and TUI projections must stay passive:

- `harness providers list`
- `harness providers status`
- `harness models list`
- `harness models inspect`
- `harness models protocols`
- TUI dashboard model pane
- TUI model picker open/filter/move

Only explicit commands such as `harness models refresh`, `harness providers login`, and confirmed provider-connect actions may write state or access network.

### Use Fake Providers First

Every adapter should land with fake/recorded stream tests before any live provider path is relied on. Live provider tests should be opt-in and skipped by default unless credentials and an explicit environment flag are present.

### Preserve Script Contracts

The text/TUI output may improve, but existing JSON schemas should remain compatible. If a schema version must change, add compatibility tests and update docs in the same slice.

## Definition Of Done

Harness reaches the target experience when:

- A new operator can open the model picker, see usable models, understand blocked models, connect a provider, mark favorites, and switch sessions without lag.
- A local OpenAI-compatible provider can be added without code changes, refreshed explicitly, selected from the picker, and used with true streaming.
- Hosted providers remain blocked until explicitly configured, credentialed, and approved.
- At least OpenAI Responses, Anthropic Messages, Google Generative, and Bedrock Converse have registered adapters or explicit unsupported status.
- Provider events preserve text, tool calls, tool results, reasoning, usage, cost, errors, and aborts in canonical Harness evidence.
- Cross-provider handoff tests prevent silent loss of reasoning/tool/signature state.
- Provider/model catalog operations never leak credentials or grant hidden fallback.
- Docs and smoke checks describe the actual implemented behavior.
