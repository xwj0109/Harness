# Codex-Like Live Run Experience Plan

## Summary

Add a Codex CLI-style live execution experience to Harness without exposing raw hidden chain-of-thought. The product target is live execution transparency: streamed model output where available, streamed tool and procedure events, live stdout/stderr, patch and diff progress, token and cost counters, and a final safe reasoning summary.

The feature must preserve the Harness safety model. The persisted run event stream remains authoritative. The UI and CLI render that stream, but they do not invent progress, bypass policy, stream raw backend logs directly, expose hidden reasoning tokens, or mutate the active repository during live editing.

Target commands:

```bash
harness run-live --task-file tasks/fix.md --agent code_editor
harness tasks run <task_id> --live
harness runs tail <run_id>
harness events <run_id> --jsonl --follow
harness transcript <run_id>
harness summary <run_id>
```

Target live output:

```text
● Run started
  run_id: run_...
  task_id: task_...
  agent: code_editor
  backend: codex_cli / local_coder
  mode: edit-isolated

● Resolving policy
  hosted_provider: approval_required
  active_repo_write: forbidden until apply-back
  isolated_workspace: required

● Preparing workspace
  copied repo to .harness/workspaces/run_...
  base commit: abc123

● Model started
  streaming: enabled
  input tokens: 18,240

thinking summary:
  I am inspecting the test failure, then I will patch the smallest affected module.

● Tool call: repo_read
  path: tests/test_parser.py

● Tool result
  found failing assertion around parse_config(...)

● Editing
  modified src/parser.py

● Diff ready
  +12 -4 lines

● Running tests
  python -m pytest -q

● Tests passed
  87 passed in 4.2s

● Final summary
  Fixed parser fallback handling. No active repo write performed.
  Apply-back requires approval.
```

## Product Principles

- Show observable execution, not private hidden reasoning.
- Persist every visible step as an event before rendering it.
- Keep the run event stream authoritative across CLI, UI, transcripts, and reports.
- Preserve hosted-provider, network, Docker, destructive-action, and apply-back approvals.
- Keep live edits inside isolated workspaces until explicit apply-back approval.
- Redact before user display, transcript registration, and final report generation.
- Store raw backend logs only as restricted artifacts when policy allows.
- Make JSON/JSONL stable and schema-versioned.
- Render human streams from the same events used for machine replay.

## Visibility Contract

Visible live stream:

```text
- model message tokens
- safe reasoning summaries
- procedure events
- tool calls and tool results
- shell/test stdout/stderr after redaction
- diffs and diff stats after redaction checks
- token counts and cost estimates
- safety and approval decisions
- final summary
```

Not exposed:

```text
- raw hidden chain-of-thought
- private model scratchpad
- unredacted secrets
- hidden backend internals
- unrestricted raw backend logs
```

If a backend provides reasoning token counts, Harness may display the count. It must not display raw reasoning content. The final report should include a concise `reasoning_summary`, not chain-of-thought.

## Architecture

Introduce a `LiveRunStream` layer across the existing control plane and execution plane.

The control plane owns:

```text
- tasks
- runs
- effective policy
- approvals
- event envelopes
- artifact registration
- manifests
- redaction metadata
- CLI/API JSON and JSONL contracts
```

The execution plane owns:

```text
- backend routing
- backend streaming adapters
- typed tools
- isolated workspaces
- Docker/test execution
- stdout/stderr capture
- patch and diff generation
- secret scanning
```

Flow:

```text
User prompt
  -> create TaskSpec / Run
  -> resolve EffectivePolicy
  -> create run directory
  -> open event stream
  -> start backend runner
  -> stream model/tool/test events
  -> persist transcript.jsonl
  -> persist events.jsonl
  -> write final_report.md
  -> write manifest.json
  -> expose final summary
```

The frontend should never synthesize run state. It subscribes to persisted events and renders them.

## Run Event Schema

Add these event types:

