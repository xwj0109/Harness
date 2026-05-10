# Unified Chat Experience Plan

This plan originally described a separate chat command as an OpenCode-style CLI chatbot. It is now absorbed into the unified Harness application: bare `harness` launches one Textual app with passive dashboard context and the real chat/orchestrator prompt, while `harness --plain` keeps the line-oriented fallback for tests and unsuitable terminals.

The desired user experience is conversational:

```text
harness

> summarize this repository
> make a safe isolated Codex edit for this bug
> show me the diff
> deny apply-back
> what should I do next?
```

The implementation must not make the chat UI the authority layer. The chat UI is a conversational operator surface over explicit harness operations.

Core invariant:

```text
user asks -> chat proposes explicit harness action -> harness records/leases/dispatches -> evidence returns to chat
```

Never:

```text
user asks -> model decides -> model executes ambiently
```

## Non-Negotiable Safety Rules

- No OpenAI API usage or `OPENAI_API_KEY`.
- No paid API fallback.
- No hosted fallback.
- No generic shell execution.
- No direct active repository mutation by chat or model output.
- No hidden background execution.
- No MCP/A2A/browser/email/calendar/broker actions.
- No descriptor/spec/AGENTS.md permission grants.
- Codex remains a supervised external agent backend, not a raw model provider.
- Hosted-boundary approval is not apply-back approval.
- Apply-back remains denied by default unless an explicit apply-back approval path approves the inspected diff.
- `daemon run-once` leases only.
- `daemon execute` dispatches only already-leased tasks to registered adapters.

## Target Architecture

```text
unified Harness app
  passive dashboard context
  session state
  slash commands
  local command renderer
  intent parser
  confirmation prompts
  approval prompts
  progress renderer
  result summarizer

harness control plane
  tasks
  objectives
  attempts
  leases
  approvals
  policy
  runs
  artifacts
  manifests
  traces

harness execution layer
  registered adapter descriptors
  daemon execute dispatcher
  dry_run
  read_only_summary
  codex_isolated_edit
  future approved adapters
```

The chat layer may create tasks, inspect state, request approvals, lease work, dispatch registered adapters, and summarize run evidence. It must do so through the same CLI/service APIs that non-chat commands use.

## Milestone 1: Chat Shell Foundation

Goal: add an interactive CLI shell that feels like a chatbot but performs no model calls and no execution.

Command:

```bash
harness --project .
harness --project . --output json
harness --project . --plain
```

Behavior:

- Starts an interactive prompt with project context.
- Shows current project root, initialized state, active branch if available, task counts, active lease count, and available registered adapters.
- Supports local slash commands:
  - `/help`
  - `/home`
  - `/tasks`
  - `/runs`
  - `/leases`
  - `/adapters`
  - `/status`
  - `/quit`
- Plain natural-language input is accepted but only returns a safe message explaining that model-backed intent routing is not enabled yet.
- Does not create tasks, acquire leases, execute adapters, preflight backends, run Docker, call Codex, call local model backends, mutate `.harness/`, or write session history.

Implementation notes:

- Keep the chat engine separate from Textual so the unified app and `--plain` fallback call the same code.
- Reuse the dashboard builders as passive context inside the unified Textual app.
- Add a small `src/harness/chat.py` module for session state, slash-command parsing, and render payloads.

Acceptance criteria:

- `harness --project . --output json` returns availability/context without entering interactive mode.
- Interactive mode exits cleanly on `/quit`, `quit`, `exit`, or EOF.
- Slash commands are deterministic and local-only.
- No provider/backend/Docker preflight occurs.

Tests:

- CLI JSON availability test.
- Slash command parser tests.
- Interactive smoke using `CliRunner` input.
- Monkeypatch Codex/local backend/Docker constructors to assert chat shell does not touch them.

## Milestone 2: Read-Only Chat Context

Goal: make chat useful for inspecting local harness state without execution.

Behavior:

- Add chat responses for:
  - latest tasks
  - latest runs
  - active leases
  - registered adapters
  - selected task details
  - selected run manifest summary
  - selected artifact metadata
- Add natural-language aliases for read-only inspection only:
  - `show tasks`
  - `show latest run`
  - `what adapters are available?`
  - `what is blocked?`
  - `what is the current project state?`
