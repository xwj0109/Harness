# Backend To TUI Wiring Plan

Status: draft

Goal: make the Harness backend and Textual TUI operate as one reliable local operator app. The user should be able to start `harness`, inspect state, create and switch sessions, send prompts, stream assistant progress, resolve permissions, manage session metadata, and recover from failures without hidden execution, stale views, or split-brain state between direct `SQLiteStore` calls and the local backend/runtime.

## Current State

Harness already has two partially overlapping application paths.

Backend path:

- `src/harness/local_server.py` exposes an authenticated local HTTP API with `/health`, `/openapi.json`, `/sessions`, `/sessions/{session_id}`, `/sessions/{session_id}/messages`, `/sessions/{session_id}/prompt`, `/sessions/{session_id}/prompt_async`, `/sessions/{session_id}/events/stream`, `/permission`, `/question`, `/tools`, `/commands`, `/settings/tui`, and related projections.
- `src/harness/session_runtime.py` owns process-local live runtime state for queued prompts, active turns, provider events, waiting permissions, retries, compaction, and session status.
- `src/harness/event_broker.py` publishes persisted store events to process-local subscribers and powers backend SSE streams.
- `harness serve` starts the local server only for initialized projects, with bearer auth and generated or supplied token.
- `harness attach` can probe an existing local server, but it is currently read-only and separate from the TUI.

TUI path:

- `src/harness/tui.py` creates the Textual app through `create_harness_app()` and `run_harness_app()`.
- The TUI reads projections directly with `build_tui_dashboard()` and `build_session_pane_projection()`.
- The TUI submits natural prompts with `handle_chat_input()` in a worker thread.
- Session pane actions call `SQLiteStore` directly for blank session creation, archive, restore, cancel metadata, fork, hard delete, rename, agent selection, and model selection.
- Live refresh is polling-based: `_refresh_live_view()` refreshes dashboard state roughly every 1.5 seconds and re-renders more often while a prompt is in flight.
- The TUI has strong operator-surface discipline: read-only dashboard/palette by default, visible action contracts, no ambient shell, no hidden provider fallback, and explicit session-tool permission paths.

Main mismatch:

- Backend runtime and TUI prompt execution are not yet one path. The TUI can produce useful local chat/orchestration behavior through `handle_chat_input()`, while the backend can queue and execute session prompts through `SessionRuntimeManager`, but the TUI does not consume that backend contract.
- Direct store mutations from the TUI bypass the same route-level response envelopes, event semantics, and runtime status projections that HTTP clients will use.
- TUI freshness depends on polling persisted state instead of subscribing to the same event stream that backend clients use.

## Non-Negotiable Product Contract

The TUI remains a supervised operator cockpit, not a generic chat terminal.

Every TUI action must fit exactly one authority class:

- Read-only projection: dashboard, session rail, right pane, transcript, events, model/provider/tool catalogs, settings, files/VCS summaries, progress, evidence, and terminal-tab projections.
- UI-local state: focus, selected row, collapsed sections, filters, palette search, prompt draft, in-memory theme choice, dialog cursor.
- Explicit Harness action: create session, submit prompt, enqueue follow-up, reply to permission, answer/reject question, abort runtime, fork session, archive/restore/delete session, rename session, update session model/agent, retract/correct message.
- Fail-closed unsupported operation: arbitrary command execution, generic shell, provider auth mutation, config writes, PTY websocket, worktree reset, apply-back, hosted fallback, paid fallback, plugin/MCP network activity, desktop launch, or any action that would broaden authority outside the existing Harness contract.

The TUI must never:

- Grant permission implicitly.
- Hide provider/model execution behind a UI refresh.
- Start a background daemon outside visible runtime state.
- Treat fail-closed backend responses as crashes.
- Store bearer tokens in session records, events, logs, or prompt text.
- Read artifact bodies or secret paths for display.
- Mutate active repository files except through an existing explicit Harness action path that already records approval/evidence.

## Target Architecture

Use one in-process backend service contract with an optional HTTP transport.

The TUI should not become an HTTP-only client by default. It runs in the same Python process as the CLI and can call a shared service layer directly. HTTP remains the contract for browser/remote clients and for verifying route parity.

Target layers:

