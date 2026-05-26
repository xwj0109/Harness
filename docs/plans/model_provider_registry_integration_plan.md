# Model Provider Registry Integration Plan

Status: historical complete

Historical note: this plan records the completed registry-foundation work. Follow the active remaining-work checklist in `docs/plans/model_provider_completion_execution_plan.md` for credential resolution, provider connect/OAuth, active-provider state, runtime hardening, and release gates.

Follow-on: `docs/plans/model_provider_flawless_experience_plan.md` completes the next phase: provider account plumbing, custom providers/models, durable discovery, expanded protocol adapters, capability validation, usage/cost normalization, cross-provider handoff tests, and the polished model picker.

Reference sources:

- `.harness/reference-code/opencode`
- `.harness/reference-code/pi`

Goal: evolve Harness model wiring from backend-configured single-model execution into a first-class provider/model registry with protocol-based runtime dispatch, while preserving Harness safety semantics: explicit model refs, fail-closed validation, no hidden provider/model fallback, visible billing/data boundaries, and durable evidence for every execution path.

## Progress

- [x] Slice 1: descriptor schemas and registry skeleton.
- [x] Slice 2: catalog projection compatibility over descriptors.
- [x] Slice 3: model-selection validation resolves executable descriptors.
- [x] Slice 4: protocol adapter registry and current backend wrappers.
- [x] Slice 5: session runtime routes through resolved model descriptors.
- [x] Slice 6: built-in provider/model metadata.
- [x] Slice 7: aliases and canonical refs.
- [x] Slice 8: reasoning, variants, and provider option merge.
- [x] Slice 9: local and hosted discovery.
- [x] Slice 10: TUI/CLI/operator docs and smoke coverage.

## Current State

Harness currently has model/provider concepts, but they are thin projections over backend config.

- `src/harness/config.py` defines default backends:
  - `codex_cli` as an external-agent backend using Codex subscription auth.
  - `local_openai_compatible` as a local native model backend pointed at `http://localhost:11434/v1`.
  - `paid_openai_compatible` as a disabled hosted OpenAI-compatible backend.
- `src/harness/model_catalog.py` lists providers from `config.backends` and lists models from each backend's `settings["model"]`.
- `raw_model_ref` values use `provider/model` strings such as `codex_cli/gpt-5.5`.
- `validate_model_selection()` fails closed for missing refs, provider-less refs, unknown providers, disabled providers, and unknown models.
- `LocalOpenAICompatibleBackend.complete()` always posts to `/chat/completions` using `self.config.settings["model"]`.
- Codex execution passes the configured model to `codex exec --model`.
- `ModelCatalogEntry` contains useful safety/evidence fields, but it is not yet an executable model descriptor.
- `ProviderAdapter` normalizes provider events, but runtime does not resolve a rich model object before choosing the adapter.

This is safe and explicit, but it is not equivalent to opencode or pi.

## Reference Lessons

### opencode

opencode has a real catalog service with mutable provider and model records.

Useful ideas to port:

- Provider records and model records are separate.
- Models carry provider id, API id, endpoint, options, capabilities, variants, release time, cost, status, and limits.
- Model resolution merges provider-level endpoint/options with model-level endpoint/options.
- Availability and defaults are catalog functions, not hardcoded backend settings.
- Plugins can enrich or alter provider/model records.

Do not port directly:

- TypeScript Effect service structure.
- AI SDK-specific assumptions as the only transport layer.
- Automatic default fallback unless Harness surfaces it as an explicit, auditable selection.

### pi

pi has a generated model registry and clean runtime dispatch by model API/protocol.

Useful ideas to port:

- A model object carries its protocol/API.
- Runtime dispatch is `model.api -> registered provider implementation`.
- Provider implementations can be lazily registered.
- Models include context window, max output tokens, cost, input modalities, reasoning support, thinking-level maps, and compatibility overrides.

Do not port directly:

- Ungoverned direct dispatch from model to provider.
- Hidden router/provider fallback behavior.
- Broad provider enablement without Harness policy checks.

## Target Architecture

Harness should use an opencode-style provider/model catalog plus pi-style protocol dispatch.

The runtime path should be:

```text
raw_model_ref
  -> parse provider/model/variant
  -> validate against local catalog and policy
  -> resolve executable ModelDescriptor
  -> resolve ProviderDescriptor
  -> resolve ProtocolAdapter by model.protocol
  -> execute provider-native request
  -> normalize provider-native stream into Harness ProviderEvent
  -> persist events, messages, evidence, and validation metadata
```

