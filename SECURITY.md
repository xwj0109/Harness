# Security

`agent-harness` is a local-first supervised agent runtime. Its safety model is built around local project control, explicit approval gates, isolated edits, reviewable artifacts, and no hidden hosted or paid fallback behavior.

## Hard Boundaries

- Do not use the OpenAI API or `OPENAI_API_KEY`.
- Do not add paid API fallback.
- Do not add hosted fallback.
- Do not read, print, summarize, copy, or expose secrets.
- Do not modify `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, or `secrets/`.
- Preserve local/private data-boundary safeguards.
- Treat Codex as a supervised external agent backend, not a raw model provider.

## Current Safety Model

The current system is a v0.1 safety kernel. It records runs, events, artifacts, backend metadata, approvals, isolated edit outputs, test results, and safety-related events under local project state.

Harness uses a four-plane local security model:

- Policy and approvals decide whether a task, adapter, backend, hosted boundary, Docker path, or apply-back path may proceed.
- Runtime controls and breakers can locally narrow execution by disabling risky categories or pausing repeatedly failing adapters.
- Sandbox, profile, and evidence boundaries describe where execution may run and what proof is recorded before and after dispatch.
- Context, provenance, integrity, and detection make untrusted inputs and local evidence inspectable without granting permissions.

Important safety properties:

- Local project state lives under `.harness/` and is treated as private local runtime state.
- Hosted-boundary work requires explicit approval before Codex planning or edit context is sent outside the local machine.
- Codex edit work happens in an isolated workspace first.
- Active repository changes require apply-back inspection, policy validation, and operator approval.
- Docker test execution uses a sanitized temporary workspace rather than mounting the active repository directly.
- Docker network access is disabled by default.
- Secret-like paths and project-private paths are blocked from normal tool access and apply-back.
- Artifact registration marks clean evidence as `not_required`, stores redacted derived evidence for secret-like text, and blocks secret-like artifact paths.

## Security Decisions

Generic registered-adapter dispatch records a typed pre-run security decision before creating a run. `daemon inspect-lease` can show the decision without executing anything, and `daemon execute` fails closed when the decision is `deny` or `approval_required`.

The decision is additive evidence over the existing policy, approval, lease, and adapter checks. It does not grant new execution authority, add sandbox profiles, bypass hosted-boundary approval, bypass apply-back approval, or enable unregistered adapters.

## Runtime Controls

Local runtime kill switches can disable registered adapters, task types, the Codex backend target, hosted-boundary execution, Docker execution, and active repo apply-back. These controls only narrow authority: enabling a control does not grant execution that policy, approval, lease, sandbox, or adapter validation would otherwise deny.

Adapter breakers deny new generic dispatch after repeated adapter execution failures in a short local window. Breakers count execution failures, not approval-required or policy-denied outcomes, and require an explicit local reset before dispatch resumes.

## Context And Memory Boundaries

Run manifests and traces include context provenance for prompts, task metadata, memory records, generated evidence, and artifacts. Provenance records mark trust level, source kind, redaction state, ids, and hashes where available without embedding artifact contents.

Memory is local operator context only. It is explicitly marked as non-authoritative for permissions, policy, approvals, hosted-boundary execution, Docker/network access, shell/tool grants, and active repo apply-back. Untrusted repo content, generated text, artifacts, and memory can inform operator-visible context, but they cannot weaken `EffectivePolicy` or satisfy approval gates.

## Local Detection

`harness security check` and `harness evals run --suite security` inspect local metadata for security findings such as missing approval evidence, unknown adapter attempts, breaker-open dispatch attempts, Docker network evidence, missing sandbox profile evidence, unsafe apply-back evidence, and secret-like metadata. These checks do not read artifact bodies, call providers, run Docker, create records, or inspect forbidden paths.

## Local Integrity And Provenance

`harness integrity check` and `harness evals run --suite integrity` record local package and evidence integrity for built-in specs, registered adapter descriptors, security-sensitive docs, and static TUI assets. The checks compute metadata and hashes locally, validate that built-in specs preserve security invariants, and make adapter descriptor drift visible in run compare/baseline evidence.

Run manifests and trace exports include artifact provenance records for generated evidence. Provenance is evidence only: it does not sign artifacts, call remote verification services, create SBOMs, admit deployments, update packages, or grant execution authority.

## Security Layer Completion Scope

For the local-first registered-adapter scope, the security layer is complete when `harness evals run --suite security-layer` passes. That audit verifies typed pre-run decisions, adapter sandbox profiles, manifest evidence, local controls, local detections, integrity checks, context/memory authority boundaries, and operator-visible blocked-state explanations.

The audit is evidence only. It does not remediate state, create approvals, create runs, execute adapters, call providers, run Docker, perform network checks, or implement future hosted deployment controls such as remote signing, SBOM services, admission controllers, service meshes, or SIEM export.

## Threat Model

Primary risks:

- Accidental secret disclosure through prompts, artifacts, logs, diffs, or tool output.
- Unapproved movement from local-only execution to hosted-provider execution.
- Agent edits escaping an isolated workspace and mutating the active repository directly.
- Unsafe apply-back of generated, binary, symlink, deleted, renamed, or secret-like files.
- Docker test execution gaining access to host secrets, host networking, the Docker socket, or unfiltered project state.
- Future orchestration features weakening current approval and data-boundary rules.

Current mitigations:

- Forbidden path checks for `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, and `secrets/`.
- Secret scanner primitives and log sanitization helpers.
- Hosted data-boundary approval profiles.
- Isolated Codex edit workspaces.
- Diff inspection and policy validation before active-repo apply-back.
- Per-run approval for Docker test execution.
- Sanitized temporary Docker workspaces with generated/local artifacts excluded.

## Non-Goals

The project does not currently provide:

- A hosted agent platform.
- A raw OpenAI API wrapper.
- Paid API execution or paid fallback.
- Hidden hosted fallback.
- Browser, email, calendar, plugin, MCP, or marketplace automation.
- Automatic external message sending.
- Automatic job application submission.
- Live trading, broker integration, capital allocation, or order placement.

## Reporting And Review

This is a local project, so security issues should be handled by local review before broader publication. When changing security-sensitive behavior, include focused tests and update this document plus the operator guide where behavior changes are visible to operators.