1. Core service layer:
   - Add or complete a `src/harness/core_service.py` facade that wraps session, prompt, runtime, event, permission, question, settings, and catalog operations.
   - Each service method returns the same shape as the corresponding local-server route: `schema_version`, `ok`, domain payload, mutation flags, `permission_granting: false` unless it is an explicit permission reply, and clear `error_code` on failures.
   - The service owns all direct `SQLiteStore`, `SessionRuntimeManager`, and projection calls for app clients.

2. Local server adapter:
   - `local_server.py` should delegate route work to the service layer instead of duplicating store mutations.
   - HTTP remains responsible for auth, CORS, body parsing, URL params, status codes, and SSE serialization.

3. TUI client adapter:
   - The TUI calls the service layer directly by default.
   - Add an optional attach mode later for `harness --server-url ... --token ...` or `harness tui --server-url ... --token ...` that uses HTTP against an already-running server. This is not the first milestone.

4. Event bridge:
   - The service exposes a process-local subscription API over the same `EventBroker` used by SSE.
   - The TUI subscribes to session/global events and invalidates dashboard/session/transcript projections on event arrival.
   - Polling remains as a low-frequency safety net.

## Backend Service Interface

Create a small app-facing service object instead of making the Textual app know backend details.

Suggested API:

```python
class HarnessAppService:
    def __init__(
        self,
        project_root: Path,
        *,
        store: SQLiteStore | None = None,
        runtime: SessionRuntimeManager | None = None,
        execution_enabled: bool = True,
    ) -> None: ...

    def health(self) -> dict: ...
    def dashboard(self, *, selected_session_id: str | None = None) -> dict: ...
    def session_pane(self, *, selected_session_id: str | None, status_filter: str, query: str) -> dict: ...
    def list_sessions(self) -> dict: ...
    def create_session(self, body: dict) -> dict: ...
    def update_session(self, session_id: str, body: dict) -> dict: ...
    def archive_session(self, session_id: str) -> dict: ...
    def restore_session(self, session_id: str) -> dict: ...
    def hard_delete_session(self, session_id: str) -> dict: ...
    def fork_session(self, session_id: str, body: dict) -> dict: ...
    def append_message(self, session_id: str, body: dict) -> dict: ...
    def submit_prompt(self, session_id: str, body: dict) -> dict: ...
    def prompt_async(self, session_id: str, body: dict) -> dict: ...
    def abort_session(self, session_id: str, body: dict) -> dict: ...
    def list_messages(self, session_id: str, *, limit: int | None = None) -> dict: ...
    def list_events(self, session_id: str, *, after_seq: int | None = None) -> dict: ...
    def runtime_status(self, session_id: str) -> dict: ...
    def list_permissions(self, session_id: str | None = None) -> dict: ...
    def reply_permission(self, session_id: str, permission_id: str, body: dict) -> dict: ...
    def list_questions(self, session_id: str | None = None) -> dict: ...
    def reply_question(self, question_id: str, body: dict) -> dict: ...
    def settings_tui(self, session_id: str | None = None) -> dict: ...
    def subscribe_session_events(self, session_id: str, *, after_seq: int | None = None) -> EventSubscription: ...
    def subscribe_global_events(self) -> EventSubscription: ...
```

Rules:

- Do not expose raw `SQLiteStore` from the service to the TUI.
- Do not let the service call `typer`, print text, or depend on Textual.
- Keep route and service response schemas stable and versioned.
- Ensure every mutating method appends a store event, or explicitly states why no event is recorded.
- Ensure every method has `permission_granting: false` unless its only purpose is resolving a permission request.

## TUI Runtime Modes

Implement two explicit runtime modes.

### Mode A: Embedded Service (first milestone)

Default command:

```bash
harness --project .
harness tui --project .
```

Behavior:

- No HTTP server is started.
- No bearer token is generated.
- The TUI instantiates `HarnessAppService(project_root)`.
- Runtime execution uses the same `SessionRuntimeManager` object family as the backend route path.
- Events are delivered through process-local `EventBroker` subscriptions.
- This keeps the local terminal app simple and avoids local auth/token UX inside the TUI.

### Mode B: Attached Server (later milestone)

Optional command:

```bash
harness tui --server-url http://127.0.0.1:8765 --token "$HARNESS_SERVER_TOKEN"
```

Behavior:

- TUI calls the HTTP API with bearer auth.
- TUI reads SSE through a fetch/urllib streaming equivalent, not native browser `EventSource`.
- TUI uses the same response schemas as embedded mode.
- This mode is for multi-client/server testing, not the default local app.

