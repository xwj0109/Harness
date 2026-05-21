# Backend Frontend Wiring Plan

Status: draft

Goal: make the Harness local backend and a browser/desktop frontend work as one reliable local app: start the server, open the UI, connect with a bearer token, inspect project state, create and resume sessions, send prompts through the correct Harness execution boundary, stream events, resolve permissions, and recover from failures without hidden side effects.

## Current State

Harness already has a local HTTP backend in `src/harness/local_server.py` and a CLI entrypoint in `src/harness/cli/main.py`.

Implemented backend foundations:

- `harness serve --project . --host 127.0.0.1 --port 8765` starts the local server after project initialization.
- `harness serve --openapi --output json` emits the backend contract.
- API routes require `Authorization: Bearer <token>`.
- CORS defaults to `http://127.0.0.1` and can be overridden with `HARNESS_SERVER_CORS_ORIGIN`.
- `/health`, `/openapi.json`, `/server/lifecycle`, `/sessions`, `/sessions/{session_id}`, `/sessions/{session_id}/messages`, `/sessions/{session_id}/events`, `/sessions/{session_id}/events/stream`, `/event`, `/permission`, `/question`, `/api/provider`, `/api/model`, `/tools`, `/commands`, `/vcs/status`, `/files/status`, `/web/client`, and related projections are already present.
- Session, message, event, permission, and question state is append-only or projection-based.
- `/sessions/{session_id}/prompt` routes a prompt through the shared Harness operator loop.
- `/sessions/{session_id}/prompt_async` appends a user prompt and enters the process-local runtime queue.
- `/api/session/{session_id}/prompt` is OpenCode v2-compatible append-only prompt persistence and does not start assistant execution.
- `/event?live=1` and `/sessions/{session_id}/events/stream?live=1` expose live SSE streams.

Current frontend gap:

- `/web/client` reports `client_available: false`, `static_assets_served: false`, and `open_supported: false`.
- There is no checked-in browser frontend package, static web asset package, or typed frontend API client.
- The local server does not yet serve `/web` assets.

Important integration constraint:

- Native browser `EventSource` cannot send an `Authorization` header. Since Harness API/SSE routes require bearer auth, the frontend must consume SSE using `fetch` plus a streaming parser, or a small library built on `fetch`, not native `EventSource`.
- Do not put bearer tokens in query parameters. Query strings end up in browser history, logs, proxies, and screenshots.

## Product Contract

The frontend is a supervised operator cockpit over Harness state, not an independent execution surface.

Every frontend interaction must fit one of these categories:

- Read-only projection: dashboards, sessions, files, providers, models, tools, commands, status, events, and settings.
- Safe UI-local state: selected session, collapsed sections, theme, filters, draft prompt text.
- Explicit Harness action: create session, send operator prompt, enqueue async prompt, reply to permission, answer question, abort session, fork session, update title, append correction/retraction.
- Fail-closed unsupported operation: worktree mutation, config write, PTY websocket, command execution, provider auth mutation, desktop launch, package update, MCP connect, shell execution without exact session-tool permission.

The frontend must never:

- Infer or grant permissions.
- Start hidden provider/model execution.
- Store or reveal backend credentials.
- Use `/api/session/{session_id}/prompt` as the primary send path when the operator expects a response.
- Treat fail-closed `501` responses as app crashes.
- Read artifact bodies, secret paths, or file contents outside the backend's redacted routes.

## Target Architecture

Use a two-layer implementation.

1. Development frontend:
   - Add `frontend/` as a Vite + React + TypeScript app.
   - Run it at `http://127.0.0.1:5173` during development.
   - Configure the backend for development with `HARNESS_SERVER_CORS_ORIGIN=http://127.0.0.1:5173`.

2. Packaged frontend:
   - Build the frontend into static assets.
   - Copy build output into a Python package directory such as `src/harness/web_static/`.
   - Serve `GET /web` and `GET /web/*` from the local server.
   - Keep API routes under their existing paths and do not let the SPA fallback swallow `/api`, `/sessions`, `/event`, `/permission`, `/question`, `/tools`, `/commands`, `/files`, `/vcs`, or `/openapi.json`.
   - Update Python packaging so `web_static/**` files are included in wheels.

Static asset auth rule:

- Serve static `/web` assets without bearer auth because a browser cannot attach a bearer header to the first page navigation.
- Do not embed secrets, project state, the generated bearer token, file contents, or session data in `index.html`.
- Require bearer auth for every JSON, SSE, and mutation route.
- The frontend connection screen asks for `server_url` and `token`, stores them in local browser storage only after a successful `/health` probe, and lets the operator clear them.

## Frontend Modules

Create these frontend modules.

| Module | Responsibility |
| --- | --- |
| `frontend/src/api/client.ts` | Base URL normalization, bearer header injection, JSON requests, error decoding, abort support, response metadata. |
| `frontend/src/api/sse.ts` | `fetch`-based SSE reader with bearer auth, `Last-Event-ID`, heartbeat handling, reconnect/backoff, event dispatch. |
| `frontend/src/api/types.ts` | Handwritten minimal TypeScript contracts at first, generated from OpenAPI later. |
| `frontend/src/state/server.ts` | Connection state, token storage, health, lifecycle, reconnect status. |
| `frontend/src/state/sessions.ts` | Session list/detail/message/event caches and invalidation. |
| `frontend/src/state/catalogs.ts` | Providers, models, tools, commands, agents, workspaces, files, VCS status, settings. |
| `frontend/src/state/permissions.ts` | Pending permission/question cards and replies. |
| `frontend/src/ui/AppShell.tsx` | Dense cockpit layout: session rail, transcript, prompt composer, right-side status/safety panels. |
| `frontend/src/ui/SessionView.tsx` | Transcript, message parts, runtime status, events, pending permission/question cards. |
| `frontend/src/ui/PromptComposer.tsx` | Sends through the selected execution mode with clear disabled/error states. |
| `frontend/src/ui/ActionCard.tsx` | Explicit action contracts before permission replies, shell/tool actions, abort, fork, retraction, correction. |

Preferred runtime libraries:

- React + TypeScript for the UI.
- TanStack Query or a small local query cache for JSON projections.
- A tiny local SSE parser or `@microsoft/fetch-event-source`; if adding a dependency is undesirable, implement the parser locally because the SSE grammar needed here is small.
- No router is required for the first usable app. Keep session selection in local state and URL hash only.

## Backend Changes

### Slice 1: Serve The Web App

Backend work:

- Add `src/harness/web_static/` with a placeholder `index.html` in the first backend slice.
- Add package data for `harness.web_static` in `pyproject.toml`.
- In `local_server.py`, add static handlers before JSON route fallback:
  - `GET /web` -> `index.html`
  - `GET /web/` -> `index.html`
  - `GET /web/assets/*` -> static asset by path
  - unknown `GET /web/*` -> `index.html` for SPA fallback
- Protect static file lookup with `resolve()` and require the resolved path to remain under `web_static`.
- Serve correct content types through `mimetypes`.
- Add conservative cache headers:
  - `index.html`: `Cache-Control: no-store`
  - hashed assets: `Cache-Control: public, max-age=31536000, immutable`
  - non-hashed assets: `Cache-Control: no-cache`
- Keep static assets unauthenticated.
- Keep all non-static API routes authenticated.

Contract update:

- Change `/web/client` to report:
  - `client_available: true`
  - `static_assets_served: true`
  - `client_url: http://127.0.0.1:8765/web`
  - `requires_running_server: true`
  - `permission_granting: false`
- Leave `/web/open` fail-closed until there is a deliberate local browser-open action. A static UI being available does not require the backend to launch a browser process.

Tests:

- `GET /web` returns HTML without `Authorization`.
- `GET /web/assets/<asset>` returns the asset and a content type.
- path traversal under `/web/assets` is rejected.
- `/web/client` reports static availability.
- `/health` still rejects missing/invalid bearer auth.
- `/sessions` still rejects missing/invalid bearer auth.

### Slice 2: Stabilize CORS And Dev Setup

Backend work:

- Keep same-origin packaged `/web` as the default production path.
- For Vite development, document:

```bash
harness init --project .
export HARNESS_SERVER_TOKEN=dev-token
export HARNESS_SERVER_CORS_ORIGIN=http://127.0.0.1:5173
harness serve --project . --host 127.0.0.1 --port 8765 --token "$HARNESS_SERVER_TOKEN"
```

