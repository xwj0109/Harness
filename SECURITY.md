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

Important safety properties:

- Local project state lives under `.harness/` and is treated as private local runtime state.
- Hosted-boundary work requires explicit approval before Codex planning or edit context is sent outside the local machine.
- Codex edit work happens in an isolated workspace first.
- Active repository changes require apply-back inspection, policy validation, and operator approval.
- Docker test execution uses a sanitized temporary workspace rather than mounting the active repository directly.
- Docker network access is disabled by default.
- Secret-like paths and project-private paths are blocked from normal tool access and apply-back.

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