The catalog remains local-first and metadata-only unless the operator explicitly requests refresh/discovery. Listing models must not login, refresh credentials, call hosted providers, write credentials, or start execution.

## Non-Negotiable Safety Contract

Harness remains the authority layer.

- Unknown provider refs must fail before provider execution.
- Unknown model refs must fail before provider execution.
- Disabled providers must fail before provider execution.
- Missing credentials must fail before provider execution unless the provider does not require credentials.
- Hosted paid providers require explicit policy approval.
- Local-only providers must validate that endpoints are loopback or explicitly approved LAN endpoints.
- Provider/model fallback is forbidden unless represented as an explicit catalog entry and selected by the operator.
- Alias resolution must be visible in validation output and session events.
- Reasoning-effort coercion must be visible. Default behavior should block unsupported reasoning levels instead of silently clamping.
- Catalog projections must preserve `metadata_only: true`, `provider_execution_started: false`, `model_execution_started: false`, `network_accessed: false`, `credentials_included: false`, and `no_hidden_fallback: true`.
- Runtime execution events must preserve `hidden_provider_fallback: false` and `hidden_model_fallback: false`.
- TUI/CLI model selection remains a visible operator action, not an ambient side effect.

## Terminology

- Provider: identity and policy boundary for a model source, for example `openai`, `anthropic`, `local_openai`, `codex_cli`, or `openrouter`.
- Backend: existing Harness execution/config bucket. During migration, backend ids may also act as provider ids for compatibility.
- Protocol: provider API transport shape, for example `openai_chat`, `openai_responses`, `anthropic_messages`, `bedrock_converse`, `google_generative`, or `codex_cli`.
- Model descriptor: executable metadata for a model.
- Catalog projection: stable existing CLI/TUI output derived from descriptors.
- Alias: visible alternate ref that resolves to a canonical provider/model.

## Schema Targets

Add new descriptor models in a focused module, likely `src/harness/model_registry.py` or `src/harness/model_descriptors.py`.

### ProviderDescriptor

```python
class ProviderDescriptor(BaseModel):
    schema_version: str = "harness.provider_descriptor/v1"
    provider_id: str
    display_name: str
    backend_id: str | None = None
    enabled: bool = True
    endpoint: str | None = None
    protocol_defaults: dict[str, Any] = Field(default_factory=dict)
    credential: CredentialDescriptor | None = None
    metadata: BackendMetadata
    capabilities: BackendCapabilities
    constraints: list[str] = Field(default_factory=list)
    policy_boundary: dict[str, Any] = Field(default_factory=dict)
    source: str = "config"
    metadata_only: bool = True
    provider_execution_started: bool = False
    model_execution_started: bool = False
    network_accessed: bool = False
    credentials_included: bool = False
    hidden_provider_fallback: bool = False
    hidden_model_fallback: bool = False
    no_hidden_fallback: bool = True
```

### CredentialDescriptor

```python
class CredentialDescriptor(BaseModel):
    schema_version: str = "harness.credential_descriptor/v1"
    kind: Literal["none", "env", "static_local", "codex_login"]
    env_var: str | None = None
    status: ProviderCredentialStatus = ProviderCredentialStatus.UNKNOWN
    credential_write_supported: bool = False
    credential_written: bool = False
```

Rules:

- Never include actual credential values.
- `static_local` is only for local placeholder values such as Ollama's `api_key: ollama`.
- Credential refresh/login is out of scope for metadata listing.

### ModelDescriptor

```python
class ModelDescriptor(BaseModel):
    schema_version: str = "harness.model_descriptor/v1"
    provider_id: str
    model_id: str
    raw_model_ref: str
    api_id: str | None = None
    protocol: Literal[
        "codex_cli",
        "openai_chat",
        "openai_responses",
        "anthropic_messages",
        "bedrock_converse",
        "google_generative",
    ]
    backend_id: str | None = None
    endpoint: str | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)
    model_options: dict[str, Any] = Field(default_factory=dict)
    variants: dict[str, ModelVariantDescriptor] = Field(default_factory=dict)
    context_limit: int | None = None
    max_output_tokens: int | None = None
    input_modalities: list[str] = Field(default_factory=lambda: ["text"])
    output_modalities: list[str] = Field(default_factory=lambda: ["text"])
    tool_support: bool = False
    reasoning_support: Literal["none", "effort", "tokens", "unknown"] = "unknown"
    reasoning_effort_map: dict[str, str | None] = Field(default_factory=dict)
    cost: dict[str, Any] | None = None
    status: Literal["active", "beta", "deprecated", "disabled"] = "active"
    source: str = "config"
    policy_boundary: dict[str, Any] = Field(default_factory=dict)
    safety_notes: list[str] = Field(default_factory=list)
```