- Confirm preflight `OPTIONS` includes:
  - `Access-Control-Allow-Origin: http://127.0.0.1:5173`
  - `Access-Control-Allow-Headers: Authorization, Content-Type`
  - `Access-Control-Allow-Methods: GET, POST, PUT, PATCH, DELETE, OPTIONS`

Tests:

- `OPTIONS /health` succeeds without bearer auth.
- CORS origin follows `HARNESS_SERVER_CORS_ORIGIN`.
- API auth behavior is unchanged after CORS changes.

### Slice 3: Make OpenAPI Consumable

Backend work:

- Preserve `harness.local_server.openapi/v1`.
- Add enough response schema detail for frontend-critical routes:
  - `LocalServerError`
  - health
  - server lifecycle
  - web client
  - sessions list/detail/status
  - messages and parts
  - events
  - prompt responses
  - permission list/reply
  - question list/reply/reject
  - providers/models/tools/commands
- Keep schema additions backward-compatible.

Frontend work:

- Start with handwritten minimal types only for critical routes.
- Add an OpenAPI codegen step after the route responses are sufficiently detailed.
- Treat generated types as compile-time help, not as runtime trust. Runtime still validates `ok`, `schema_version`, and `error_code`.

Tests:

- `harness serve --openapi --output json` includes every route the frontend imports.
- Type generation, once added, runs in CI.

## Frontend Connection Flow

The first screen is a connection view, not a marketing page.

Fields and actions:

- Server URL: default `http://127.0.0.1:8765`.
- Bearer token: empty by default.
- Connect button.
- Clear saved connection button when a connection exists.

On connect:

1. Normalize `server_url` by removing a trailing slash.
2. Call `GET /health` with `Authorization: Bearer <token>`.
3. If successful, call `GET /server/lifecycle`, `GET /web/client`, and `GET /openapi.json`.
4. Store `{server_url, token}` in local browser storage.
5. Enter the cockpit.

Connection failure states:

- `401 unauthorized`: show token-specific error.
- network error: show server reachability error and the start command.
- schema mismatch: show unsupported backend version warning but allow read-only inspection only if critical routes are present.
- CORS failure in dev: show the exact `HARNESS_SERVER_CORS_ORIGIN=http://127.0.0.1:5173` fix.

## Cockpit Boot Sequence

After connection, load projections in this order:

1. `GET /health`
2. `GET /server/lifecycle`
3. `GET /sessions`
4. `GET /sessions/status`
5. `GET /api/provider`
6. `GET /api/model`
7. `GET /tools`
8. `GET /commands`
9. `GET /vcs/status`
10. `GET /files/status`
11. `GET /settings/tui`
12. `GET /permission`
13. `GET /question`

Render even if non-critical projections fail. The app should be usable when one panel fails.

Critical projections:

- health
- sessions
- selected session detail/messages/status

Non-critical projections:

- providers
- models
- tools
- commands
- VCS/file status
- settings
- workspace/desktop/distribution status

## Session Flow

### Create Session

Use:

```http
POST /sessions
Authorization: Bearer <token>
Content-Type: application/json

{
  "title": "Fix failing tests",
  "prompt": "Fix the failing tests",
  "agent_id": "plan",
  "model": "gpt-5.5"
}
```

Frontend behavior:

- Create a session with an optional first prompt.
- Select the new session immediately.
- Refresh `/sessions`, `/sessions/{id}`, `/sessions/{id}/messages`, and `/sessions/{id}/events`.
- Show the backend flags directly: `execution_started`, `provider_execution_started`, `permission_granting`, and `no_hidden_fallback`.

### Inspect Session

Use:

- `GET /sessions/{session_id}`
- `GET /sessions/{session_id}/status`
- `GET /sessions/{session_id}/messages?limit=100`
- `GET /sessions/{session_id}/events?limit=200`
- `GET /sessions/{session_id}/permissions`
- `GET /sessions/{session_id}/questions`
- `GET /sessions/{session_id}/todos`

Frontend behavior:

- Transcript renders immutable messages and parts.
- Event rail renders persisted events separately from chat messages.
- Permission/question cards render above the prompt composer while pending.
- Session status controls send-button availability.

### Send Prompt

There are three distinct send paths. The frontend must label and use them correctly.