```python
class RunEventType(str, Enum):
    RUN_STARTED = "run.started"
    POLICY_RESOLVED = "policy.resolved"
    APPROVAL_REQUIRED = "approval.required"
    WORKSPACE_PREPARED = "workspace.prepared"
    BACKEND_STARTED = "backend.started"
    MODEL_TOKEN = "model.token"
    MODEL_MESSAGE_DELTA = "model.message_delta"
    REASONING_SUMMARY_DELTA = "reasoning.summary_delta"
    TOOL_CALL_STARTED = "tool_call.started"
    TOOL_CALL_OUTPUT = "tool_call.output"
    TOOL_CALL_FINISHED = "tool_call.finished"
    FILE_READ = "file.read"
    FILE_WRITE = "file.write"
    DIFF_UPDATED = "diff.updated"
    TEST_STARTED = "test.started"
    TEST_OUTPUT = "test.output"
    TEST_FINISHED = "test.finished"
    TOKEN_USAGE_UPDATED = "token_usage.updated"
    ARTIFACT_REGISTERED = "artifact.registered"
    RUN_SUMMARY_CREATED = "run.summary_created"
    RUN_FINISHED = "run.finished"
    RUN_FAILED = "run.failed"
```

Add visibility and redaction state enums:

```python
class EventVisibility(str, Enum):
    USER_VISIBLE = "user_visible"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


class RedactionState(str, Enum):
    NOT_REQUIRED = "not_required"
    REDACTED = "redacted"
    RESTRICTED = "restricted"
    BLOCKED = "blocked"
```

Stable JSONL envelope:

```json
{
  "schema_version": "harness.event/v1",
  "event_id": "evt_...",
  "run_id": "run_...",
  "task_id": "task_...",
  "trace_id": "trace_...",
  "seq": 42,
  "timestamp": "2026-05-14T12:00:00Z",
  "type": "tool_call.started",
  "visibility": "user_visible",
  "redaction_state": "redacted",
  "payload": {
    "tool": "repo_read",
    "input_preview": {
      "path": "src/parser.py"
    }
  }
}
```

Sequence contract:

- `seq` is strictly increasing per run.
- Every emitted event has `run_id`, `seq`, `timestamp`, and `type`.
- Events can be replayed deterministically from `events.jsonl`.
- JSONL output has no ANSI escape codes.
- Human output is a rendering of event data, not a separate source of truth.

## Token Usage

Add token accounting:

```python
class TokenUsageSnapshot(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cached_input_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost_usd: Decimal | None = None
```

Rules:

- Emit `token_usage.updated` whenever the backend provides a new usage snapshot.
- Persist the latest aggregate snapshot to `token_usage.json`.
- Include token usage in `final_report.md`.
- Display `reasoning_tokens` only as a count, and only if provided by the backend.
- Do not infer hidden reasoning content from token counts.

## Transcript Artifacts

Each live run writes:

```text
.harness/runs/<run_id>/
  events.jsonl
  transcript.jsonl
  token_usage.json
  procedure.md
  final_report.md
  manifest.json
  diff.patch
  stdout.log
  stderr.log
```

Artifact definitions:

- `events.jsonl`: complete machine-readable event stream.
- `transcript.jsonl`: user-facing conversation and procedure stream.
- `token_usage.json`: latest token usage snapshot plus backend/model metadata.
- `procedure.md`: readable Codex-style trace rendered from events.
- `final_report.md`: post-run summary with request, procedure, changes, tests, token usage, approvals, artifacts, and risks.
- `manifest.json`: run manifest referencing all registered artifacts.
- `diff.patch`: workspace diff when edits occurred.
- `stdout.log` and `stderr.log`: redacted or restricted logs according to policy.

Artifacts must be registered with size, SHA-256, redaction state, visibility, and relative path. The manifest must reference every generated artifact.

## Backend Streaming Protocol

Add a common streaming event emitted by backend adapters:

```python
class BackendStreamEvent(BaseModel):
    type: Literal[
        "message_delta",
        "reasoning_summary_delta",
        "tool_call",
        "tool_result",
        "token_usage",
        "status",
        "error",
    ]
    text: str | None = None
    payload: dict = {}
```

Adapter responsibilities:

- Convert backend-native streaming into Harness run events.
- Capture stdout/stderr separately from user-visible summaries.
- Redact before emitting user-visible events.
- Preserve raw logs as restricted artifacts only when policy allows.
- Emit useful status events for non-streaming backends.

Codex CLI adapter:

```text
Codex subprocess stdout/stderr
  -> parse structured events if available
  -> otherwise classify line-oriented output
  -> persist raw backend log as restricted artifact
  -> emit redacted user-visible stream events
```

Local OpenAI-compatible adapter:

```text
stream=True response
  -> message deltas
  -> token usage snapshots when available
  -> synthetic procedure events around tool execution
```

Non-streaming backend adapter:

```text
emit BACKEND_STARTED
emit periodic heartbeat/status events
emit final MODEL_MESSAGE_DELTA once complete
emit TOKEN_USAGE_UPDATED if available
```

Mock backend:

```text
emit deterministic message deltas
emit deterministic tool/status events
emit deterministic token usage snapshots
support golden event-order tests
```

## CLI Plan

Add:

```bash
harness run-live --task-file tasks/fix.md --agent code_editor
```

Equivalent to:

```bash
harness run --task-file tasks/fix.md --agent code_editor --stream human
```

Add stream formats:

```bash
--stream human
--stream jsonl
--stream none
```

Add follow commands:

```bash
harness runs tail <run_id>
harness runs tail <run_id> --jsonl
harness events <run_id> --jsonl --follow
```

Add transcript commands:

```bash
harness transcript <run_id>
harness transcript <run_id> --format markdown
harness transcript <run_id> --format jsonl
```

Add token display:

```bash
harness usage <run_id>
```

Example JSONL stream:

```json
{"type":"run.started","seq":1,"run_id":"run_123"}
{"type":"policy.resolved","seq":2,"levels":{"active_repo":"approval_required"}}
{"type":"model.message_delta","seq":3,"delta":"I will inspect the failing tests first."}
{"type":"tool_call.started","seq":4,"tool":"repo_read"}
{"type":"token_usage.updated","seq":5,"output_tokens":312}
```

CLI acceptance:

- `harness runs tail <run_id>` displays events while a run is active.
- Interrupting `tail` does not affect the run.
- Completed runs can be replayed from `events.jsonl`.
- JSONL stream remains machine-readable and stable.
- Human stream is readable in a terminal.

## UI Plan

Add a Codex mode run screen with four panes:

```text
1. Live Procedure
   Human-readable event stream: policy, workspace, tool calls, edits, tests.

2. Model Output
   Streamed assistant text and safe reasoning summaries.

3. Artifacts
   diff.patch, final_report.md, manifest.json, logs, test output.

4. Controls
   approve hosted boundary
   approve apply-back
   stop run
   copy prompt
   open isolated workspace
   inspect diff
```

Run states:

```text
Queued
Resolving policy
Waiting approval
Preparing workspace
Thinking
Calling tool
Editing
Running tests
Summarising
Awaiting apply-back approval
Succeeded
Failed
Cancelled
```

UI acceptance:

- A run starts immediately after prompt submission.
- The user sees live events without refreshing.
- The final summary appears after completion.
- Approval events pause the run instead of failing silently.
- UI state is derived from persisted events.

## Safety And Redaction

This feature must not become a dump-everything mode.

Redact before display and before normal transcript registration:

```text
- API keys
- tokens
- private environment variables
- secret file paths
- backend stderr containing secrets
- test output containing secrets
- patch content if secret scanner flags it
```

Rules:

- Do not expose raw chain-of-thought tokens.
- Do not bypass Harness policy to imitate Codex CLI.
- Do not stream unredacted backend logs directly to the UI.
- Do not allow Codex or any hosted backend to run without hosted-boundary approval.
- Do not mutate the active repo from a live run.
- Live editing must happen in an isolated workspace.
- Apply-back requires approval.
- If patch content contains secrets, do not silently mutate the patch artifact. Mark it blocked or restricted according to policy.

## Final Summary Contract

At the end of every run, generate `final_report.md`:

```markdown
# Run Summary

## User request
...

## What happened
...

## Procedure taken
1. Resolved policy.
2. Prepared isolated workspace.
3. Inspected files.
4. Applied patch in workspace.
5. Ran tests.
6. Produced diff.

## Reasoning summary
A concise safe explanation of why these steps were chosen.

## Files changed
...

## Tests
...

## Token usage
...

## Approvals
...

## Artifacts
...

## Remaining risks
...
```

The summary gives the user a clear account of what the model did and why without exposing raw hidden reasoning.

## PR Sequence

Implementation status:

```text
PR 1 - Live Event Schema: completed in working tree
PR 2 - Stream Writer And Tail Command: completed in working tree
PR 3 - Backend Streaming Abstraction: completed in working tree
PR 4 - Procedure Renderer: completed in working tree
PR 5 - Transcript And Final Report Artifacts: completed in working tree
PR 6 - UI Codex Mode: completed in working tree
PR 7 - Approval Integration: completed in working tree
PR 8 - Tests And Golden Flows: completed in working tree
```

### PR 1 - Live Event Schema

Add `RunEventType`, event visibility, redaction state, sequence numbers, event envelope models, and token usage snapshots.

Acceptance criteria:

```text
- every emitted event has run_id, seq, timestamp, type
- event seq is strictly increasing per run
- events are persisted to events.jsonl
- events can be replayed deterministically
```

### PR 2 - Stream Writer And Tail Command

Add a stream writer that writes both SQLite events and JSONL artifacts. Add:

```bash
harness runs tail <run_id>
harness events <run_id> --jsonl --follow
```

Acceptance criteria:

```text
- tail displays events while a run is active
- interrupted tail does not affect the run
- completed runs can be replayed from events.jsonl
```

### PR 3 - Backend Streaming Abstraction

Add `BackendStreamEvent` and adapters for:

```text
- codex_cli
- local_compatible streaming
- fake/mock backend for tests
```

Acceptance criteria:

```text
- mock backend streams deterministic deltas
- Codex backend stdout/stderr is captured
- backend output is redacted before user display
- raw/restricted backend logs are handled by policy
```

### PR 4 - Procedure Renderer

Build the human-readable Codex-like display.

Render:

```text
- run start
- policy resolution
- approvals
- workspace setup
- model status
- tool calls
- file edits
- diffs
- tests
- token updates
- final summary
```

Acceptance criteria:

```text
- human stream is readable in terminal
- JSONL stream remains stable and machine-readable
- no ANSI escapes in JSON output
```

### PR 5 - Transcript And Final Report Artifacts

Add:

```text
transcript.jsonl
procedure.md
final_report.md
token_usage.json
```

Acceptance criteria:

```text
- artifacts are registered with sha256 and size
- manifest references all generated artifacts
- final report includes reasoning_summary, not raw chain-of-thought
```

### PR 6 - UI Codex Mode

Add the app screen that subscribes to a run stream.

UI components:

```text
- live procedure feed
- streamed output panel
- artifacts panel
- approval controls
- token/cost/status header
```

Acceptance criteria:

```text
- run starts immediately after prompt submission
- user sees live events without refreshing
- final summary appears after completion
- approvals pause the run instead of failing silently
```

### PR 7 - Approval Integration

Make hosted-boundary, network, Docker, and apply-back approvals visible in-stream.

Acceptance criteria:

```text
- approval_required event is emitted
- run status becomes waiting_approval
- approving resumes the run
- denying produces a clean final report
```

### PR 8 - Tests And Golden Flows

Add golden tests:

```text
init -> live inspect -> transcript artifacts
init -> live fake edit -> streamed diff -> report
init -> live Codex mock -> isolated diff -> deny apply -> unchanged repo
init -> live Codex mock -> approve apply -> changed repo
init -> live Docker denied -> no Docker invocation
init -> live Docker approved -> streamed stdout/stderr
```

Acceptance criteria:

```text
- artifact names are stable
- active repo mutation boundaries are verified
- event order is deterministic
- manifest contents match generated artifacts
- secret redaction is covered for stdout, stderr, transcripts, and reports
```

## Definition Of Done

Implementation status: completed in working tree.

The feature is complete when this works:

```bash
harness run-live --task-file tasks/small_fix.md --agent code_editor
```

And the user sees, in real time:

```text
- run created
- policy checked
- approvals requested if needed
- workspace prepared
- model output streaming
- tool calls streaming
- edits and diff updates
- test output streaming
- token usage updates
- final summary
- artifact list
- apply-back approval state
```

After the run, these files must exist:

```text
.harness/runs/<run_id>/events.jsonl
.harness/runs/<run_id>/transcript.jsonl
.harness/runs/<run_id>/procedure.md
.harness/runs/<run_id>/final_report.md
.harness/runs/<run_id>/manifest.json
.harness/runs/<run_id>/token_usage.json
```

This delivers a Codex CLI-style live run experience while preserving the Harness safety kernel: streamed procedure, durable evidence, explicit approvals, isolated edits, redaction, and final summarization.