### ModelVariantDescriptor

```python
class ModelVariantDescriptor(BaseModel):
    schema_version: str = "harness.model_variant_descriptor/v1"
    variant_id: str
    display_name: str | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)
    model_options: dict[str, Any] = Field(default_factory=dict)
    reasoning_effort_map: dict[str, str | None] = Field(default_factory=dict)
    safety_notes: list[str] = Field(default_factory=list)
```

### ResolvedModelSelection

```python
class ResolvedModelSelection(BaseModel):
    schema_version: str = "harness.resolved_model_selection/v1"
    raw_model_ref: str
    canonical_model_ref: str
    provider_id: str
    model_id: str
    variant: str | None = None
    alias_used: str | None = None
    provider: ProviderDescriptor
    model: ModelDescriptor
    resolved_endpoint: str | None = None
    resolved_provider_options: dict[str, Any] = Field(default_factory=dict)
    resolved_model_options: dict[str, Any] = Field(default_factory=dict)
    policy_boundary: dict[str, Any] = Field(default_factory=dict)
```

## Protocol Adapter Contract

Add a protocol adapter layer that is independent of UI and CLI code.

```python
class ProtocolAdapter(Protocol):
    protocol: str

    def stream(
        self,
        provider: ProviderDescriptor,
        model: ModelDescriptor,
        request: ProviderRequest,
    ) -> Iterator[ProviderEvent]:
        ...
```

```python
class ProtocolAdapterRegistry:
    def register(self, adapter: ProtocolAdapter) -> None: ...
    def get(self, protocol: str) -> ProtocolAdapter: ...
    def has(self, protocol: str) -> bool: ...
    def list_protocols(self) -> list[str]: ...
```

Initial adapters:

- `codex_cli`: wraps current `CodexCliBackend`.
- `openai_chat`: wraps current `LocalOpenAICompatibleBackend` behavior first, then becomes a general OpenAI-compatible chat-completions adapter.

Later adapters:

- `openai_responses`
- `anthropic_messages`
- `bedrock_converse`
- `google_generative`

Adapter rules:

- Adapters receive already-validated descriptors.
- Adapters must not choose a different model if execution fails.
- Adapters must not silently switch endpoints.
- Adapters must produce normalized `ProviderEvent` entries.
- Provider-native payloads persisted in events must be sanitized.
- Missing adapter must fail as `protocol_adapter_missing` before provider execution.

## Implementation Slices

### Slice 1: Descriptor Schemas And Registry Skeleton

Add descriptor models and a registry builder that reads current Harness config.

Suggested files:

- `src/harness/model_registry.py`
- `tests/test_model_registry.py`

Required functions:

```python
def build_provider_descriptors(config: HarnessConfig) -> list[ProviderDescriptor]: ...
def build_model_descriptors(config: HarnessConfig, registry: SpecRegistry | None = None) -> list[ModelDescriptor]: ...
def parse_model_ref(raw_model_ref: str | None) -> ParsedModelRef: ...
def resolve_model_selection(config: HarnessConfig, raw_model_ref: str, registry: SpecRegistry | None = None) -> ResolvedModelSelection: ...
```

Initial descriptor mapping:

| Existing ref | Protocol | Notes |
| --- | --- | --- |
| `codex_cli/gpt-5.5` | `codex_cli` | Keep subscription/billing metadata. |
| `local_openai_compatible/qwen3-coder:30b` | `openai_chat` | Local-only endpoint validation remains mandatory. |
| `paid_openai_compatible/gpt-5.3-codex` | `openai_chat` initially | Keep disabled by default. Move to `openai_responses` only when an adapter exists. |

Acceptance:

- Descriptors are generated from `default_config()`.
- No descriptor construction performs network, login, credential refresh, or provider execution.
- Credential values are never included in model/provider dumps.
- Tests prove local, Codex, and paid providers all produce descriptors.
- Tests prove descriptor policy/evidence flags default to metadata-only.

### Slice 2: Catalog Projection Compatibility

Make existing catalog functions project from descriptors instead of separately interpreting backend config.

