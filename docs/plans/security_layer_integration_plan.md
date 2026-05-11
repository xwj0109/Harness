# Security Layer Integration Plan

This plan turns the deep security-layer research into a Harness-native roadmap. The goal is not to turn Harness into a hosted platform. The goal is to make the existing local-first app express the same security structure clearly: trusted control plane, bounded execution plane, explicit policy decisions, isolated adapters, redacted evidence, and reversible operator control.

## Baseline

Harness already has several pieces that map directly to the research recommendations:

- Control-plane state: objectives, tasks, attempts, leases, runs, events, artifacts, approvals, manifests, and traces.
- Policy plane: `EffectivePolicy`, monotonic policy merging, backend descriptors, tool policies, workbench restrictions, and policy explain output.
- Approval plane: scoped hosted-boundary approvals and explicit apply-back approval separation.
- Execution plane: registered adapters, fail-closed dispatch, Codex read-only planning, Codex isolated editing, Docker test sandboxing, and no generic shell path.
- Secrets safeguards: forbidden path checks, secret-like path blocking, secret scanner primitives, log sanitization, and redaction state on artifacts.
- Evidence: run manifests, artifact hashes, backend hashes, policy hashes, compare/baseline snapshots, safety-smoke evals, and OTEL-shaped trace export.

The main gap is that these pieces are implemented as separate safety features rather than as one explicit security layer with stable concepts, decision records, trust tiers, kill switches, and security-specific regression checks.

## Hard Boundaries

This plan must preserve the repository rules:

- Do not use the OpenAI API or `OPENAI_API_KEY`.
- Do not add paid API fallback.
- Do not add hosted fallback.
- Do not read, print, summarize, copy, or expose secrets.
- Do not modify `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, or `secrets/`.
- Keep Codex modeled as a supervised external agent backend, not a raw model provider.
- Keep execution available only through registered adapters, active leases, explicit approvals, and existing apply-back boundaries.

Cloud-native items from the report, such as OIDC, mTLS, STS, Vault, Kubernetes admission, service mesh policy, SIEM rules, and microVM pools, should be treated as future deployment mappings. The local app should first implement the same control intent through local identities, local policy decisions, local evidence, local sandbox descriptors, and fail-closed dispatch.

## Target Shape

Harness should expose a security layer with four planes.

### Identity Plane

Current local equivalent:

- Operator intent is represented by CLI/app commands and confirmation.
- Hosted-boundary approval is represented by `ApprovalProfile`.
- Backend identity is represented by `BackendDescriptor`.
- Task ownership is represented by task leases and daemon owner fields.

Target:

- Introduce a typed `SecuritySubject` or equivalent local identity context for policy evaluation.
- Distinguish operator, daemon owner, backend, adapter, workbench, agent, task, and run identities in evidence.
- Include subject identifiers in adapter eligibility, run manifests, events, and trace spans.
- Keep this local and non-auth-provider-specific for now.

### Policy Plane

Current local equivalent:

- `resolve_*_effective_policy(...)` produces monotonic policy snapshots.
- Registered adapter descriptors state required approvals, side effects, replay policy, and sandbox requirements.
- Dispatcher and adapters perform exact metadata checks and fail closed.

Target:

- Centralize pre-execution security decisions into a typed decision record.
- Evaluate subject, task metadata, adapter descriptor, backend descriptor, approvals, data boundary, side-effect tier, sandbox tier, replay policy, and project state before run creation.
- Persist or emit every allow, deny, and approval-required decision with a stable decision id and policy hash.
- Make adapter-specific checks additive, not substitutes for the central decision.

### Secrets Plane

Current local equivalent:

- Secret-like paths are blocked.
- Text is scanned and redacted before logging.
- Artifact records carry `redaction_state`.
- Backend settings and environment variables are not printed by operator commands.

Target:

- Treat secrets as a first-class security boundary even though Harness does not broker real credentials yet.
- Add a local `CredentialUse` or `SecretLeaseRef` placeholder type only for evidence references, not secret values.
- Ensure artifacts, memory records, traces, events, and command outputs store references and redaction state, never secret material.
- Add regression coverage for secret-like content in task metadata, event payloads, artifact metadata, backend diagnostics, trace export, TUI context, and chat summaries.

### Execution Plane

Current local equivalent:

- Execution happens through registered adapters only.
- Codex editing happens in isolated workspaces.
- Active repo mutation requires inspected diff approval.
- Docker tests use sanitized temporary workspaces, disabled network by default, resource limits, command token validation, and docker-socket blocking.

Target:

- Add explicit sandbox/trust tiers for adapters and tools.
- Bind each adapter descriptor to a required sandbox profile and side-effect tier.
- Record sandbox profile evidence in manifests and traces.
- Add operator kill switches for adapter ids, task types, tool categories, and hosted-boundary execution.
- Keep shell, browser, MCP/A2A, email/calendar, broker actions, and unmanaged background execution out of scope until separate plans authorize them.

## Milestone 0: Security Layer Inventory

Goal: create a precise security map without changing execution authority.

Deliverables:

- Add or update documentation that maps report controls to Harness components.
- Add a local threat model section for the current app, including untrusted prompts, malicious repo content, Codex hosted-boundary use, isolated workspace apply-back, Docker test runs, and artifact/log leakage.
- Identify all pre-run gates in code:
  - task status and lease validation;
  - adapter descriptor metadata;
  - policy resolution;
  - hosted-boundary approval;
  - backend descriptor safety;
  - sandbox requirements;
  - apply-back validation.
- Identify all evidence outputs:
  - runs;
  - events;
  - artifacts;
  - manifests;
  - trace export;
  - compare/baseline;
  - smoke checklist.

Acceptance:

- The plan and security docs state where each security decision is made.
- No new execution path is added.
- Existing tests still pass.

## Milestone 1: Typed Security Decisions

Goal: make allow, deny, and approval-required decisions explicit objects.

Deliverables:

- Add a typed model such as `SecurityDecision`.
- Minimum fields:
  - `schema_version`;
  - `id`;
  - `created_at`;
  - `subject_kind`;
  - `subject_id`;
  - `resource_kind`;
  - `resource_id`;
  - `action`;
  - `decision`: `allow`, `deny`, or `approval_required`;
  - `policy_sha256`;
  - `required_approvals`;
  - `satisfied_approvals`;
  - `missing_approvals`;
  - `adapter_id`;
  - `task_type`;
  - `data_boundary`;
  - `side_effect_level`;
  - `sandbox_profile_id`;
  - `replay_policy`;
  - `reasons`.
- Add a central evaluator for registered adapter execution.
- Ensure `daemon inspect-lease` can show the decision without executing anything.
- Ensure `daemon execute` records the final decision before run creation.

Code targets:

- `src/harness/models.py`
- `src/harness/policy.py`
- `src/harness/execution.py`
- `src/harness/memory/sqlite_store.py`
- CLI display paths in `src/harness/cli/main.py`

Tests:

- Decision is deterministic for the same task, adapter, backend, approvals, and policy.
- Missing approval produces `approval_required`, not a run.
- Unsafe metadata produces `deny`, not a run.
- Unknown adapter produces `deny`, not a run.
- Existing adapter-level rejection events remain sanitized.

## Milestone 2: Adapter Trust Tiers And Sandbox Profiles

Goal: make execution isolation requirements visible, typed, and enforceable before dispatch.

Deliverables:

- Add `SandboxProfileDescriptor`.
- Minimum fields:
  - `id`;
  - `tier`: `none`, `read_only`, `isolated_workspace`, `docker_sandbox`, or `future_stronger_isolation`;
  - `network`: `forbidden`, `approval_required`, or `allowed`;
  - `active_repo_write`: `forbidden` or `approval_required`;
  - `host_filesystem`: `forbidden`, `sanitized_copy`, or `isolated_workspace`;
  - `resource_limits`;
  - `forbidden_mounts`;
  - `secret_path_policy`;
  - `notes`.
- Bind every registered adapter to a sandbox profile id.
- Include sandbox profile id in adapter descriptors, security decisions, run manifests, and trace spans.
- Add a CLI inspection surface for profiles if useful, but keep it read-only.

Initial profile mapping:

- `dry_run`: `none`, local evidence write only.
- `read_only_summary`: `read_only`, Codex read-only sandbox, hosted-boundary approval required.
- `repo_planning`: `read_only`, Codex read-only sandbox, hosted-boundary approval required.
- `codex_isolated_edit`: `isolated_workspace`, hosted-boundary approval required, active repo write forbidden until apply-back approval.
- Direct Docker tests: `docker_sandbox`, sanitized copy, network disabled by default, resource limits required.

Tests:

- Every registered adapter has a sandbox profile.
- Manifest includes sandbox profile evidence for executed runs.
- Read-only adapters fail if the backend cannot provide read-only sandbox support.
- Docker sandbox profile rejects host networking, docker socket mounts, shell strings, and secret-like copied paths.

## Milestone 3: Secrets And Redaction Hardening

Goal: make secret safety consistent across all user-visible and machine-readable outputs.

Deliverables:

- Extend secret scanning and redaction tests across:
  - events;
  - task metadata;
  - adapter rejection reasons;
  - backend diagnostics;
  - artifacts;
  - trace export;
  - chat context;
  - TUI dashboard context;
  - command catalog examples where relevant.
- Add explicit artifact registration behavior:
  - `redaction_state=not_required` for clean evidence;
  - `redaction_state=redacted` for safe derived evidence;
  - `redaction_state=blocked` for evidence that cannot be safely registered.
- Ensure derived redacted artifacts preserve lineage instead of mutating original evidence silently.
- Add tests for `OPENAI_API_KEY`, bearer tokens, private keys, AWS-style keys, password assignments, and secret-like paths.

Code targets:

- `src/harness/security.py`
- `src/harness/memory/sqlite_store.py`
- `src/harness/traces.py`
- `src/harness/operator_context.py`
- `src/harness/chat.py`
- `src/harness/tui.py`

Tests:

- No raw secret-like value appears in JSON output.
- No raw secret-like value appears in text output.
- No raw secret-like value appears in trace export.
- Secret-like artifact content is blocked or represented by redacted derived evidence.
- Forbidden paths are rejected before file contents are read.

## Milestone 4: Execution Kill Switches And Breakers

Goal: make risky execution reversible at the Harness control plane.

Deliverables:

- Add local kill-switch state for:
  - adapter id;
  - task type;
  - backend name;
  - hosted-boundary execution;
  - docker execution;
  - active repo apply-back.
- Keep kill-switch state in approved Harness runtime state, not planning docs.
- Add read-only inspection and explicit enable/disable commands.
- Ensure disabled capabilities are hidden or marked unavailable in chat/TUI capability surfaces and blocked in dispatcher decisions.
- Add a simple breaker model for repeated adapter failures:
  - threshold;
  - window;
  - breaker-open decision;
  - manual reset.

Rules:

- Kill switches cannot enable forbidden behavior.
- Kill switches can only narrow execution.
- A missing or unreadable kill-switch state must fail closed for high-risk execution and fail safe for read-only inspection.

Tests:

- Disabled adapter cannot execute even with a valid lease.
- Disabled hosted-boundary execution blocks Codex adapters.
- Disabled Docker execution blocks Docker test runs.
- Apply-back kill switch blocks active repo mutation.
- TUI and chat show blocked state without executing anything.

## Milestone 5: Prompt, Context, And Memory Boundary Controls

Goal: reduce prompt-injection and memory-poisoning risk without pretending local text can be perfectly sanitized.

Deliverables:

- Mark untrusted context sources in run manifests and trace spans.
- Add provenance metadata for:
  - repo files;
  - user prompts;
  - tool outputs;
  - artifacts;
  - generated plans;
  - memory records.
- Add policy checks for memory writes:
  - scoped;
  - explicit;
  - redacted;
  - lineage-preserving;
  - not permission-granting.
- Add operator-visible warnings when untrusted content contributes to a plan or edit request.

Rules:

- Memory cannot grant approvals, tools, hosted boundary access, or apply-back permission.
- Retrieved or generated text cannot weaken `EffectivePolicy`.
- Planning artifacts in `docs/plans/` remain documentation only.

Tests:

- Memory records containing secret-like text are redacted or blocked.
- Memory records do not change adapter eligibility.
- A malicious instruction inside a repo file cannot authorize hosted execution, active repo writes, Docker, shell, or network.

## Milestone 6: Security Telemetry And Local Detection

Goal: make Harness produce enough evidence to reconstruct important security decisions locally.

Deliverables:

- Extend trace spans and events with:
  - security decision id;
  - adapter id;
  - task type;
  - policy hash;
  - approval id;
  - sandbox profile id;
  - backend descriptor hash;
  - artifact ids and hashes;
  - redaction state;
  - final outcome.
- Add local detection checks for:
  - high-risk adapter attempted without approval;
  - hosted-boundary execution without approval;
  - active repo apply-back attempted without inspected diff approval;
  - Docker network unexpectedly enabled;
  - secret-like output in event/artifact/trace data;
  - adapter executed without sandbox profile evidence;
  - unknown adapter dispatch attempt;
  - breaker-open execution attempt.
- Expose detections through a local `harness security check` or the existing evals surface.

Tests:

- Detection suite passes on clean local evidence.
- Synthetic unsafe evidence produces expected findings.
- Detection output is sanitized and does not read forbidden paths.

## Milestone 7: Supply-Chain And Artifact Integrity

Goal: adapt the report's SBOM/signing/provenance ideas to local packaging without adding hosted infrastructure.

Deliverables:

- Ensure wheel/package smokes verify packaged built-in YAML specs and security-sensitive docs.
- Add artifact provenance records for generated planning, manifests, trace exports, and static TUI assets.
- Add local integrity checks for built-in specs and adapter descriptors.
- Document future hosted deployment mappings:
  - image signing;
  - SBOM generation;
  - provenance attestations;
  - admission verification.

Rules:

- Do not add remote verification services.
- Do not add network calls.
- Do not add package update automation.

Tests:

- Package smoke includes security-layer models and built-in descriptors.
- Built-in spec integrity checks fail closed on malformed policy broadening.
- Adapter descriptor drift is visible in compare/baseline evidence.

## Milestone 8: Documentation And Operator UX

Goal: make the security layer understandable from the app and docs.

Deliverables:

- Update `SECURITY.md` with the four-plane local security model.
- Update `docs/operator_guide.md` where operator-visible behavior changes.
- Update `docs/command_catalog.md` for new inspection commands.
- Update `docs/smoke_checklist.md` with security-layer smoke paths.
- Add concise TUI/chat explanations for blocked states:
  - missing approval;
  - disabled adapter;
  - unsafe metadata;
  - unknown adapter;
  - sandbox profile mismatch;
  - breaker open;
  - forbidden path or secret-like content.

Rules:

- The UI may explain and inspect.
- The UI must not create hidden approvals, hidden tasks, hidden leases, hidden runs, hidden memory, or hidden execution.

Acceptance:

- An operator can see why an action is blocked without reading SQLite manually.
- The same blocked reason is visible through JSON, text CLI, chat, and TUI.
- Docs do not describe unauthorized future adapters as available behavior.

## Recommended Release Slicing

This is too large for one implementation pass. A safe release sequence is:

1. `v1.8-security-foundation`: inventory, `SecurityDecision`, central evaluator, adapter decision display.
2. `v1.9-sandbox-and-secrets`: sandbox profiles, manifest evidence, redaction hardening.
3. `v2.0-control-and-detection`: kill switches, breakers, local security checks.
4. `v2.1-context-and-integrity`: untrusted-context provenance, memory safeguards, artifact integrity.

Each release should be independently shippable and should not broaden execution authority.

## First Implementation Slice

Start with the smallest useful slice:

1. Add `SecurityDecision` model.
2. Add a central registered-adapter decision function.
3. Wire `daemon inspect-lease` to report the decision.
4. Wire `daemon execute` to use and record the decision before run creation.
5. Add tests for allow, approval-required, deny, unknown adapter, unsafe metadata, and sanitized reasons.
6. Update `SECURITY.md` and the smoke checklist for the new decision evidence.

This slice creates the core security-layer abstraction without adding any new adapter, provider, network, shell, browser, hosted fallback, paid fallback, or active repo write path.

## Done Definition

The security-layer integration is complete when:

- Every registered execution attempt has a typed security decision before run creation.
- Every run manifest includes policy, approval, backend, sandbox, adapter, artifact, and redaction evidence.
- Every adapter has a declared trust tier and sandbox profile.
- Every user-visible evidence path is sanitized and covered by regression tests.
- Operators can disable risky execution categories quickly.
- Local detection checks catch approval bypass, sandbox mismatch, secret leakage, unknown adapters, and breaker-open execution attempts.
- Planning, memory, prompts, and tool outputs cannot grant permissions or weaken policy.
- The app remains local-first, supervised, and bounded by registered adapters.
