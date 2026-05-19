# Natural-Language Operator Layer Plan

## Summary

Implement a Harness natural-language operator layer that gives the user conventional coding-agent flow while preserving Harness control-plane authority.

The operator layer should let a user say:

```text
move to the Harness repo, show me the diff, run the session tool tests, and fix the bug
```

Harness should respond by routing obvious navigation/read/test intent deterministically, using model-driven tool calls for less obvious work, pausing for approval before execution or mutation, persisting evidence before rendering output, and continuing only until the task is complete, blocked, denied, or waiting for approval.

The target is single-turn task-run autonomy, not daemon autonomy. Harness remains the authority for tools, cwd/project boundaries, policy, approvals, evidence, artifacts, sandboxing, and active-repo mutation.

## Reference Repos

The reference code is checked out locally under ignored Harness state:

```text
.harness/reference-repos/pi
.harness/reference-repos/opencode
```

Pinned references from this pull:

```text
earendil-works/pi
  branch: main
  commit: f4f0ac7adaef08f2ec750ccb4c76cd90e93922a7

anomalyco/opencode
  branch: dev
  commit: 9790a61f96771ca9fcb7b8b8d336aa8c150bb52d
```

Keep these files open while implementing:

```text
.harness/reference-repos/opencode/packages/opencode/src/session/tools.ts
.harness/reference-repos/opencode/packages/opencode/src/session/processor.ts
.harness/reference-repos/opencode/packages/opencode/src/session/prompt.ts
.harness/reference-repos/opencode/packages/opencode/src/session/prompt/default.txt
.harness/reference-repos/opencode/packages/app/src/pages/session/composer/session-permission-dock.tsx
.harness/reference-repos/opencode/packages/app/src/pages/session/composer/session-followup-dock.tsx
.harness/reference-repos/pi/packages/agent/docs/agent-harness.md
.harness/reference-repos/pi/packages/agent/src/agent-loop.ts
.harness/reference-repos/pi/packages/agent/src/harness/agent-harness.ts
.harness/reference-repos/pi/packages/agent/src/harness/types.ts
```

Use these repos as references only. Do not add either as a runtime dependency.

## Core Decision

Harness should not become a generic opencode/pi clone.

Adopt:

- opencode's smooth model-driven tool loop, session tool schema exposure, durable tool parts, prompt pressure to use tools, and concise session UX.
- pi's lifecycle shape: turn snapshots, explicit phases, pending writes, steer/follow-up/next-turn queues, before/after tool hooks, abort settlement, and save points.

Preserve:

- local-first Harness state under `.harness/`;
- typed session tools and `execute_session_tool`;
- exact permission targets for shell/test/network/mutation;
- policy snapshots and explicit approvals;
- event and artifact evidence before render;
- active-repo mutation boundaries and apply-back separation;
- no hidden provider fallback;
- no generic ambient terminal access.

## Current Harness Baseline

Harness already has a strong substrate. The plan should build on it instead of replacing it.

Current useful seams:

```text
src/harness/session_tools.py
  SessionToolDescriptor
  default_session_tool_descriptors()
  decide_session_tool_permission()
  execute_session_tool()

src/harness/session_cwd.py
  CwdResolver
  session_cwd_payload()

src/harness/chat.py
  ChatSessionState
  /pwd, /cd, /project, /workspace
  _model_chat_response()
  _try_execute_model_session_tool()
  _normalize_session_tool_request()

src/harness/local_server.py
  session route projections
  /sessions/{id}/tool-equivalent direct gateway path

src/harness/memory/sqlite_store.py
  SQLiteStore.initialize()
  schema_migrations
  event_store
  sessions/messages/parts/permissions

src/harness/memory/migrations/
  20260516_001_sessions.sql
  20260518_002_context_chunks.sql
  20260518_003_context_vectors.sql

tests/test_session_tools.py
tests/test_local_server.py
tests/test_product_spine_v0_3.py
tests/test_migrations_runner.py
```

Important baseline observations:

- `SQLiteStore.initialize()` already applies versioned migrations and then replays additive `schema.sql` as a repair pass.
- Session tools already include `pwd`, `cd`, `read`, `glob`, `grep`, `git-diff`, `repo-overview`, permission-gated `shell`, `docker-test`, web tools, skill/MCP projections, and guarded write prototypes.
- Chat can execute slash-routed session tools and can let the model request tools through the existing JSON-text `harness.tool_request/v1` loop.
- The current model loop is not yet a provider-native tool loop. It parses tool requests from text and caps calls at `MAX_CHAT_TOOL_CALLS = 3`.
- Natural language routing handles some high-level intents, but app-control phrases such as `move to /repo`, `go to repo root`, and combined tasks need a deterministic pre-router before the model.
- There is no first-class Harness turn snapshot/save-point model for chat/TUI/server prompts.

## Reference Lessons

### opencode

Use these ideas:

- `SessionTools.resolve` converts internal tool registry entries into model-visible AI SDK tools.
- Each tool execution gets a context with session id, message id, call id, abort signal, agent, model, metadata updates, and permission asks.
- Tool lifecycle updates are recorded as durable parts: pending, running, completed, error.
- The processor records text, reasoning, tool input start/delta/end, tool called, tool success/failure, step start, step finish, cost/tokens, and patch snapshots.
- The prompt aggressively tells the model to inspect, use tools, continue, verify, and keep terminal-facing output concise.
- The UI has explicit docks for permissions, questions, follow-ups, todos, terminal output, and changed files.

Do not copy:

- broad generic shell semantics;
- direct active write/edit tools without Harness apply-back policy;
- permission prompts that grant authority outside Harness policy;
- provider fallback or external network behavior that bypasses Harness config.

### pi

Use these ideas:

- `AgentHarness` owns phase, session persistence, runtime config, resources, tools, queues, pending session writes, hooks, abort, and settlement.
- A turn snapshot is created once per provider request and contains messages, resources, system prompt, model, thinking level, tools, active tools, stream options, and session id.
- Config setters may update future snapshots while a turn is active, but never mutate the in-flight provider request.
- `beforeToolCall` runs after argument preparation/validation and can block execution.
- `afterToolCall` runs after execution and can patch the model-visible result or terminate the tool batch.
- Save points occur after assistant and tool-result messages complete; pending writes flush there; the next turn snapshot is created there.
- `steer`, `followUp`, and `nextTurn` queues are drained at safe points instead of mutating in-flight turns.

Do not copy:

- pi's TypeScript runtime as a dependency;
- raw hook access that would let extensions reorder Harness evidence;
- app-level session writes without Harness event-store ordering.

## Target Architecture

```text
Harness Operator Layer
  NaturalLanguageRouter
  HarnessAgentLoop
  HarnessTurnState
  HarnessToolCallRecord
  HarnessSavePoint
  ApprovalPause/Resume
  OperatorRenderer

Harness Control Plane
  SQLiteStore and event_store
  policy engine
  approval/session permission service
  event/evidence writer
  artifact registry
  task/objective/run state
  manifest writer

Harness Execution Plane
  session tool gateway
  CwdResolver
  project/workspace resolver
  shell runner
  Docker test runner
  isolated edit/apply-back runner
```

The new layer sits above the session-tool gateway. It decides what to call; the gateway decides what is valid, allowed, persisted, and visible.

## Target UX

Read-only navigation and inspection should feel immediate:

```text
User: move to src/harness
Harness:
  cwd: src/harness
```

```text
User: show me what changed
Harness:
  Captured current diff.
  Target: .
  ...
```

Execution should pause for exact approval:

```text
User: run the session tool tests
Harness:
  Approval required to run tests:

  cwd: .
  command: python3 -m pytest tests/test_session_tools.py -q
  timeout: 120s
```

Errors should be operator-facing:

```text
The session database is missing required tables.
Run: harness doctor --repair
```

Raw SQLite errors, stack traces, and tool JSON belong in debug mode only.

## Data Models