Targets:

- `list_provider_catalog()`
- `list_model_catalog()`
- `ProviderCatalogEntry`
- `ModelCatalogEntry`

Rules:

- Preserve current schema versions.
- Preserve current fields used by TUI, CLI, and tests.
- Add optional fields only if tests and JSON clients remain compatible.
- Keep current `raw_model_ref` values stable.
- Continue listing profile-derived rows if existing UI relies on them, but mark their source as profile aliases.

Acceptance:

- Existing model catalog tests pass.
- Existing TUI model picker tests pass.
- `harness models list` output is unchanged unless intentionally extended.
- Catalog projection still says `metadata_only: true` and execution flags are false.

### Slice 3: Validation Resolves Executable Descriptors

Extend validation so a successful result has a resolved descriptor internally and eventually in JSON output.

Targets:

- `validate_model_selection()`
- session creation/update validation paths
- foreground prompt/native-agent validation paths
- local server model validation route
- TUI model activation path

New blocked reasons:

- `protocol_unknown`
- `protocol_adapter_missing`
- `credential_missing`
- `hosted_provider_not_approved`
- `local_endpoint_not_local`
- `variant_unknown`
- `reasoning_effort_unsupported`

Rules:

- Unknown models still persist in session metadata when current behavior requires it, but execution must not start.
- Validation events must include `provider_execution_started: false`.
- Validation must distinguish provider known/model unknown from provider unknown.
- If a protocol adapter is missing, validation blocks before constructing the backend/adapter.

Acceptance:

- Existing fail-closed unknown-model tests still pass.
- New tests prove missing protocol adapter blocks execution.
- New tests prove hosted disabled provider blocks execution.
- New tests prove local non-local endpoint blocks execution.
- Session events record canonical model resolution when successful.

### Slice 4: Protocol Adapter Registry

Introduce protocol dispatch without changing user-visible behavior.

Suggested files:

- `src/harness/protocol_adapters.py`
- `tests/test_protocol_adapters.py`

Initial implementation:

- Register `CodexCliProtocolAdapter`.
- Register `OpenAIChatProtocolAdapter`.
- Keep existing `ProviderAdapter` event normalization where possible.
- Bridge old backends inside the adapters to reduce risk.

Acceptance:

- Registry returns adapters by protocol.
- Unknown protocol fails closed.
- `openai_chat` adapter can call a fake OpenAI-compatible HTTP client in tests.
- `codex_cli` adapter can call a fake Codex backend in tests.
- Adapter errors are classified into existing provider error categories.

### Slice 5: Runtime Uses Resolved Model Selection

Route session runtime execution through descriptor resolution.

Targets:

- `src/harness/session_runtime.py`
- `src/harness/provider_adapters.py`
- `src/harness/core_service.py`
- foreground/direct run paths in `src/harness/cli/main.py`

Desired behavior:

1. Prompt submission determines `model_ref` from request or session.
2. Runtime validates and resolves the model.
3. Runtime records model validation event.
4. Runtime resolves protocol adapter.
5. Runtime starts provider execution only after validation succeeds.
6. Runtime persists provider/model started events with canonical model metadata.

Acceptance:

- Unknown model tests prove no adapter/backend is constructed.
- Successful runtime tests prove the selected descriptor is the one executed.
- Provider events include `model_ref`, `provider_id`, `model_id`, `protocol`, and `canonical_model_ref`.
- No hidden fallback flags remain false.

### Slice 6: Built-In Provider And Model Metadata

Add a small curated metadata layer.

Preferred location:

- `src/harness/builtin_specs/providers.yaml`
- `src/harness/builtin_specs/models.yaml`

Alternative if YAML becomes awkward:

- `src/harness/builtin_model_data.py`

Start with minimal entries:

- `codex_cli/gpt-5.5`
- `local_openai_compatible/qwen3-coder:30b`
- `paid_openai_compatible/gpt-5.3-codex`

Then add canonical future entries:

- `openai/gpt-5.3-codex`
- `openai/gpt-5.5`
- `anthropic/claude-sonnet-4`
- `anthropic/claude-opus-4`
- `openrouter/<explicitly configured model>`

Metadata to include where known:

- protocol
- context limit
- max output tokens
- modalities
- tool support
- reasoning support
- reasoning effort map
- status
- cost if available and stable enough

Rules:

- If metadata is unknown, use `unknown`/`None`, not guessed values.
- Do not fetch hosted model metadata during normal catalog listing.
- Keep model metadata source-labeled.

Acceptance:

- Built-in data loads deterministically.
- Invalid YAML/schema fails tests.
- Catalog output includes richer fields where present.

### Slice 7: Canonical Refs And Aliases

Add alias support after descriptor execution is stable.

Examples:

- `local/qwen3-coder` -> `local_openai_compatible/qwen3-coder:30b`
- `openai/gpt-5.3-codex` -> `paid_openai_compatible/gpt-5.3-codex` during migration
- `codex/gpt-5.5` -> `codex_cli/gpt-5.5` only if product naming wants this

Rules:

- Alias entries are visible in catalog output.
- Validation output includes both `raw_model_ref` and `canonical_model_ref`.
- Session events include `alias_used` when applicable.
- Alias must not bypass disabled-provider checks.
- Alias must not turn a local-only selection into hosted execution.

Acceptance:

- Alias selection succeeds only when canonical target succeeds.
- Alias to unknown target fails closed.
- Alias to disabled provider fails closed.
- TUI model picker can show canonical and alias refs clearly.

### Slice 8: Reasoning, Variants, And Option Merge

Implement opencode-style option merge and pi-style reasoning support.

Resolution order:

1. Provider defaults.
2. Model options.
3. Variant options.
4. Request-scoped options allowed by policy.

Rules:

- Later layers may override earlier layers only for allowed option keys.
- Credentials are never represented as ordinary options.
- Unknown variant blocks with `variant_unknown`.
- Unsupported reasoning effort blocks by default.
- If a future policy allows clamping, record `requested_reasoning_effort`, `resolved_reasoning_effort`, and `reasoning_resolution: clamped`.

Acceptance:

- Tests prove provider/model/variant merge order.
- Tests prove disallowed option keys are rejected or ignored visibly.
- Tests prove unknown variant blocks.
- Tests prove unsupported reasoning blocks before execution.

### Slice 9: Discovery And Refresh

Discovery is optional and explicit.

Local discovery:

```bash
harness models refresh local_openai_compatible --project . --output json
```

Hosted discovery:

```bash
harness models refresh openai --project . --output json
```

Rules:

- Normal `models list` is metadata-only and does not call providers.
- Local discovery may call a local endpoint after local URL validation.
- Hosted discovery requires explicit hosted-provider/network policy approval.
- Discovery records source, timestamp, endpoint, redaction state, and whether network was accessed.
- Discovery never writes credentials.
- Discovered models are marked `source=discovered`.
- Discovered hosted models are not auto-enabled unless policy allows it.

Acceptance:

- Local fake `/models` refresh updates a cache under `.harness` or test store.
- Hosted refresh without approval fails closed.
- Refresh output has `network_accessed: true` only when an actual provider request occurred.
- Catalog can include discovered entries with clear source labels.

### Slice 10: CLI, TUI, Docs, Smoke

Update operator-facing surfaces after the internal path is stable.

CLI targets:

```bash
harness models list --project . --output json
harness models providers --project . --output json
harness models validate <provider/model> --project . --output json
harness models inspect <provider/model> --project . --output json
harness models protocols --project . --output json
```

TUI targets:

- Model picker shows provider, model, protocol, status, source, context limit, reasoning support, and data boundary.
- Active session display shows raw ref and canonical ref when an alias is used.
- Disabled/blocked models show reason without being selectable for execution.
- Hosted/paid models show required approval boundary.
- Local models show endpoint and local-only validation status without printing secrets.

Docs:

- Update `docs/operator_guide.md`.
- Update `docs/command_catalog.md` if command output changes.
- Add smoke steps to `docs/smoke_checklist.md`.

Acceptance:

- CLI smoke tests cover list, validate, inspect, and blocked execution.
- TUI tests cover model picker projection and activation.
- Docs mention no hidden fallback, explicit hosted provider approval, and local endpoint safety.

## Compatibility Strategy

Keep existing refs working until a major migration explicitly removes them.

Stable existing refs:

- `codex_cli/gpt-5.5`
- `local_openai_compatible/qwen3-coder:30b`
- `paid_openai_compatible/gpt-5.3-codex`

Do not rename these in the first implementation.

Add canonical names later as visible aliases or additional descriptors:

- `codex_cli/gpt-5.5` remains the exact Codex CLI backend ref.
- `local_openai/...` may become the clearer namespace for local OpenAI-compatible servers.
- `openai/...` may become the clearer namespace for hosted OpenAI API execution.

