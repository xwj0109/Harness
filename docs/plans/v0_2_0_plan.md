# v0.2.0 Complete Implementation Plan

## Goal

Ship v0.2.0 as a declarative agent-spec and model-profile release. The release should let operators inspect built-in agents and workbenches, validate explicit user-authored spec bundles, export normalized registries, compare spec definitions, and preview resolved policy relationships without enabling scheduling, autonomy, active execution, hidden hosted routing, paid API fallback, or broad repository mutation.

v0.2.0 is a schema, registry, validation, and operator-inspection milestone. It prepares the harness for task queues and workbenches in later versions, but it must not introduce autonomous behavior.

## Current Baseline

v0.1 hardening is complete. The repository has run modes, backend descriptors, run manifests, JSON inspection output, `SECURITY.md`, `harness doctor`, and golden evidence tests.

The v0.2.0 foundation already exists:

- `ModelProfile`
- `ToolPolicy`
- `MemoryScope`
- `AgentSpec`
- `WorkbenchSpec`
- `SpecRegistry`
- `builtin_spec_registry()`
- Built-in profiles: `local_reasoning`, `codex_supervised`
- Built-in agents: `repo_inspector`, `code_editor`, `test_runner`, `quant_researcher`, `job_researcher`
- Built-in workbenches: `coding`, `quant`, `personal`
- Read-only custom bundle validation with required `schema_version: harness.spec_bundle/v1`
- Centralized explicit-path custom bundle guard
- Registry mapping key and contained id consistency checks
- Memory-scope hard-forbidden path invariants
- Model-profile backend compatibility invariants
- Tool-policy safety invariants
- Workbench forbidden-action invariants
- Normalized spec export for built-in and custom registries
- Registry diff for built-in versus custom registries
- Effective policy preview for built-in and custom registries
- `harness specs`, `harness specs agent`, `harness specs workbench`, `harness specs validate`, `harness specs export`, `harness specs diff`, and `harness specs preview` CLI surfaces

## Implementation Status

Treat this plan as the implementation checklist for the remaining v0.2.0 work. The schema, registry validation, read-only custom validation, normalized export, registry diff, and effective policy preview slices are implemented. The current implementation target is v0.2.0 documentation and release hygiene.

Already available:

- Built-in schema models and registry construction.
- Built-in starter model profiles, agents, and workbenches.
- Built-in registry inspection commands.
- Read-only JSON/YAML custom bundle validation.
- Normalized export, registry diff, and effective policy preview.

Next implementation target:

- v0.2.0 is ready for release review. After release, start v0.3 manual task queue planning.

Do not start runtime routing, task queues, scheduling, daemon behavior, execution, backend preflight, hosted fallback, paid fallback, OpenAI API usage, or `.harness/` persistence.

## Hard Boundaries

These boundaries apply to every v0.2.0 slice:

- Do not use OpenAI API or `OPENAI_API_KEY`.
- Do not add paid API fallback.
- Do not add hosted fallback.
- Do not read or expose secrets.
- Do not modify `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, or `secrets/`.
- Do not execute agents from spec validation or registry inspection commands.
- Do not preflight backends from spec validation or registry inspection commands.
- Do not persist custom spec bundles in `.harness/`.
- Do not add task queues, schedulers, daemons, background workers, or autonomy.
- Do not allow a workbench, agent, or custom spec to weaken repository-level forbidden paths.
- Keep Codex modeled as a supervised external agent backend, not a raw model provider.

## Cross-Cutting Specs Command Rules

All `harness specs ...` commands in v0.2.0 must:

- Work from an uninitialized directory.
- Avoid reading or writing `.harness/`.
- Avoid reading project config, SQLite, environment variables, backend settings, or secrets.
- Avoid backend preflight and all execution.
- Use stable JSON wrappers with `schema_version`.
- Emit deterministic ordering for machine-readable output.
- Use the same explicit-path guard for every command that reads a custom bundle.

## Release Definition

v0.2.0 is done when an operator can:

- Inspect built-in model profiles, tool policies, memory scopes, agents, and workbenches.
- Inspect one built-in agent or workbench by id.
- Validate an explicit JSON or YAML spec bundle without registering, persisting, activating, executing, or preflighting it.
- Receive stable human-readable and JSON validation output.
- Export a normalized built-in or custom registry in a stable JSON shape.
- Compare the built-in registry with an explicit custom spec registry and see added, removed, changed, and unchanged definitions.
- Preview resolved agent and workbench policy relationships without execution or runtime enforcement.
- Read operator documentation that explains v0.2.0 behavior and boundaries.
- Run the full test suite with existing tests passing.

## Non-Goals

Defer these until v0.3 or later:

- Persistent tasks.
- Task dependency graphs.
- Manual task runner commands.
- Daemon or 24/7 operation.
- Runtime agent routing.
- Effective permission inheritance enforcement at execution time.
- Dynamic tool execution from agent specs.
- Backend selection from custom model profiles.
- User spec activation.
- Registry persistence under `.harness/`.
- Browser, email, calendar, broker, trading, or external-message automation.
- Any hosted or paid fallback path.

## Workstream 1: Schema Hardening

Purpose: make the spec models strict enough to be durable release contracts.

Current status: implemented for v0.2.0. The core pydantic models, custom bundle schema-version contract, registry key/id checks, memory-scope hard-forbidden paths, model-profile backend compatibility, tool-policy safety rules, and workbench forbidden-action invariants are in place.

Implementation tasks:

- Add explicit schema version handling for user-authored spec bundles.
- Require user-authored spec bundles to declare `schema_version: harness.spec_bundle/v1`.
- Reject missing or unknown custom spec bundle schema versions with stable validation errors.
- Ensure mapping keys match contained `id` fields for model profiles, memory scopes, agents, and workbenches.
- Ensure tool policy ids are represented consistently if they remain mapping-only definitions.
- Reject duplicate logical ids after normalization.
- Reject empty `allowed_agents` for built-in workbenches unless a specific future use case requires empty workbenches.
- Validate `ModelProfile.kind` and backend compatibility rules:
  - `local` profiles may reference local-compatible backends only.
  - `external_agent` profiles may reference supervised external agent backends such as `codex_cli`.
  - No profile may imply OpenAI API, paid fallback, or hosted fallback.
- Validate `ToolPolicy` safety invariants:
  - `network` defaults to `forbidden`.
  - `hosted_boundary` defaults to `approval_required`.
  - `active_repo_write` is never broadened silently.
  - Built-in read-only policies keep active repo writes forbidden.
- Validate `MemoryScope` safety invariants:
  - Default forbidden paths include `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, and `secrets/`.
  - Custom memory scopes cannot remove repository hard-forbidden paths.
  - Custom allowed paths cannot point at hard-forbidden paths.
- Validate workbench safety invariants:
  - `forbidden_actions` cannot be overridden by custom specs to allow paid fallback, hosted fallback, live trading, broker actions, external message sends, or application submission.
  - Child or workbench-level declarations may narrow permissions but must not broaden repository hard rules.

Acceptance criteria:

- Unit tests cover valid and invalid schema versions.
- Unit tests cover mismatched mapping keys and contained ids.
- Unit tests cover forbidden memory-scope allowed paths.
- Unit tests cover unsafe model profile declarations.
- Built-in registry tests continue to prove all references resolve.
- Existing v0.1 tests continue to pass.

## Workstream 2: Read-Only Custom Spec Validation

Purpose: finish the explicit-path validator as the first complete custom-spec surface.

Current status: implemented. JSON/YAML parsing, explicit bundle schema-version validation, stable error payloads, guarded path resolution, and read-only command-contract tests are in place.

Custom bundles must declare this schema version:

```yaml
schema_version: harness.spec_bundle/v1
```

Implementation tasks:

- Keep validation limited to the operator-provided path.
- Support `.json`, `.yaml`, and `.yml`.
- Reject unsupported extensions before parsing.
- Reject `.harness`, `.git`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, and `secrets/` paths before reading.
- Parse with structured JSON/YAML parsers only.
- Convert parser and pydantic errors into stable validation error payloads.
- Return normalized valid registry contents on success.
- Return stable `ok: false` results with error arrays on failure.
- Ensure validation does not initialize a project.
- Ensure validation does not read config, preflight backends, write `.harness/`, or create run artifacts.

CLI target:

```bash
harness specs validate path/to/specs.json
harness specs validate path/to/specs.json --output json
harness specs validate path/to/specs.yaml
harness specs validate path/to/specs.yaml --output json
```

Acceptance criteria:

- Tests cover valid JSON and YAML bundles.
- Tests cover parse failures.
- Tests cover missing `schema_version`.
- Tests cover unknown `schema_version`.
- Tests cover missing cross-references.
- Tests cover unsupported extensions.
- Tests cover secret-like explicit paths.
- Tests prove validation does not create `.harness/`.
- Tests prove validation does not require project initialization.
- JSON output validates as JSON and uses a stable schema version.