| User intent | Backend route | Expected behavior |
| --- | --- | --- |
| Operator prompt through Harness chat/action router | `POST /sessions/{session_id}/prompt` | Runs the shared operator loop and returns `harness.session_prompt_response/v1`. |
| Native async runtime turn | `POST /sessions/{session_id}/prompt_async` | Appends user prompt and accepts it into runtime queue. Stream/poll for assistant events. |
| OpenCode v2 compatibility append-only prompt | `POST /api/session/{session_id}/prompt` | Persists user prompt only. Does not start assistant execution. |

Default frontend behavior:

- The primary send button uses `POST /sessions/{session_id}/prompt`.
- Add an explicit "enqueue async" mode only after the runtime provider adapter path is configured and tested end to end.
- Never use `/api/session/{session_id}/prompt` for the primary chat composer.

`POST /sessions/{session_id}/prompt` request:

```json
{
  "prompt": "Summarize this repo and suggest the next safe step"
}
```

Accepted response handling:

- If `ok: true`, append/refresh transcript and events.
- If `permission_required: true`, display `approval_card` and refresh `/permission`.
- If `model_execution_started: true`, start or keep SSE active.
- If `ok: false`, render the returned `title`, `lines`, and `event_sequence` as a recoverable operator response.

`POST /sessions/{session_id}/prompt_async` request:

```json
{
  "agent": "plan",
  "parts": [
    { "type": "text", "text": "Continue the previous plan" }
  ],
  "model": "gpt-5.5"
}
```

Async response handling:

- Check `async_accepted`.
- Read `runtime.phase`, `turn_id`, `execution_started`, and `provider_execution_started`.
- Subscribe to session SSE.
- Poll `POST /api/session/{session_id}/wait` with a small timeout when SSE is unavailable.
- Refresh messages when `model.message_delta`, `model.completed`, or `harness.turn.finished` arrives.

## Streaming Flow

Implement a `fetch`-based SSE client.

Global stream:

```http
GET /event?live=1
Authorization: Bearer <token>
Accept: text/event-stream
```

Session stream:

```http
GET /sessions/{session_id}/events/stream?live=1
Authorization: Bearer <token>
Accept: text/event-stream
Last-Event-ID: <last_seen_seq>
```

SSE client requirements:

- Use `fetch` so `Authorization` can be sent.
- Parse `id:`, `event:`, and multi-line `data:` fields.
- Preserve `lastEventId` per stream.
- Reconnect with exponential backoff capped at 10 seconds.
- Treat `harness.heartbeat` as a keepalive, not a visible event.
- Stop stream on logout/token clear.
- On `401`, stop reconnecting and show disconnected state.
- On malformed event data, log a client-local error and keep the stream alive.

Event handling:

| Event kind | Frontend action |
| --- | --- |
| `server.connected` / `harness.ready` | Mark stream connected. |
| `harness.heartbeat` | Update last heartbeat timestamp. |
| `session.created`, `session.updated`, `session.archived` | Invalidate sessions and selected session detail. |
| `message.appended`, `part.appended`, `model.message_delta` | Invalidate or patch transcript. |
| `model.completed`, `harness.turn.finished` | Refresh messages, status, events, permissions. |
| `harness.runtime.permission_waiting` | Refresh permissions and show action card. |
| unknown event | Add to event rail and refresh selected projections conservatively. |

Fallback polling:

- If SSE fails after repeated retries, poll selected-session projections every 2 seconds while the session is running or waiting on permission.
- Poll every 15 seconds while idle.
- Stop polling when disconnected.

## Permission And Question Flow

Pending permission sources:

- `GET /permission`
- `GET /sessions/{session_id}/permissions`
- `GET /sessions/{session_id}/permissions/snapshot`

Reply route:

```http
POST /permission/{permission_id}/reply
Authorization: Bearer <token>
Content-Type: application/json

{
  "decision": "approve",
  "reason": "Allow exact read-only command for test inspection"
}
```

Question routes:

- `GET /question`
- `POST /question/{question_id}/reply`
- `POST /question/{question_id}/reject`

Frontend requirements:

- Permission cards must display requested operation, target, cwd, timeout, network expectation, filesystem expectation, and exact approval scope when present.
- Approval and denial buttons are explicit action buttons, never automatic.
- After reply, refresh permission lists, selected session status, events, and messages.
- Permission replies must not be batched unless the backend adds an explicit batch route.

## Error Model