Do not build attached mode until embedded service parity is complete.

## Prompt Submission Contract

The biggest user-visible gap is prompt handling. The TUI composer must submit through one canonical path.

### Current TUI path

`action_submit_prompt()`:

- Handles safe slash commands and UI actions locally.
- Sends natural text to `handle_chat_input(request, project_root, self._chat_state, progress_callback=progress)`.
- Stores user and assistant messages only in TUI memory for the visible transcript unless the chat path itself creates session records/events.

### Target prompt path

For natural language that is not a safe slash/UI command:

1. Resolve or create the active session through the service.
2. Append the user message with the session id, selected agent id, selected model ref, durable cwd, and prompt metadata.
3. Submit the prompt to runtime through `submit_prompt()` or `prompt_async()`.
4. Render the persisted user message immediately in the transcript.
5. Subscribe to session events and append streamed `model.message_delta`, `tool_call.*`, `permission.*`, `harness.runtime.*`, and `harness.turn.*` updates.
6. When the turn finishes, refresh session messages and replace any transient streaming text with persisted assistant message parts.

Default send mode:

- Use an async runtime path for the TUI: accept immediately, stream events, and keep the app responsive.
- The response envelope must indicate whether execution started:
  - `accepted`
  - `queued`
  - `execution_started`
  - `worker_started`
  - `runtime.phase`
  - `turn_id`
  - `prompt_id`

Queue behavior:

- If the session is idle: start the worker.
- If the session is running: default to `FOLLOW_UP`.
- If the operator explicitly chooses steering later: use `STEER`.
- If the selected session is terminal: block and offer fork/new session.

Important decision:

- Keep `handle_chat_input()` for deterministic slash-style operator commands and action-contract drafting until those flows are migrated into the service.
- Do not mix one visible prompt across both `handle_chat_input()` and `SessionRuntimeManager`. A prompt either creates a deterministic Harness action draft or enters session runtime, not both.

## Transcript Projection

The TUI transcript should become a projection over persisted session data plus transient event deltas.

Data sources:

- `GET/service list_messages(session_id)` for durable messages and parts.
- `GET/service list_events(session_id)` for turn, tool, permission, runtime, and provider event timeline.
- Live event subscription for incremental updates.

Rendering rules:

- User messages render from persisted session messages, not only `_messages`.
- Assistant final text renders from persisted assistant message parts.
- Model deltas render transiently while a turn is running.
- Tool calls render as compact procedure rows with status and sanitized target.
- Permission wait renders a visible card with operation, target, risk, boundary, allowed replies, and exact equivalent CLI command.
- Event and message rendering must not reveal secret path contents, artifact bodies, bearer tokens, raw environment values, or unredacted provider payloads.

Migration tactic:

- Introduce `build_tui_transcript_projection(store/service, session_id, transient_events=...)`.
- Move `render_codex_like_transcript()` to consume that projection instead of `_messages` as the source of truth.
- Keep `_messages` only for:
  - first-run welcome before any session exists;
  - transient local UI notices;
  - legacy deterministic action-draft responses during the migration.

## Live Refresh And Events

Replace high-frequency full polling with event-driven invalidation.

Current:

- `_refresh_live_view()` runs every 0.25 seconds.
- Dashboard rebuild happens at most every 1.5 seconds or while a request is in flight.

Target:

- Subscribe to global events after mount.
- Subscribe to active session events whenever the active session changes.
- Keep `last_seen_seq` per session.
- On session event:
  - update transient stream buffer;
  - mark dashboard/session pane/transcript dirty;
  - render lightweight transcript changes immediately.
- On global event:
  - mark dashboard/right pane/session rail dirty.
- Keep a fallback poll every 5 seconds for missed events and initial compatibility.

Event kinds the TUI must understand first:

- `harness.runtime.prompt_queued`
- `harness.turn.started`
- `model.started`
- `model.message_delta`
- `model.completed`
- `harness.runtime.retry_scheduled`
- `harness.runtime.compaction_started`
- `harness.runtime.permission_waiting`
- `harness.runtime.permission_resolved`
- `harness.turn.finished`
- `operator.turn.started`
- `tool_call.started`
- `tool_call.output`
- `tool_call.finished`
- `permission.checked`
- `session.model_validation`
- `tui.ui_activation.applied`