- The response should feel conversational but be deterministic and local-only.

Implementation notes:

- Use a rule-based intent matcher first.
- Do not introduce a model dependency in this milestone.
- Responses should include the exact equivalent CLI command where useful.
- Keep JSON response schemas for testability:

```text
harness.chat/v1
harness.chat_response/v1
harness.chat_intent/v1
```

Acceptance criteria:

- Users can inspect queue/run/adapter state from chat without knowing exact CLI commands.
- All actions are read-only.
- No backend preflight or execution occurs.

Tests:

- Intent routing tests for local read-only phrases.
- Snapshot-like tests for stable response payload shape.
- Safety test proving no `.harness/` mutation beyond normal read access.

## Milestone 3: Task Drafting And Preview

Goal: let chat draft explicit task creation operations without executing them.

Supported task drafts:

- `dry_run / phase_1a_test`
- `read_only_summary / read_only_repo_summary`
- `codex_isolated_edit / codex_code_edit`

Behavior:

- User can say:

```text
summarize this repository
create a dry run task
prepare a Codex isolated edit for this bug
```

- Chat produces a draft task:

```text
title
description
execution_adapter
task_type
agent/workbench if supplied or inferred
required approvals if any
```

- Chat asks for explicit confirmation before calling `tasks add`.
- If user declines, no task is created.
- Codex task drafting must clearly say hosted-boundary approval is required before execution and apply-back is a separate later approval.

Implementation notes:

- Keep intent matching rule-based.
- Add `ChatDraftTask` model.
- Add a preview renderer with:
  - task metadata
  - safety notes
  - equivalent CLI command
  - mutates_when_confirmed flag
- Require confirmation by typing `yes` or `/confirm`.

Acceptance criteria:

- Drafting does not create task state.
- Confirmation creates exactly one task.
- Decline creates no task.
- Unsupported phrases produce a safe fallback response.

Tests:

- Draft-only no mutation.
- Confirmed dry-run task creation.
- Confirmed read-only task creation.
- Confirmed Codex task creation with safety note.
- Unsupported task type rejected.

## Milestone 4: Lease And Dispatch Flow

Goal: allow the chatbot to perform the safe equivalent of `daemon run-once`, `inspect-lease`, and `daemon execute` with explicit confirmation.

Behavior:

- For a ready task, chat can guide:

```text
lease the next task
inspect the lease
run the registered adapter
```

- Chat must show:
  - selected task
  - lease id
  - adapter id
  - execution eligibility
  - policy hash
  - rejection reasons if any
- Chat requires explicit confirmation before dispatching `daemon execute`.
- If adapter is missing/unknown/ineligible, chat shows fail-closed reason and does not retry.

Implementation notes:

- Do not create a new execution path.
- Internally call the same store/dispatcher functions used by CLI commands.
- Keep `daemon execute` result as the source of truth.
- Render result in conversational form, but include run id and artifact references.

Acceptance criteria:

- Dry-run task can be drafted, confirmed, leased, inspected, executed, and summarized entirely from chat.
- Unknown adapter rejection is shown as a safety refusal, not an error requiring workaround.
- Duplicate execution is rejected and explained.

Tests:

- End-to-end chat dry-run flow in temp project.
- Unknown adapter fail-closed flow.
- Duplicate execute flow.
- No run fabricated for pre-run rejection.

## Milestone 5: Read-Only Summary Chat Flow

Goal: make `read_only_summary` feel like a normal chatbot answer while preserving the adapter boundary.

Behavior:

- User says:

```text
summarize this repository
inspect the execution layer
what changed in git?
```

- Chat drafts and confirms a `read_only_summary` task.
- Chat leases and dispatches the registered adapter after confirmation.
- Chat renders:
  - progress status
  - run id
  - final model summary from artifact/report
  - tools executed
  - invalid command count if any
  - artifact references

Implementation notes:

- Use existing `ReadOnlyRepoSummaryRunner` through `daemon execute`.
- Do not call local model backend from chat directly.
- If Codex CLI is unavailable or hosted-boundary approval is missing, show adapter failure/rejection with recovery suggestion.

Acceptance criteria:

- The user experiences a conversational repo summary.
- The implementation still creates a task, lease, run, artifacts, manifest, and events.
- Chat answer cites the run id and final report artifact.

Tests:

- Fake Codex backend read-only chat flow.
- Backend unavailable path.
- Transcript/final report summary rendered without exposing backend settings.

## Milestone 6: Codex Isolated Edit Chat Flow

Goal: make queued Codex isolated editing feel like an OpenCode-style coding conversation while preserving all hosted-boundary and apply-back rules.

Behavior:

- User says:

```text
fix this bug with Codex
try an isolated edit for failing test X
modify only file Y
```

- Chat drafts a `codex_isolated_edit / codex_code_edit` task.
- Before execution, chat checks for hosted-boundary approval.
- If missing, chat explains and offers to create an approval profile only after explicit confirmation.
- Chat leases and dispatches only after confirmation.
- Chat shows:
  - isolated workspace status
  - run id
  - changed files
  - policy violations
  - apply-back decision
  - active repo mutation status
- By default, queued chat execution must deny apply-back unless an explicit apply-back approval provider is implemented.

Critical approval distinction:

```text
hosted-boundary approval = permission to send scoped context to Codex
apply-back approval = permission to mutate the active repository with inspected diff
```

Implementation notes:

- Do not call Codex directly from chat.
- Use `codex_isolated_edit` through `daemon execute`.
- Do not use Codex final message as patch source.
- Apply-back review UX can be added in a later milestone if the current approval provider is not interactive in chat.

Acceptance criteria:

- Missing hosted approval rejects before run creation.
- With hosted approval, Codex runs isolated.
- Denied apply-back is rendered as safe successful non-mutation.
- Applied changes, when supported by explicit approval path, show inspected changed files and run evidence.

Tests:

- Missing approval chat path.
- Fake Codex denied apply-back chat path.
- Fake Codex no-change chat path.
- Fake Codex policy violation chat path.
- Assert `OPENAI_API_KEY` is not exposed.

## Milestone 7: Conversational Result Memory

Goal: make the session useful across multiple turns without adding persistent memory or secret risk.

Behavior:

- Session-local references:
  - `latest task`
  - `latest lease`
  - `latest run`
  - `that diff`
  - `the failed task`
- No persistent memory in v1.
- No artifact content indexing unless explicitly requested and path/secret guards allow it.

Implementation notes:

- Add `ChatSessionState` with recent ids and summaries.
- Keep session state in memory only.
- Do not write preferences/history unless a later milestone explicitly plans artifact-backed memory.

Acceptance criteria:

- User can say `show that run` after an execution.
- Session reset clears references.
- No history file is written.

Tests:

- Session reference resolution.
- Session reset.
- No history persistence.

## Milestone 8: Streaming And Progress Rendering

Goal: make execution feel live without changing execution semantics.

Behavior:

- While an adapter runs, chat prints status updates:
  - task created
  - lease acquired
  - eligibility checked
  - run started
  - adapter completed/rejected/failed
  - artifacts available
- For long-running adapters, poll local run/events state.

Implementation notes:

- Use local events as source of truth.
- Do not stream raw backend/provider output directly unless sanitized and already captured as harness evidence.
- Keep non-interactive JSON mode stable.

Acceptance criteria:

- User sees progress without needing separate commands.
- Output remains sanitized.
- Terminal interruption does not corrupt task state.

Tests:

- Progress renderer unit tests.
- Interrupt/EOF smoke where possible.
- Event sanitization assertions.

## Milestone 9: Apply-Back Review UX

Goal: support a comfortable chat-native diff review while keeping active repo mutation explicit and validated.

Behavior:

- Chat can show:
  - diff stat
  - changed files
  - policy violations
  - freshness status
  - full diff path/artifact reference
- User can choose:
  - deny apply-back
  - approve apply-back
  - keep isolation for inspection
- Approval must be explicit and separate from hosted-boundary approval.

Implementation notes:

- Reuse existing diff inspection and apply-back provider mechanics.
- Do not parse or apply patches from chat text.
- Approval applies only the already-inspected diff artifact.

Acceptance criteria:

- Denial leaves active repo unchanged.
- Approval applies only validated existing text-file modifications.
- Freshness mismatch fails closed.