## Workstream 3: Built-In Registry Inspection

Purpose: make built-in v0.2.0 definitions easy to inspect and verify from the CLI.

Current status: implemented. Keep this surface stable while finishing documentation and release hygiene.

Implementation tasks:

- Keep `harness specs` as a read-only built-in registry view.
- Keep `harness specs agent <agent_id>` as a read-only built-in agent view.
- Keep `harness specs workbench <workbench_id>` as a read-only built-in workbench view.
- Add focused inspection commands if needed:
  - `harness specs profiles`
  - `harness specs policies`
  - `harness specs memory-scopes`
- Preserve human-readable default output.
- Preserve stable JSON output with schema versions.
- Ensure missing ids fail clearly without tracebacks.
- Keep all inspection commands independent of project initialization.

Acceptance criteria:

- CLI tests cover default text output for built-in registry, agent, and workbench views.
- CLI tests cover JSON output for built-in registry, agent, and workbench views.
- CLI tests cover missing agent and workbench ids.
- Tests prove inspection does not create `.harness/`.

## Workstream 4: Normalized Export

Purpose: provide a stable machine-readable representation that later task and workbench features can depend on.

Current status: implemented. Normalized export is available for the built-in registry and explicit custom bundles.

Implementation tasks:

- Add normalized registry export for the built-in registry.
- Add normalized registry export for an explicit custom bundle.
- Ensure export ordering is deterministic.
- Use pydantic serializers rather than ad hoc text parsing.
- Include schema version, source type, source path when custom, and normalized registry sections.
- Avoid including environment, secrets, config internals, backend preflight results, or local machine details.

CLI target:

```bash
harness specs export --source builtin
harness specs export --source builtin --output json
harness specs export --source path/to/specs.yaml --output json
```

Expected JSON top-level shape:

```json
{
  "schema_version": "harness.spec_export/v1",
  "source": {
    "kind": "builtin",
    "path": null
  },
  "registry": {
    "model_profiles": {},
    "tool_policies": {},
    "memory_scopes": {},
    "agents": {},
    "workbenches": {}
  }
}
```

Acceptance criteria:

- Tests cover built-in export.
- Tests cover custom export from explicit JSON/YAML.
- Tests cover deterministic ordering.
- Tests cover invalid custom bundles returning validation failure.
- Export commands do not initialize a project or mutate `.harness/`.

## Workstream 5: Registry Comparison

Purpose: let operators review custom spec changes before later versions introduce activation or runtime routing.

Current status: implemented for built-in versus explicit custom bundles. Diff compares normalized registry payloads.

Implementation tasks:

- Add a pure comparison helper that takes two validated registries.
- Compare model profiles, tool policies, memory scopes, agents, and workbenches.
- Report added, removed, changed, and unchanged ids per section.
- Keep text mode concise and reviewable.
- Support built-in versus custom comparison.
- Reject invalid inputs with stable validation errors.
- Do not persist comparison results unless the operator redirects output outside the harness.

CLI target:

```bash
harness specs diff --source path/to/specs.yaml
harness specs diff --source path/to/specs.yaml --output json
```