Unknown events:

- Render only a sanitized timeline row if user-visible.
- Never crash the TUI on an unknown event kind.

## Permission And Approval Flow

The TUI needs a first-class permission card wired to backend/session tools.

Minimum behavior:

- Detect pending permission from session runtime/operator projection.
- Show operation, cwd, command/tool, target, risk, policy reasons, boundary kind, scope, and expiration.
- Offer explicit actions:
  - allow once;
  - allow always only if supported by the permission scope;
  - deny;
  - cancel.
- Route replies through service `reply_permission()`, which delegates to the same logic as `/sessions/{session_id}/permissions/{permission_id}/reply`.
- After reply, runtime must receive `permission_resolved()` and the TUI must refresh from events.

Safety rules:

- Enter does not approve by default. It opens details or focuses the permission card.
- Approval shortcuts must require a visible selected card.
- The reply response must state `permission_granting: true` only for the actual permission reply route/method, and must include the decision, scope, session id, permission id, and persisted event id.
- Denial/cancel must never start execution.

## Session Actions

Move session pane mutations behind the service layer.

Map current TUI methods:

| Current TUI method | Target service method | Required event |
| --- | --- | --- |
| `action_create_blank_session()` | `create_session()` | `session.created` or existing create-session event |
| `action_archive_selected_session()` | `archive_session()` | `session.archived` |
| `action_restore_selected_session()` | `restore_session()` | `session.restored` |
| `action_abort_selected_session()` | `abort_session()` | `harness.runtime.abort_requested` plus metadata cancellation event |
| `action_fork_selected_session()` | `fork_session()` | `session.forked` |
| `_confirm_session_hard_delete()` | `hard_delete_session()` | pre-delete tombstone event if possible; response includes deleted counts |
| `_confirm_session_rename()` | `update_session(title=...)` | `session.renamed` |
| `_activate_selected_agent_dialog_entry()` | `update_session(agent_id=...)` | `agent.selected` |
| `_persist_model_selection()` | `update_session_model()` | `session.model_validation` |

Rules:

- The TUI should still block destructive actions with visible confirmation.
- Hard delete must remain unavailable for the active in-flight session.
- Abort must clearly state whether a process was actually stopped. Metadata-only cancellation is not enough if runtime execution is active; runtime abort support must be implemented before the UI claims process stop.
- The active session pointer in `ChatSessionState` should update only after service success.

## Model And Agent Selection

Keep model selection metadata-only.

Target behavior:

- `/models` and model dialog read from service dashboard/model catalog.
- `/model <ref>` validates through the same model validation function the backend uses.
- Successful model selection persists only session metadata and validation event.
- Model selection never calls the provider, checks network, starts execution, or falls back to a hidden model.

Agent selection:

- Agent dialog lists default, native aliases (`plan`, `build`), and imported project agents from the service.
- Persist selected `agent_id` through service `update_session()`.
- Agent selection never grants tools or execution authority.

## Initialization Flow

The TUI must work gracefully before `.harness/` exists.

Current:

- `build_tui_dashboard()` returns an uninitialized projection and guidance.
- Some session actions call `SQLiteStore.open_initialized()` and fail if uninitialized.

Target:

- Read-only dashboard works before initialization.
- Prompt submit before initialization opens a visible action contract for `harness init --project .`, or a deterministic guidance response.
- `n` new session before initialization should show "Initialize project first" with exact command and no traceback.
- `/init` can become an explicit service action only if it maps to the existing CLI initialization behavior and records clear side effects.

Do not auto-initialize just because the TUI starts.

## Error Handling

Define stable UI error classes.

| Error class | User-facing state | Recovery |
| --- | --- | --- |
| `project_uninitialized` | Setup needed | show `harness init --project <root>` |
| `schema_missing_or_old` | Repair needed | show doctor/init repair command |
| `session_not_found` | Session disappeared | select next available session |
| `runtime_busy` | Prompt queued or blocked | show queue policy choices |
| `terminal_session` | Cannot continue | offer fork/new session |
| `permission_required` | Waiting approval | focus permission card |
| `provider_configuration` | Model/provider blocked | open model selector/settings |
| `provider_unavailable` | Retry or choose model | show retry status and model selector |
| `context_overflow` | Compaction attempted or needed | show compaction state |
| `event_stream_lost` | Live refresh degraded | fall back to poll |