All API errors should be normalized into one frontend shape:

```ts
type HarnessApiError = {
  status: number
  errorCode: string
  message: string
  schemaVersion?: string
  permissionGranting?: false
  raw?: unknown
}
```

Handling rules:

- `401`: clear active connection state, keep saved server URL, ask for token again.
- `404`: show route-specific not-found state, not a full app crash.
- `413`: show request body limit from `X-Harness-Max-Request-Body-Bytes`.
- `501`: render as "unsupported by this Harness phase" with the backend `error` string.
- network/CORS failure: show reconnect controls and dev CORS hint.
- schema mismatch: degrade panel to raw JSON inspector if safe.

## UI Layout

Use a dense operator cockpit.

Primary regions:

- Left rail: connection status, project name/root, session list, create session button.
- Center: selected session transcript, event status strip, prompt composer.
- Right rail: Now, Safety, Pending, Project, Models, Tools, VCS, Files, Commands.
- Bottom or collapsible drawer: raw event rail and request diagnostics.

Required visible states:

- Connected/disconnected.
- Server URL.
- Selected project root.
- Current session status.
- Runtime phase.
- Active turn/run/task ids when present.
- Pending permission/question count.
- Last event id and stream status.
- Whether the last prompt started execution or was append-only.

Do not use a marketing homepage. The first viewport should be the actual cockpit or connection form.

## Security Requirements

- Never put bearer tokens in URLs.
- Never send bearer tokens to non-Harness origins.
- Never log bearer tokens in frontend diagnostics.
- Redact `Authorization` before showing request debug panels.
- Static `/web` must contain no project data.
- API calls must reject unknown origins unless explicitly configured for dev.
- Frontend must render backend redaction state; it must not try to recover redacted content.
- Frontend must treat backend `permission_granting: false` as a safety signal, not as authority to proceed.
- All side-effecting controls must be wired to explicit backend action routes and show the returned action/evidence payload.

## Verification Plan

### Backend Tests

Run:

```bash
python -m pytest tests/test_local_server.py -q
```

Add tests for:

- unauthenticated `GET /web` static load
- authenticated API still enforced
- static path traversal rejected
- `/web/client` availability flags
- packaged asset inclusion
- CORS dev origin
- SSE auth rejection
- SSE live stream with bearer auth

### Frontend Unit Tests

Add tests for:

- `HarnessClient` bearer header injection
- JSON error normalization
- URL normalization
- `fetch` SSE parser with multi-line data
- reconnect/backoff behavior
- token clearing on `401`
- route selection for primary prompt vs append-only prompt

### Integration Tests

Use Playwright against the local server.

Happy path:

1. Start initialized Harness project.
2. Start `harness serve` with fixed token.
3. Open `/web`.
4. Connect with token.
5. See project/session cockpit.
6. Create a session.
7. Send a prompt through `/sessions/{id}/prompt`.
8. See transcript/events update.
9. Refresh browser.
10. Session state remains visible from backend persistence.

Async path:

1. Create session.
2. Enqueue prompt through `/sessions/{id}/prompt_async`.
3. Confirm stream connects with bearer auth.
4. Confirm runtime/status/events update.
5. Confirm no hidden fallback is reported if provider adapter is unavailable.

Permission path:

1. Trigger a backend route that creates a pending permission.
2. See pending card.
3. Deny it.
4. See session/event state update.
5. Verify no permission remains pending.

Failure path:

1. Connect with bad token.
2. See `401` state.
3. Correct token.
4. Stop server.
5. See disconnected state and reconnect loop.
6. Restart server.
7. Reconnect without losing selected session id.

### Manual Smoke Commands

Backend:

```bash
harness init --project .
export HARNESS_SERVER_TOKEN=dev-token
harness serve --project . --host 127.0.0.1 --port 8765 --token "$HARNESS_SERVER_TOKEN"
```

Health:

```bash
curl -sS \
  -H "Authorization: Bearer dev-token" \
  http://127.0.0.1:8765/health
```

Client status:

```bash
curl -sS \
  -H "Authorization: Bearer dev-token" \
  http://127.0.0.1:8765/web/client
```

Create session:

```bash
curl -sS \
  -H "Authorization: Bearer dev-token" \
  -H "Content-Type: application/json" \
  -d '{"title":"Frontend smoke","prompt":"Show project status"}' \
  http://127.0.0.1:8765/sessions
```