Add these models or equivalent Pydantic types.

```python
class HarnessTurnState(BaseModel):
    schema_version: str = "harness.turn_state/v1"
    turn_id: str
    session_id: str
    project_root: str
    cwd: str
    model_profile_id: str
    backend_id: str | None = None
    agent_id: str
    workbench_id: str | None = None
    run_mode: str
    active_tools: list[str]
    effective_policy_sha256: str
    context_pack_sha256: str | None = None
    stream_options: dict[str, Any] = {}
    created_at: datetime
```

```python
class HarnessAgentPhase(str, Enum):
    IDLE = "idle"
    TURN = "turn"
    WAITING_APPROVAL = "waiting_approval"
    COMPACTION = "compaction"
    RETRY = "retry"
    PROJECT_SWITCH = "project_switch"
```

```python
class HarnessToolCallRecord(BaseModel):
    schema_version: str = "harness.tool_call/v1"
    tool_call_id: str
    turn_id: str
    session_id: str
    tool_id: str
    raw_args: dict[str, Any]
    normalized_args: dict[str, Any]
    cwd: str
    permission_state: Literal["not_required", "pending", "approved", "denied"]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: Literal["pending", "running", "completed", "failed", "blocked"]
    result_artifact_ids: list[str] = []
```

```python
class HarnessSavePoint(BaseModel):
    schema_version: str = "harness.save_point/v1"
    save_point_id: str
    turn_id: str
    session_id: str
    flushed_event_count: int
    flushed_artifact_count: int
    next_turn_state_sha256: str | None = None
    created_at: datetime
```

The critical rule from pi: config, project/cwd changes, queued messages, model changes, and pending writes created during a turn affect the next turn snapshot, not the currently running provider request.

## Natural-Language Router

Add a deterministic router before the model loop. Do not rely on the model for obvious app-control commands.

Handle these forms:

```text
move to <path>
go to <path>
switch to <path>
open project <path>
use workspace <path>
cd <path>
pwd
repo root
go back to repo root
show diff
what changed
run tests
run the <name> tests
search for <term>
find where <term>
read <file>
```

Resolution rules:

```text
If target path is a Harness project root or contains .harness:
  route to project/workspace switch.

Else if target path resolves inside active project:
  route to cd.

Else if target path is outside active project:
  return project-switch proposal or boundary rejection.

If prompt asks for shell/test/run/build:
  prepare shell or docker-test tool call and pause at approval.

If prompt combines read-only steps and one approval-required step:
  run read-only steps first, persist evidence, then pause at approval.
```

Acceptance cases:

```text
move to /Users/oscarxue/Documents/harness
  -> project switch if .harness exists

move to src/harness
  -> cd inside active project

go back to repo root
  -> cd .

show me what changed
  -> git-diff session tool

find where shell approval is implemented
  -> grep/read session tools, no shell

run the session tool tests
  -> shell approval card, no execution before approval

move to /tmp
  -> project switch proposal or clean boundary rejection
```

## Model-Driven Tool Loop

Replace the text-parsed tool-request loop with a first-class Harness agent loop when the backend supports tool calls. Keep the text-parsed loop only as a compatibility path for older local models if needed.

Canonical loop:

```text
create user message/event
create HarnessTurnState
build model context
expose SessionToolDescriptor entries as model-visible tool schemas
stream assistant output
if model emits tool calls:
  validate schema
  normalize cwd/project/path
  run before_tool_call
  execute allowed tool through execute_session_tool
  pause on approval-required tool
  persist result/evidence/artifacts
  append tool result to model-visible context
  save point
  continue unless blocked, done, or budget exhausted
else:
  persist final assistant output
  save point
  stop
```

Recommended limits:

```text
max_tool_steps_per_turn = 12
max_same_tool_same_args = 3
max_project_switches_per_turn = 1
max_shell_requests_per_turn = 3
max_wall_clock_seconds_per_turn = 600
```

Doom-loop guard:

```text
same tool id + same normalized args repeated 3 times in one turn
  -> tool error returned to model
  -> event kind: harness.agent_loop_guard.triggered
  -> no execution on the guarded call
```

## Tool Gateway Contract

Every model-visible tool call must go through the session-tool gateway.

```text
model tool call
  -> SessionToolDescriptor lookup
  -> input schema validation
  -> cwd/project/path normalization
  -> effective policy check
  -> permission decision
  -> approval gate
  -> execute_session_tool
  -> event/artifact persistence
  -> model-visible tool result
```

No chat, TUI, or server path should call bespoke helpers for read, grep, shell, cd, diff, tests, or repo overview.

Tool classes:

```text
Auto-safe:
  pwd
  cd inside active project
  read
  glob
  grep
  repo-overview
  git-diff
  policy-explain
  lsp-diagnostics static projection
  lsp-symbols static projection

Permission-required:
  shell
  docker-test
  web-fetch
  web-search
  repo-clone
  skill-load
  MCP resource/tool
  context-excluded cwd/path

Forbidden or separately approval-gated:
  active repo mutation
  apply-back
  direct write
  patch apply
  external messages
  broker/trading actions
```

Shell remains exact-capability, bounded, one-shot by default. It must not become ambient terminal access.

## Permission Model

Implement a formal before/after gate around the existing `decide_session_tool_permission()` and `execute_session_tool()` behavior.

```text
before_tool_call:
  validate descriptor exists
  validate raw args against input schema
  normalize args
  resolve cwd/project/path
  compute effective policy snapshot
  classify side effects
  check existing exact approval
  return allow | block | pending_approval

after_tool_call:
  persist result event
  register artifacts
  write redacted previews
  update manifest/run evidence
  return model-visible result
```

Approval target must include:

```text
project_root fingerprint
session_id
tool_id
normalized cwd
normalized command or operation
timeout
shell executable
env policy
network policy
sandbox profile
run mode
```

Approval reuse must fail if any target field changes.

## Cwd And Project Semantics

Keep these semantics:

```text
Session cwd:
  project-relative, durable, inherited by tools.

Project/workspace root:
  explicit attach/switch operation.

Shell cwd:
  resolved cwd for one command, not a replacement for session cwd unless exact simple `cd <path>` is routed to the cd tool.
```

Required recovery:

```text
persisted cwd missing
  -> render clean recovery message
  -> offer reset to "."
  -> do not show raw traceback
```

Boundary handling:

```text
absolute path inside active project
  -> cd or tool path allowed after symlink resolution

absolute path outside active project with .harness
  -> project switch proposal

absolute path outside active project without .harness
  -> reject or propose initialization, depending on command

symlink escape
  -> deny with path_security
```

## Save Points

Add `harness.save_point` as a first-class event.

A save point occurs after:

```text
assistant message completed
all tool calls in that assistant message completed or blocked
tool-result messages persisted
artifacts registered
pending session writes flushed
```

At each save point:

```text
refresh session cwd
refresh project status
refresh effective policy
refresh active tools
refresh model/backend state
refresh context pack if needed
emit harness.save_point
drain safe queues
create next HarnessTurnState if the model loop continues
```

This is the strongest pi mechanism to adopt. It keeps in-flight provider requests immutable while still letting operator steering and runtime changes affect the next model call in the same task run.

## Queues

Add three queue types:

```text
steer:
  User interrupts while agent is working.
  Inject at next safe point before the next assistant request.

follow_up:
  User sends another request while current work is running.
  Process after current turn would otherwise finish.

next_turn:
  Message inserted before the next user-initiated turn.
```

Mode:

```text
one-at-a-time by default
all only when explicitly configured
```

Abort:

```text
abort clears steer/follow_up queues
abort does not clear next_turn
abort flushes pending writes during settlement or failure cleanup
phase returns to idle
```

## Operator Renderer

Replace raw internal traces with concise operator output.

Bad:

```text
intent: unsupported
Ran model turn
Ran cd
no such table: sessions
```

Good:

```text
Switching project to /Users/oscarxue/Documents/harness...
Project switched.
cwd: .
```