Implementation rules:

- No raw tracebacks in normal TUI panels.
- Store schema repair messages may be shown directly if already curated.
- Error cards include equivalent CLI inspection commands.
- A failed side panel must not prevent prompt composer and session rail rendering.

## Implementation Slices

### Slice 1: Service Facade And Parity Tests

Deliverables:

- Add `HarnessAppService` in `src/harness/core_service.py` or a nearby app-service module.
- Implement read-only methods first: health, dashboard, session pane, list sessions, list messages, list events, runtime status, settings, model/provider catalogs if needed.
- Refactor local-server read routes to call service methods where practical.
- Keep existing route schemas and tests passing.

Tests:

- Service dashboard equals `build_tui_dashboard()` for initialized and uninitialized projects.
- Service session pane equals `build_session_pane_projection()`.
- Service message/event methods match local-server route payloads.
- Local-server route tests still pass.

### Slice 2: TUI Reads Through Service

Deliverables:

- Instantiate the service once in `create_harness_app()`.
- Replace direct dashboard/session-pane calls in `_dashboard_snapshot()`, `_session_pane_projection()`, and `_left_pane_projection()` with service calls.
- Keep render models unchanged as much as possible.
- Preserve current keyboard behavior and layout.

Tests:

- Existing TUI projection tests still pass.
- Add a test with a fake service proving the TUI asks for selected session dashboard state.
- Verify uninitialized dashboard does not initialize project state.

### Slice 3: Session Mutations Through Service

Deliverables:

- Move create, archive, restore, abort, fork, rename, hard delete, agent selection, and model selection through service methods.
- Ensure every mutation response includes side-effect flags and event metadata.
- Update local-server routes to use the same service methods for matching operations.

Tests:

- Each TUI session action has a service unit test and route parity test.
- Hard delete refuses active in-flight session at the TUI layer and service layer.
- Abort response distinguishes metadata cancellation from actual runtime/process stop.
- Model selection records validation event and does not start provider/network execution.

### Slice 4: Runtime Prompt Submission

Deliverables:

- Add TUI service method for natural prompt submit.
- Persist user message before runtime queueing.
- Use `SessionRuntimeManager.submit_prompt()` for execution.
- Keep deterministic slash/UI commands local.
- Keep action-contract chat flows on the existing `handle_chat_input()` path until migrated, but make the split explicit in code.

Tests:

- Prompt submit creates a user message and queues runtime work.
- Idle session starts worker.
- Busy session queues follow-up.
- Terminal session rejects with fork/new guidance.
- Provider text/delta events appear in persisted event list.
- TUI transcript projection shows user message, running status, and final assistant message.

### Slice 5: Event-Driven Refresh

Deliverables:

- Add service event subscriptions for global and session streams.
- TUI starts subscriptions on mount and swaps session subscription when active session changes.
- Event callback invalidates projection caches and updates transient transcript buffer.
- Polling interval becomes fallback, not the main live transport.

Tests:

- Publishing a session event updates the TUI dirty state.
- Switching sessions closes old subscription and replays from selected session.
- Unknown events do not crash rendering.
- Event stream loss degrades to polling and shows a compact status.

### Slice 6: Permission Cards

Deliverables:

- Add permission card projection to right pane and/or transcript.
- Add keyboard/dialog actions for allow once, deny, cancel.
- Route replies through service and runtime `permission_resolved()`.
- Render equivalent CLI command for every pending permission.

Tests:

- Pending session permission renders operation, target, risk, boundary, and command.
- Allow once changes permission status and appends resolution events.
- Deny/cancel does not resume execution as success.
- Enter alone does not approve.

### Slice 7: Prompt/Transcript Source Of Truth

Deliverables:

- Introduce `build_tui_transcript_projection()`.
- Render transcript from persisted messages/events.
- Keep `_messages` only for local notices and migration shims.
- Remove duplicate assistant output once final persisted message arrives.

Tests:

- Transcript is reconstructable after app restart.
- Streaming deltas merge with final message without duplication.
- Tool calls and permission waits render from events.
- Secret-looking content and artifact bodies are not rendered.

### Slice 8: Attached Server Mode

Deliverables:

- Add optional CLI flags for server attachment if desired:
  - `--server-url`
  - `--token`