Stream session events with bearer auth:

```bash
curl -N \
  -H "Authorization: Bearer dev-token" \
  -H "Accept: text/event-stream" \
  "http://127.0.0.1:8765/event?live=1"
```

## Implementation Order

### Milestone 1: Usable Static Shell

- Add static `/web` serving.
- Update `/web/client`.
- Add backend tests.
- Add a minimal frontend connection page.
- Verify same-origin `/web` loads.

Acceptance:

- Operator can run `harness serve`, open `http://127.0.0.1:8765/web`, enter the token, and see server health.

### Milestone 2: Read-Only Cockpit

- Add frontend API client.
- Load health, lifecycle, sessions, catalogs, VCS/file status, permissions, and questions.
- Render session list and selected session transcript.
- Add resilient panel-level errors.

Acceptance:

- Operator can inspect project and sessions without using CLI commands.
- Unsupported backend features render as disabled/fail-closed states, not broken UI.

### Milestone 3: Prompt Loop

- Wire create-session and primary prompt send through `/sessions/{session_id}/prompt`.
- Add event refresh after prompt responses.
- Add prompt composer disabled states for terminal sessions and disconnected server.

Acceptance:

- Operator can create a session, send a prompt, see the Harness operator response, and inspect returned event/evidence fields.

### Milestone 4: Live Updates

- Add `fetch` SSE client.
- Connect global stream after login.
- Connect selected-session stream after session selection.
- Add fallback polling.

Acceptance:

- Messages/events/status update without manual refresh while the server is running.
- Frontend reconnects after transient network/server interruptions.

### Milestone 5: Permissions And Questions

- Render pending permission/question cards.
- Wire reply/reject routes.
- Refresh state after replies.
- Add explicit safety copy from backend payloads.

Acceptance:

- Operator can resolve pending approvals/questions in the UI and see session state advance.

### Milestone 6: Async Runtime Mode

- Decide whether local server should configure a real `ProviderAdapter` or keep async runtime as explicit experimental mode.
- If enabled, wire `/sessions/{session_id}/prompt_async` to a configured provider adapter without hidden fallback.
- Add runtime status and wait/polling UI.

Acceptance:

- Async prompt mode either works end to end with visible provider/runtime evidence, or is disabled with a clear "provider runtime unavailable" state.

### Milestone 7: Packaging

- Build frontend assets during release packaging.
- Include `web_static/**` in wheel.
- Add packaging smoke for static assets.
- Update `README.md`, `docs/operator_guide.md`, and `docs/smoke_checklist.md`.

Acceptance:

- A wheel install can run `harness serve`, open `/web`, connect, inspect sessions, and run the prompt loop.

## Definition Of Flawless For This Phase

The application is considered wired correctly when all of these are true:

- A fresh operator can initialize a project, start the backend, open `/web`, enter a token, and see the cockpit.
- The frontend uses the backend OpenAPI/session contracts instead of hardcoded mock data.
- Static assets load without auth, but every stateful API/SSE request requires bearer auth.
- The main prompt composer uses `/sessions/{session_id}/prompt`, not the append-only compatibility route.
- SSE works in the browser with bearer auth through `fetch` streaming.
- The UI never crashes on `401`, `404`, `413`, `501`, CORS, server restart, malformed JSON, or unknown event kinds.
- Read-only panels remain usable when non-critical projections fail.
- Permissions and questions are explicit visible action cards.
- Unsupported operations remain visibly fail-closed.
- No token, secret, backend config credential, or redacted content is exposed by the frontend.
- Tests cover static serving, auth, CORS, critical API client behavior, SSE parsing, prompt route selection, and the end-to-end browser happy path.

## Open Decisions

- Frontend stack: default to Vite + React + TypeScript unless the team wants a no-build static app.
- Static route auth: default to unauthenticated static `/web` assets and authenticated API data.
- Token persistence: default to local browser storage for local app usability, with a visible clear action.
- Async runtime: default primary UX to `/sessions/{session_id}/prompt` until provider-backed async runtime is wired and tested.
- `/web/open`: keep fail-closed until a separate explicit browser-launch command is approved.
- OpenAPI typing: start handwritten for critical contracts, then generate once response schemas are detailed enough.
