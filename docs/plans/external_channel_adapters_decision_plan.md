# External Channel Adapters Decision Plan

This plan is the first post-v1.8 decision gate from `openclaw_style_next_version_plan.md`. It does not authorize implementation of Slack, Discord, Telegram, email, calendar, Matrix, Signal, Teams, WhatsApp, web chat, or any other external channel adapter.

The purpose is to define the minimum safe shape that a later channel-adapter implementation plan would need before any code can be written.

## Product Target

Harness should remain a local-first supervised agent app. External channels, if ever added, must be operator-facing notification and handoff surfaces around the existing Harness control plane, not new execution authorities.

The first acceptable channel milestone is **read-only channel readiness**, not live messaging:

- Describe channel adapter boundaries in docs and schemas.
- Represent a channel as a disabled capability descriptor until credentials, inbound events, outbound messages, and audit evidence have separate approval gates.
- Keep `harness` TUI/chat as the primary operator surface.
- Preserve objectives, tasks, leases, approvals, registered adapters, memory, progress, artifacts, traces, and apply-back as the only control-plane authorities.

## Non-Negotiable Boundaries

Any future channel plan must preserve these rules:

- No OpenAI API usage or `OPENAI_API_KEY`.
- No paid API fallback.
- No hosted fallback.
- No generic shell.
- No browser automation.
- No MCP/A2A side door.
- No third-party skill marketplace execution.
- No unmanaged background autonomy.
- No direct active repo writes.
- No secret reads or secret exposure.
- No message sending, replying, deletion, archival, label changes, calendar edits, or external side effects without a separate explicit operator approval model.
- No channel may create tasks, acquire leases, approve hosted-boundary access, approve apply-back, dispatch adapters, or mutate `.harness/` state by itself.

Channel credentials, tokens, webhook secrets, OAuth refresh tokens, signing secrets, and API keys must never be stored in Harness SQLite, memory records, artifacts, traces, logs, TUI context, chat responses, or docs.

## Candidate Interface Shape

A later implementation plan may add only disabled, read-only descriptors first:

- `harness.channel_adapter/v1`: one descriptor per potential adapter with id, channel type, direction support, required approvals, credential boundary, side effects, replay policy, redaction notes, and readiness.
- `harness.channel_catalog/v1`: local read-only list of available and disabled channel descriptors.
- `harness channels list|inspect --output json`: read-only CLI over descriptors.

Descriptor readiness values should be limited to:

- `not_implemented`.
- `disabled_until_connector_plan`.
- `blocked_by_missing_credential_boundary`.
- `blocked_by_missing_message_approval_model`.

No descriptor may include live tokens, workspace ids, user ids, email addresses, webhook URLs, channel names, message contents, or provider API responses in v1.

## Required Future Decisions

Before implementation, a separate plan must decide:

- Which single channel is first. Do not implement multiple channels together.
- Whether the first slice is inbound-only, outbound-only, or read-only catalog-only.
- Credential storage policy and whether Harness stores no credentials at all.
- Inbound event trust model, replay/idempotency keys, and sanitization.
- Outbound message approval model, preview UX, and cancellation/revocation behavior.
- Audit evidence schema for sent/blocked/failed events without retaining message secrets.
- Rate limits and kill switches.
- Operator identity mapping.
- How channel events map to objectives/tasks without granting authority.
- Tests proving no provider calls happen from passive app startup, catalog listing, memory, or progress rendering.

## Acceptance Criteria For Any Later Implementation

A future implementation may proceed only when it proves:

- Passive `harness`, `harness --output json`, TUI dashboard, chat `/capabilities`, chat `/progress`, and `harness channels list` do not call channel providers.
- Unknown or disabled channel adapters fail closed.
- Inbound content is treated as untrusted context, not operator approval.
- Outbound content requires explicit operator confirmation and is auditable before send.
- `/reset` does not delete durable channel evidence or credentials.
- Memory records cannot authorize sending, approval, execution, or apply-back.
- Runtime controls can disable channel use globally.
- Full regression, docs checks, wheel smoke, and safety-smoke evals pass.

## Deferred Items

These remain out of scope until separately planned:

- Slack/Discord/Telegram/email/calendar/Teams/WhatsApp production integrations.
- OAuth flows and credential storage.
- Webhook servers or public network listeners.
- Automatic external replies.
- Message deletion, archival, labeling, calendar edits, or external workflow mutation.
- Browser-driven channel automation.
- MCP/A2A channel bridges.
- Background channel polling loops.
- Channel-triggered task execution or approval.