- Implement an HTTP service client with the same method names as `HarnessAppService`.
- Use HTTP streaming for events.
- Keep embedded service as default.

Tests:

- Attach mode health probe handles unauthorized/network/schema errors.
- Attached TUI can list sessions and stream events from `harness serve`.
- Missing token fails before rendering session data.

## File-Level Work Map

Expected files to touch:

- `src/harness/core_service.py`: service facade and shared response helpers.
- `src/harness/local_server.py`: delegate route logic to service; keep HTTP/auth concerns local.
- `src/harness/tui.py`: replace direct store/backend calls with service client calls; add event-driven invalidation; update prompt path.
- `src/harness/operator_context.py`: keep projection builders pure; add transcript projection if it belongs with operator projections.
- `src/harness/session_runtime.py`: expose any missing status/abort/resume hooks needed by the service.
- `src/harness/event_broker.py`: keep subscription API stable; add small helpers only if needed.
- `src/harness/cli/main.py`: optional attached-server flags and help text after embedded parity.
- `docs/operator_guide.md`: update once the TUI is wired and no longer experimental.

Expected tests:

- `tests/test_core_service.py`
- `tests/test_local_server.py`
- `tests/test_session_runtime.py`
- `tests/test_operator_chat_path.py`
- `tests/test_orchestration_cockpit.py`
- new `tests/test_tui_backend_wiring.py`
- new `tests/test_tui_transcript_projection.py`
- new `tests/test_tui_permissions.py`

## Acceptance Criteria

The wiring is complete when these workflows pass without manual repair or split-brain state.

1. Fresh uninitialized project:
   - `harness --project .` opens.
   - Dashboard shows setup needed.
   - Session creation/prompt submit show explicit init guidance.
   - No `.harness/` directory is created by just opening the app.

2. Initialized idle project:
   - `harness init --project .`
   - `harness --project .`
   - Press `n` in session pane creates a session through service.
   - Session rail updates from event/projection.
   - Rename/archive/restore/fork work and record events.

3. Prompt runtime:
   - Type a prompt in the TUI.
   - User message persists.
   - Runtime status becomes queued/running.
   - Model/tool/runtime events render live.
   - Final assistant message persists and survives TUI restart.

4. Busy session:
   - Submit a second prompt while the first is running.
   - TUI shows queued follow-up with prompt id.
   - No duplicate workers start for the same session.

5. Permission required:
   - A session tool permission request appears as a card.
   - Allow/deny/cancel goes through service.
   - Resolution persists and runtime status updates.
   - Enter alone never grants approval.

6. Model selection:
   - `/models` displays available catalog entries.
   - `/model <ref>` validates and persists metadata.
   - No provider/network execution starts from selection.

7. Event recovery:
   - If event subscription fails, TUI reports degraded live refresh and continues polling.
   - No hard crash on unknown event payloads.

8. HTTP parity:
   - Local-server routes and embedded service return compatible payloads for sessions, messages, events, runtime status, permissions, and settings.
   - `harness serve --openapi --output json` documents the routes the TUI service client depends on.

## Smoke Checklist

Run this after each implementation slice:

```bash
python -m pytest tests/test_core_service.py tests/test_local_server.py tests/test_session_runtime.py
python -m pytest tests/test_orchestration_cockpit.py tests/test_operator_chat_path.py
harness init --project /tmp/harness-tui-smoke
harness --project /tmp/harness-tui-smoke --output json
harness serve --project /tmp/harness-tui-smoke --openapi --output json
```

Manual TUI smoke after slices 4-7:

```bash
harness --project /tmp/harness-tui-smoke
```

Check:

- session rail selection is stable;
- prompt submit does not block the UI;
- right pane shows runtime phase and next command;
- transcript updates while work is running;
- session restart reconstructs the transcript;
- permission cards require explicit action;
- no backend token, secret path content, or artifact body appears in the TUI.

## Rollout Order

Recommended order:

1. Build service facade for read projections.
2. Move TUI reads to service.
3. Move session metadata mutations to service.
4. Wire prompt submit to runtime.
5. Add event-driven refresh.
6. Add permission card actions.
7. Make persisted transcript the source of truth.
8. Add optional attached-server mode.
9. Update `docs/operator_guide.md` and smoke checklist.

This order keeps the UI useful throughout the migration and avoids broadening execution authority while backend and TUI behavior converge.