Session records should preserve:

- `raw_model_ref`: what the operator selected or supplied.
- `provider_id`
- `model_id`
- `model_variant`
- `model_selection_source`

Add when available:

- `canonical_model_ref`
- `protocol`
- `alias_used`
- `model_descriptor_source`

## Test Plan

### Unit Tests

Add or extend:

- `tests/test_model_registry.py`
- `tests/test_model_catalog.py`
- `tests/test_provider_adapters.py`
- `tests/test_session_runtime.py`
- `tests/test_core_service.py`
- `tests/test_cli_smoke.py`
- `tests/test_tui_backend_wiring.py`

Required cases:

- Default config builds provider descriptors.
- Default config builds model descriptors.
- Catalog projection preserves current output.
- `provider/model@variant` and `provider/model:variant` parse correctly.
- Missing model ref blocks.
- Provider-less model ref blocks.
- Unknown provider blocks.
- Unknown model blocks.
- Disabled provider blocks.
- Missing adapter blocks.
- Local provider with hosted URL blocks.
- Hosted provider without approval blocks.
- Alias to canonical model records both refs.
- Alias to disabled provider blocks.
- Unknown variant blocks.
- Unsupported reasoning effort blocks.
- Provider/model/variant option merge is deterministic.
- Runtime does not construct adapters when validation fails.
- Runtime emits canonical model metadata on success.

### Integration Tests

Use fake clients/adapters.

- Fake OpenAI-compatible local endpoint returns `/models` and `/chat/completions`.
- Fake Codex adapter records command construction without invoking Codex.
- Fake hosted provider proves approval gates are checked before execution.
- Fake TUI projection proves blocked model appears as blocked and cannot start execution.

### Regression Tests

Preserve existing assertions:

- `provider_execution_started` is false for validation failure.
- `hidden_provider_fallback` is false.
- `hidden_model_fallback` is false.
- Unknown model can be persisted but not executed.
- Model catalog listing remains metadata-only.
- Disabled paid provider remains disabled by default.

## Migration Risks

### Risk: breaking current model picker

Mitigation:

- Keep `ModelCatalogEntry` projection stable.
- Do descriptor work behind current catalog functions first.
- Run TUI model picker tests before runtime changes.

### Risk: accidental hosted execution

Mitigation:

- Keep paid provider disabled by default.
- Add `hosted_provider_not_approved`.
- Add tests that monkeypatch adapter construction to fail if called after blocked validation.

### Risk: local endpoint misclassification

Mitigation:

- Reuse `validate_local_base_url()`.
- Preserve existing approved LAN endpoint policy.
- Add tests for loopback, private LAN, public IP, and hosted router URLs.

### Risk: descriptor/corpus drift

Mitigation:

- Schema-validate built-in metadata in tests.
- Mark unknown fields as unknown instead of guessing.
- Add source labels and timestamps for discovered metadata.

### Risk: too much provider surface at once

Mitigation:

- First wrap only existing Codex CLI and OpenAI chat-completions paths.
- Add OpenAI Responses and Anthropic only after the registry and validation tests are stable.

## Suggested Development Order

1. Add descriptor schemas and registry builder from current config.
2. Make catalog projection read from descriptors.
3. Extend validation to resolve descriptors and add blocked reasons.
4. Add protocol adapter registry with `codex_cli` and `openai_chat`.
5. Route one low-risk runtime path through descriptors behind the same public API.
6. Route all prompt/runtime execution paths through descriptors.
7. Add built-in metadata files.
8. Add aliases and canonical refs.
9. Add reasoning/variant/option merge.
10. Add explicit discovery commands.
11. Update TUI, CLI docs, command catalog, and smoke checklist.

## Done Definition

This plan is complete when:

- Every executable model ref resolves to a `ModelDescriptor`.
- Runtime dispatch is by `model.protocol`, not by ad hoc backend name checks.
- Existing `raw_model_ref` values continue to work.
- Unknown/disabled/misconfigured models fail before provider execution.
- Model catalog listing remains metadata-only.
- TUI and CLI can show protocol, source, canonical ref, variant, context, reasoning, and policy boundary.
- Local OpenAI-compatible execution still works.
- Codex CLI execution still works.
- Paid/hosted execution remains disabled unless explicitly configured and approved.
- Tests cover fail-closed validation, successful descriptor execution, alias resolution, and no hidden fallback.