Approval output:

```text
Captured current diff.
Approval required to run tests:

cwd: .
command: python3 -m pytest tests/test_session_tools.py -q
timeout: 120s
```

Failure output:

```text
The requested path leaves the active project.
Use /project <path> to switch roots.
```

Debug mode may include raw JSON, stack traces, and event ids. Normal mode should not.

## PR Sequence

### PR 1: Schema Bootstrap Audit And Runtime Migration Hardening

Goal: make `no such table: sessions` impossible before any chat/TUI/server session tool execution.

Current state: `SQLiteStore.initialize()` already applies migrations and repairs missing `IF NOT EXISTS` tables. The hardening task is to prove every runtime entry point initializes or opens through a migration-safe store boundary.

Deliverables:

```text
Audit all SQLiteStore(project_root) call sites used by chat/TUI/server/session tools.
Add a migration-safe store factory if needed.
Run migrations before chat session attach.
Run migrations before TUI session attach.
Run migrations before local-server /sessions/{id}/tool execution.
Add doctor check for session schema.
Add doctor --repair for missing sessions/session_messages/session_parts/event_store.
Convert sqlite schema errors into operator-facing migration errors.
```

Tests:

```text
Old DB without sessions table -> chat /pwd succeeds after migration.
Old DB without sessions table -> model cd succeeds after migration.
Old DB without sessions table -> local-server session tool route succeeds after migration.
Old DB without event_store -> repair path recreates table.
No raw "no such table" appears in chat/TUI/server output.
```

Primary files:

```text
src/harness/memory/sqlite_store.py
src/harness/chat.py
src/harness/tui.py
src/harness/local_server.py
tests/test_migrations_runner.py
tests/test_product_spine_v0_3.py
tests/test_session_tools.py
tests/test_local_server.py
```

### PR 2: NaturalLanguageRouter

Goal: deterministic app-control routing before the model.

Deliverables:

```text
NaturalLanguageRouter module.
PathIntent parser.
Project-root detector.
Cwd-change detector.
Diff/test/search/read detectors.
Clean boundary rejection/proposal responses.
Slash command parity tests.
```

Suggested module:

```text
src/harness/natural_language_router.py
```

Integration:

```text
src/harness/chat.py::_handle_intent()
TUI chat submit path
local-server /sessions/{id}/prompt once added
```

Tests:

```text
move to /Users/oscarxue/Documents/harness -> project switch if .harness exists
move to src/harness -> cd
go back to repo root -> cd .
show me what changed -> git-diff
find where shell approvals are handled -> grep/read path
run session tool tests -> shell approval request
move to /tmp -> project-switch proposal or boundary rejection
```

### PR 3: Harness Turn State And Phase Model

Goal: give chat/TUI/server prompt handling a lifecycle model before adding a larger loop.

Deliverables:

```text
HarnessAgentPhase.
HarnessTurnState.
turn_id generation.
turn_state event persistence.
phase transitions.
wait_for_idle behavior.
busy handling for structural operations.
operator-visible status projection.
```

Suggested modules:

```text
src/harness/operator_loop.py
src/harness/operator_models.py
```

Tests:

```text
prompt while idle enters turn phase.
phase returns to idle after success.
phase returns to idle after failure.
approval pause sets waiting_approval.
abort clears running state.
second structural prompt while turn active is rejected or queued according to mode.
```

### PR 4: Provider-Native Model-Driven Tool Loop

Goal: expose session tool descriptors as provider-native tools and keep looping until final, blocked, approval-required, or guarded.

Deliverables:

```text
HarnessAgentLoop.
model-visible tool schema generation from SessionToolDescriptor.
tool-call event handling.
tool-result feedback into model context.
max tool steps per turn.
same-tool/same-args guard.
assistant final output persistence.
compatibility fallback for text-parsed harness.tool_request/v1 if needed.
```

Current loop to replace or wrap:

```text
src/harness/chat.py::_model_chat_response()
src/harness/chat.py::_try_execute_model_session_tool()
```