Tests:

- Deny path.
- Approve path with fake backend.
- Freshness failure path.
- Unsupported change path.

## Milestone 10: Polished OpenCode-Style UX

Goal: make the experience feel like a normal LLM coding CLI.

Features:

- Clear prompt with project/mode indicator.
- Slash command completion if dependency-light implementation is feasible.
- Compact status blocks.
- Human-readable summaries with run/artifact ids.
- Conversation transcript in memory.
- Mode indicators:
  - `read-only`
  - `planning`
  - `isolated edit`
  - `apply-back review`
- Helpful refusal messages.
- Copyable equivalent commands.

Non-goals:

- No autonomous background loop.
- No generic terminal shell.
- No hidden provider calls.
- No persistent memory.
- No plugin marketplace.

Acceptance criteria:

- A user can perform the common flow without knowing internal command names:

```text
summarize repository -> confirm -> run read-only adapter -> receive summary
try isolated Codex edit -> approve hosted boundary -> run adapter -> review/deny diff
```

- Every action remains visible as an explicit harness operation.
- All evidence remains available through normal run/artifact/manifest commands.

## Recommended PR Sequence

### PR 1: Chat Shell Foundation

- Add the root `harness` app entrypoint and `--plain` fallback.
- Add `src/harness/chat.py`.
- Add slash commands and read-only project context.
- Add tests proving no backend/Docker/adapter execution.

### PR 2: Read-Only Chat Inspection

- Add rule-based read-only intent routing.
- Add task/run/lease/adapter renderers.
- Add stable JSON response schemas.

### PR 3: Task Drafting

- Add task draft models.
- Add preview/confirm flow.
- Support dry-run, read-only summary, and Codex isolated edit task drafts.

### PR 4: Lease And Dispatch

- Add confirmed lease/inspect/execute chat flow.
- Use registered dispatcher only.
- Add dry-run end-to-end chat test.

### PR 5: Read-Only Summary Chat

- Add conversational repo-summary flow through `read_only_summary`.
- Add fake local backend test.

### PR 6: Codex Isolated Edit Chat

- Add hosted-boundary approval UX.
- Add queued Codex isolated edit flow through `codex_isolated_edit`.
- Preserve deny-by-default apply-back.

### PR 7: Session References And Progress

- Add in-memory recent task/lease/run references.
- Add progress renderer from local events.

### PR 8: Apply-Back Review UX

- Add chat-native diff review.
- Add explicit approve/deny apply-back flow.

### PR 9: Polish And Docs

- Update operator guide, command catalog, smoke checklist, and this plan.
- Add end-to-end smoke scripts.

## Test Gates

## Chat-First Orchestrator Update

The unified Harness app is now the primary operator surface. It supports selectable built-in orchestrators, inline passive dashboard context, visible objective/task graph drafts, and a bounded foreground `/run` loop. For current testing, built-in agents and workbenches default to `codex_supervised`, and orchestrated chat-created tasks use `codex_isolated_edit/codex_code_edit`.

This does not add a raw provider path or autonomous background executor. The one-run approval loop creates explicit Harness records, then uses the same daemon run-once lease path and registered dispatcher as non-chat commands.

Every PR:

```bash
python3 -m pytest -q
```

Focused gates by area:

```bash
python3 -m pytest -q tests/test_cli_smoke.py
python3 -m pytest -q tests/test_docs_phase_3d.py
python3 -m pytest -q tests/test_codex_apply_back_c3.py
```

Manual smoke gates:

```text
harness --project . --output json
harness --project . --plain  # /help, /home, /tasks, /adapters, /quit
dry-run chat flow in temp project
unknown adapter rejection flow in temp project
read-only summary flow with fake Codex backend
Codex queued flow only on disposable branch with explicit hosted approval
```

## Done Definition

The chat CLI is done when:

- A normal user can operate the harness conversationally without knowing all command names.
- The system still records explicit tasks, leases, runs, artifacts, manifests, approvals, and events.
- All execution goes through registered adapters.
- Missing/unknown/unsafe execution fails closed.
- Hosted-boundary approval and apply-back approval are visibly separate.
- The active repository is never changed by chat/model output directly.
- Full regression passes.