Expected JSON top-level shape:

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
    "model_profiles": {
      "added": [],
      "removed": [],
      "changed": [],
      "unchanged": []
    },
    "tool_policies": {
      "added": [],
      "removed": [],
      "changed": [],
      "unchanged": []
    },
    "memory_scopes": {
      "added": [],
      "removed": [],
      "changed": [],
      "unchanged": []
    },
    "agents": {
      "added": [],
      "removed": [],
      "changed": [],
      "unchanged": []
    },
    "workbenches": {
      "added": [],
      "removed": [],
      "changed": [],
      "unchanged": []
    }
  }
}
```

Acceptance criteria:

- Tests cover no-op comparisons.
- Tests cover added, removed, and changed specs.
- Tests cover built-in to custom comparison.
- Tests cover invalid custom bundles.
- Diff commands do not initialize a project or mutate `.harness/`.

## Workstream 6: Effective Policy Preview

Purpose: provide a non-executing preview of how declarations combine, without claiming full runtime enforcement.

Current status: implemented. Preview resolves agent and workbench declarations into read-only summaries without runtime enforcement.

Implementation tasks:

- Add pure helpers that resolve an agent or workbench.
- For agents, report the declaration plus referenced model profile, tool policy, memory scope, and parent id.
- For workbenches, report the declaration, default model profile, allowed agents with referenced model/tool/memory specs, forbidden actions, and workbench-local declarative policy maps.
- Do not execute the agent.
- Do not route to a backend.
- Do not check backend availability.
- Do not persist the preview.

CLI target:

```bash
harness specs preview agent repo_inspector
harness specs preview agent repo_inspector --output json
harness specs preview workbench coding --output json
harness specs preview agent repo_inspector --source path/to/specs.yaml --output json
harness specs preview workbench coding --source path/to/specs.yaml --output json
```

Acceptance criteria:

- Tests cover built-in effective previews.
- Tests cover custom effective previews.
- Tests cover missing ids.
- Tests prove the command remains read-only and does not initialize `.harness/`.

## Workstream 7: Documentation

Purpose: make v0.2.0 usable by future contributors and operators.

Documentation tasks:

- Update `README.md` with a short v0.2.0 capability summary.
- Update `docs/operator_guide.md` with:
  - built-in spec inspection commands,
  - custom spec validation commands,
  - export, diff, and preview commands,
  - examples of safe validation failure,
  - explicit note that specs commands do not execute or activate agents.
- Update `docs/smoke_checklist.md` with v0.2.0 smoke checks.
- Update `docs/plans/next_steps.md` when v0.2.0 is complete, pointing next work at v0.3 manual task queue planning.
- Keep this plan as the canonical v0.2.0 implementation checklist.

Acceptance criteria:

- Docs describe current behavior, not future behavior as if implemented.
- Docs repeat the hard boundaries around no OpenAI API, no paid fallback, no hosted fallback, and no autonomy.
- Any documented command has at least one test or is explicitly marked as planned.

## Workstream 8: Release Hygiene

Purpose: make the final version bump and release state clean.

Implementation tasks:

- Update package metadata version from `0.1.0` to `0.2.0` only after the functional slices pass.
- Update README phase wording from Phase 1A/v0.1 language to v0.2.0 language.
- Run the full test suite.
- Review `git status --short` and ensure no restricted files are touched.
- Ensure no generated `.harness/` artifacts are staged.
- Ensure no secret-like files are read or modified.

Acceptance criteria:

- `pytest` passes.
- `harness specs --output json` emits valid JSON.
- `harness specs validate <valid-bundle> --output json` emits valid JSON and `ok: true`.
- `harness specs diff --source <valid-bundle> --output json` emits valid JSON.
- `harness doctor` behavior remains unchanged.
- No restricted paths are modified.

## Suggested Slice Order

1. Finish `harness specs validate` schema-version handling and read-only command-contract tests.
2. Harden registry invariants that are required by custom validation.
3. Add normalized export.
4. Add registry diff.
5. Add effective policy preview.
6. Update operator docs and smoke checklist.
7. Run full regression tests.
8. Bump version to `0.2.0`.

## Immediate Implementation Queue

Use this queue after v0.2.0 release:

1. Start v0.3 manual task queue planning.
2. Define persistent task records without daemon or autonomous background behavior.
3. Define manual `run-next` behavior with explicit operator control.
4. Preserve v0.2 registry inputs as the declarative source for later task/workbench routing.

## Test Plan

Run targeted tests after each slice:

```bash
pytest tests/test_specs_v0_2.py tests/test_registry_v0_2.py tests/test_spec_loader_v0_2.py
pytest tests/test_cli_smoke.py
```

Run the full suite before release:

```bash
pytest
```

Add new tests as features land:

- `tests/test_spec_export_v0_2.py`
- `tests/test_spec_diff_v0_2.py`
- `tests/test_spec_effective_preview_v0_2.py`
- Additional CLI coverage in `tests/test_cli_smoke.py` or a focused `tests/test_cli_specs_v0_2.py`

## Release Checklist

- [x] Custom spec validation is read-only and tested.
- [x] Schema versioning is explicit and tested.
- [x] Registry invariants are strict and tested.
- [x] Built-in spec inspection is stable and tested.
- [x] Normalized export is implemented and tested.
- [x] Registry diff is implemented and tested.
- [x] Effective policy preview is implemented and tested.
- [x] Operator docs and smoke checklist are updated.
- [x] Version is bumped to `0.2.0`.
- [x] Full `pytest` suite passes.
- [x] `git status --short` shows no restricted-path edits.

## v0.3 Handoff

After v0.2.0 ships, the next release should start from stable spec registry inputs and build a manual task queue. The first v0.3 planning target should be persistent task records and manual `run-next` behavior, with no daemon and no autonomous background work until v0.4.