Tests:

```text
model calls grep then read then final answer.
model calls git-diff then final answer.
model emits unknown tool -> model-visible tool error.
model repeats same tool/args 3 times -> doom-loop guard.
model requests shell -> approval pause, no process.
```

### PR 5: Before/After Tool Gates

Goal: formalize policy and evidence hooks around all model-visible tool execution.

Deliverables:

```text
before_tool_call gate.
after_tool_call evidence hook.
HarnessToolCallRecord.
approval pause object.
resume pending tool call after approval.
deny feedback returned as tool error.
one-shot permission consumption verified.
```

Policy behavior:

```text
read/glob/grep/git-diff -> auto if inside policy.
cd -> auto if inside project and allowed path.
shell -> exact approval required.
docker-test -> approval required unless policy explicitly allows.
context-excluded cwd -> approval required.
secret-like cwd/path -> denied.
active repo mutation -> apply-back boundary.
```

Tests:

```text
same command same cwd same timeout approved once -> executes once.
same command different cwd -> new approval.
same command different timeout -> new approval.
same command different shell executable -> new approval.
denied shell call returns model-visible denial.
context-excluded read needs permission or denial according to policy.
```

### PR 6: Save Points And Pending Writes

Goal: adopt pi's save-point boundary as a Harness event and state refresh point.

Deliverables:

```text
harness.save_point event.
pending event/artifact/session write buffer if needed.
flush after tool-result batch.
refresh turn state before next model request.
queue drain at save points.
save-point status projection.
```

Tests:

```text
save_point emitted after assistant only.
save_point emitted after assistant plus tool results.
cwd changed during turn affects next model request.
policy changed during turn affects next model request.
queued steer message appears before next assistant request.
pending writes flush after agent-emitted messages.
```

### PR 7: Chat/TUI/Server Integration

Goal: route natural-language prompt paths through the same operator loop.

Deliverables:

```text
chat routes natural language through NaturalLanguageRouter + HarnessAgentLoop.
TUI routes natural language through same loop.
HTTP /sessions/{id}/prompt uses same loop.
existing /sessions/{id}/tool remains direct tool execution.
operator renderer replaces raw traces.
debug mode preserves raw traces.
```

Route split:

```text
/sessions/{id}/prompt
  natural-language operator loop

/sessions/{id}/tool
  direct tool execution

/sessions/{id}/approval/{approval_id}
  approve/deny/resume

/sessions/{id}/status
  read-only projection
```

Tests:

```text
CLI/chat prompt and local-server prompt produce equivalent events.
TUI transcript renders the same persisted events.
Direct /tool route does not invoke model loop.
Debug mode can show tool JSON.
Normal mode hides raw JSON/tracebacks.
```

### PR 8: Shell/Test Approval UX

Goal: make approval pause and resume feel deliberate and exact.

Deliverables:

```text
approval card renderer in chat.
approval card renderer in TUI.
local-server permission projection.
approve once.
deny.
deny with feedback.
resume pending call.
one-shot expiry enforcement.
```

Acceptance:

```text
User: run the session tool tests
Expected: shell approval card, no execution before approval.

Approve:
Expected: shell executes once, result persisted, approval consumed.

Repeat same command:
Expected: new approval unless always policy explicitly allows.
```

### PR 9: Error Recovery And Doctor

Goal: make common operator failures actionable.

Deliverables:

```text
doctor checks:
  schema current
  sessions table exists
  event_store table exists
  session cwd valid
  project root initialized
  tool registry valid
  shell config valid
  docker config optional
  artifact dir writable

doctor --repair:
  run migrations
  reset invalid cwd to "."
  rebuild session status projection
```

Tests:

```text
missing cwd -> recovery prompt.
invalid cwd -> doctor repair resets to ".".
missing sessions table -> doctor reports repairable.
permission database intact after repair.
artifact dir unwritable -> clear diagnostic.
```

### PR 10: Task-Run Autonomy Bridge

Goal: connect the operator loop to the v0.3 task queue after single-turn tool autonomy works.

Deliverables:

```text
task -> operator loop.
task attempt -> turn/run linkage.
task expected outputs -> artifact validation.
waiting_approval status on approval pause.
resume task after approval.
idempotency key per task attempt.
```

Tests:

```text
task pauses waiting_approval on shell request.
approval resumes exactly one pending tool call.
task records turn/run/artifact linkage.
task retry uses idempotency key.
abort cleans up phase and pending calls.
```

## Harness Operator Prompt

Add a Harness-specific operator prompt. The prompt guides behavior only; it does not grant authority.

```text
You are the Harness operator agent.
Use available session tools to satisfy the user's request.
Prefer read, glob, grep, repo-overview, and git-diff before shell.
Use cd/project tools for navigation instead of shell cd.
Do not claim work is done unless tool evidence supports it.
When a tool requires approval, request approval and stop.
Do not bypass Harness policy.
Do not mutate the active repo except through approved Harness mutation/apply-back paths.
If the user asks for a plan only, produce a plan and do not execute.
If the user asks for work, use tools to perform the work within the current turn until complete, blocked, or approval is needed.
Keep operator-facing output concise.
```

## Test Matrix

Core natural language:

```text
move to src/harness
move to /Users/oscarxue/Documents/harness
go back to repo root
show me what changed
find where shell permissions are handled
read src/harness/session_tools.py
run the session tool tests
inspect diff and run tests
fix the failing cwd migration bug
```

Safety:

```text
move to /tmp
cd symlink_to_outside
read symlink_to_secret
shell cd /tmp
shell command timeout changed after approval
same command different cwd after approval
same command different shell executable after approval
context-excluded cwd
secret-like cwd
```

Lifecycle:

```text
user sends steering message during tool execution
user queues follow-up during turn
tool result causes another model turn
approval pause resumes exactly one pending tool call
denial is returned to model as tool error
abort cleans up phase and pending tool calls
save_point emitted after every assistant/tool-result batch
```

Persistence:

```text
old DB migrates
save_point event emitted
tool calls persisted before render
artifacts registered with sha256/size
cwd survives chat restart
invalid cwd gets clean recovery prompt
no raw sqlite errors in UI
```

Renderer:

```text
normal mode hides stack traces
normal mode hides raw tool JSON
debug mode exposes event ids and raw tool payloads
approval cards show exact target
permission denial is short and actionable
```

## Near-Term Definition Of Done

The milestone is complete when this flow works:

```text
harness chat --project /Users/oscarxue/Documents/harness

User: move to src/harness
Result: cwd changes, persisted, no raw internals.

User: show me what changed
Result: git-diff runs automatically and summarizes evidence.

User: find where shell approval is implemented
Result: grep/read tools run automatically, no shell.

User: run the session tool tests
Result: shell approval appears, no execution before approval.

User approves
Result: tests run, stdout/stderr/exit code persisted, response summarizes result.

User: move to /tmp
Result: clean project-switch proposal or boundary rejection.

Old .harness DB
Result: migration/repair path, no raw SQLite errors.
```

## Non-Goals

Do not include these in the first operator-loop milestone:

```text
generic unrestricted shell
ambient PTY terminal control
direct active-repo write from model text
provider fallback
remote daemon autonomy
MCP tool execution without Harness permission envelope
web search/fetch without explicit network policy
hosted model escalation without approval
session sharing beyond local projections
```

## Implementation Order

```text
1. Harden schema/migration entry points.
2. Add deterministic NaturalLanguageRouter.
3. Add turn state and phase model.
4. Add provider-native model-driven tool loop.
5. Add before/after tool gates.
6. Add save points and queues.
7. Integrate chat/TUI/server UX.
8. Polish shell/test approval UX.
9. Add doctor recovery.
10. Bridge to task queue.
```

This order keeps the first wins practical: migration reliability, navigation, diff/search/read behavior, and exact shell approvals. The larger model loop and task bridge come after the control-plane boundaries are explicit and testable.
