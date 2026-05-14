# Release-Ready Self-Managed Actions Plan

## Summary

Replace the current hard-coded chat fast paths with a release-ready self-managed action system. The goal is for Harness to organize safe local work by itself and return only the final result plus report, while still preserving explicit approval gates for hosted providers, secrets, destructive actions, external network, and active repo apply-back.

The current implementation contains a narrow special case for empty Markdown file creation in `src/harness/chat.py`. That behavior proves the desired UX, but it is not a scalable product architecture. This plan replaces that special case with a deterministic route, policy, executor, evidence, and reporting layer.

Target user experience:

```text
user: create an empty .md file in this directory

assistant:
Created scratch.md.

Report:
.harness/runs/run_x/final_report.md
Manifest:
.harness/runs/run_x/manifest.json
```

No action-contract wording should be shown for safe, policy-approved local work. Action contracts remain an internal mechanism and visible only when Harness needs explicit approval or cannot safely proceed.

## Current Problems

- The empty Markdown file behavior is hard-coded in chat intent handling.
- The implementation recognizes one narrow file type and one narrow action shape.
- Low-risk local actions still share too much UX with high-risk actions.
- The action-contract details leak into the product surface.
- There is no general schema for self-managed local actions.
- Evidence generation is embedded in the chat handler instead of owned by an executor.
- Risk classification is implicit in code branches instead of explicit in route/policy data.
- The TUI and plain chat do not yet share a single product-level action execution contract.

## Product Principles

- Harness should self-manage safe local work and return result plus evidence.
- The user should not need to understand Harness internals for common actions.
- Automatic must not mean permissive.
- Route selection must be deterministic before execution.
- Policy must decide whether an action can run without user confirmation.
- Every self-managed action must produce durable evidence.
- The final report is user-facing; manifests/events/artifacts remain authoritative.
- High-risk work must still pause with a clear approval reason and next action.

## Architecture

Introduce a new action spine:

```text
instruction
  -> action route
  -> normalized action request
  -> risk and policy decision
  -> executor
  -> run/event/artifact/report evidence
  -> concise final response
```

Create these modules:

```text
src/harness/action_router.py
src/harness/action_policy.py
src/harness/action_executors.py
src/harness/action_reports.py
```

The existing `intent_router.py` should continue to handle higher-level product routes such as `fix_tests`, `explain_repo`, and `plan_change`. The new `action_router.py` handles concrete local action requests such as creating files and directories.

## Public Types

Add these models to `src/harness/models.py` or a dedicated action module.

```python
class ManagedActionRisk(str, Enum):
    READ_ONLY = "read_only"
    LOCAL_WORKSPACE_WRITE_LOW = "local_workspace_write_low"
    LOCAL_WORKSPACE_WRITE_MEDIUM = "local_workspace_write_medium"
    SANDBOXED_EXECUTION = "sandboxed_execution"
    HOSTED_PROVIDER = "hosted_provider"
    ACTIVE_REPO_APPLY_BACK = "active_repo_apply_back"
    DESTRUCTIVE = "destructive"
    EXTERNAL_NETWORK = "external_network"


class ManagedActionDecisionStatus(str, Enum):
    AUTO_ALLOWED = "auto_allowed"
    APPROVAL_REQUIRED = "approval_required"
    DENIED = "denied"
    UNSUPPORTED = "unsupported"


class ManagedActionRoute(BaseModel):
    schema_version: str = "harness.managed_action_route/v1"
    intent: str
    confidence: Literal["exact", "pattern", "fallback"]
    risk: ManagedActionRisk
    executor: str
    normalized_arguments: dict[str, Any] = Field(default_factory=dict)
    required_approvals: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    policy_notes: list[str] = Field(default_factory=list)


class ManagedActionDecision(BaseModel):
    schema_version: str = "harness.managed_action_decision/v1"
    status: ManagedActionDecisionStatus
    route: ManagedActionRoute
    reasons: list[str] = Field(default_factory=list)
    requires_human: bool = False


class ManagedActionResult(BaseModel):
    schema_version: str = "harness.managed_action_result/v1"
    ok: bool
    status: str
    intent: str
    run_id: str | None = None
    created_paths: list[Path] = Field(default_factory=list)
    changed_paths: list[Path] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    report_path: Path | None = None
    manifest_path: Path | None = None
    message: str
    next_actions: list[str] = Field(default_factory=list)
```

## Routing

Move hard-coded request parsing out of `chat.py` and into `action_router.py`.

Initial route table:

```yaml
managed_action_routes:
  create_empty_markdown_file:
    patterns:
      - create empty markdown file
      - create empty .md file
      - make blank markdown file
      - make blank .md file
      - do *.md
    risk: local_workspace_write_low
    executor: create_empty_file
    default_filename: scratch.md
    allowed_extensions:
      - .md
    overwrite_policy: never
    expected_outputs:
      - created_file
      - final_report.md
      - manifest.json

  create_empty_text_file:
    patterns:
      - create empty text file
      - create empty .txt file
      - make blank text file
    risk: local_workspace_write_low
    executor: create_empty_file
    default_filename: scratch.txt
    allowed_extensions:
      - .txt
    overwrite_policy: never
    expected_outputs:
      - created_file
      - final_report.md
      - manifest.json

  create_directory:
    patterns:
      - create directory
      - make directory
      - create folder
      - make folder
    risk: local_workspace_write_low
    executor: create_directory
    overwrite_policy: no_op_if_exists
    expected_outputs:
      - final_report.md
      - manifest.json

  local_note:
    patterns:
      - write note
      - add note
      - save note
    risk: local_workspace_write_low
    executor: write_note_file
    default_filename: notes.md
    overwrite_policy: append
    expected_outputs:
      - changed_file
      - final_report.md
      - manifest.json
```

Routing rules:

- Prefer exact filename extraction over default filenames.
- Never accept path traversal.
- Never write into `.git`, `.harness`, secret-like paths, absolute paths, or parent directories.
- Never overwrite existing files unless the route explicitly supports append/no-op behavior.
- If a user request is ambiguous but has a safe default, choose the safe default and report it.
- If a safe default does not exist, ask one concise clarification.
- Unsupported actions fall back to the existing chat/model path without side effects.

## Policy

Create `action_policy.py` to decide whether a route can execute automatically.

Default policy:

```yaml
self_managed_policy:
  auto_allowed:
    - read_only
    - local_workspace_write_low
  approval_required:
    - local_workspace_write_medium
    - sandboxed_execution
    - hosted_provider
    - active_repo_apply_back
  denied:
    - destructive
    - external_network
```

Low-risk local workspace writes are auto-allowed only when all of these are true:

- Target path is inside project root.
- Target path is not secret-like.
- Target path is not under `.git`.
- Target path is not under `.harness` unless the executor is an internal evidence writer.
- Target path is not absolute input from the user.
- Target path does not traverse upward.
- Overwrite policy is satisfied.
- File extension is in the route allowlist.
- The executor is deterministic.

Medium/high-risk actions require explicit approval:

- Editing existing source files.
- Running tests.
- Invoking hosted Codex or other hosted providers.
- Network access.
- Deleting files.
- Moving files.
- Applying isolated changes back to the active repo.
- Any action involving secret-like paths or detected secrets.

## Executors

Create `action_executors.py`.

Executor interface:

```python
class ManagedActionExecutor(Protocol):
    id: str

    def execute(
        self,
        project_root: Path,
        route: ManagedActionRoute,
        store: SQLiteStore,
    ) -> ManagedActionResult:
        ...
```

Initial executors:

```text
create_empty_file
create_directory
write_note_file
```

`create_empty_file` behavior:

- Resolve filename from `route.normalized_arguments["filename"]`.
- If missing, use route default.
- Validate extension allowlist.
- Validate project-relative target.
- Use no-overwrite naming:
  - `scratch.md`
  - `scratch-2.md`
  - `scratch-3.md`
- Create file with empty content.
- Create run with `task_type = managed_action.create_empty_file`.
- Append event `managed_action.file_created`.
- Register created file artifact.
- Generate final report.
- Return concise result.

`create_directory` behavior:

- Resolve directory name from normalized arguments.
- Validate project-relative target.
- If exists and is directory, no-op with success.
- If exists and is file, fail with clear message.
- Create directory.
- Create run, event, report, manifest.

`write_note_file` behavior:

- Resolve filename or use `notes.md`.
- Append note text.
- Never overwrite whole file.
- Create run, event, artifact, report, manifest.

## Reports

Create `action_reports.py` for report generation instead of writing report content inside chat handlers.

Standard report:

```markdown
# Harness Managed Action Report

## Summary
- Request:
- Intent:
- Status:
- Risk:
- Executor:

## Result
- Created:
- Changed:
- Skipped:

## Policy
- Decision:
- Reasons:
- Hosted provider:
- External network:
- Active repo write:

## Evidence
- Run:
- Events:
- Artifacts:
- Manifest:

## Next Actions
- Inspect:
- Undo:
```

The report generator should accept `ManagedActionRoute`, `ManagedActionDecision`, and executor output.

## Chat Integration

Replace this current pattern:

```python
_maybe_self_manage_empty_markdown_file(...)
```

with:

```python
route = route_managed_action(raw, project_root)
if route.intent != "unsupported":
    decision = decide_managed_action(route, project_root)
    if decision.status == AUTO_ALLOWED:
        result = execute_managed_action(route, decision, project_root)
        return render_managed_action_result(result)
    if decision.status == APPROVAL_REQUIRED:
        return render_approval_required(decision)
    if decision.status == DENIED:
        return render_denied(decision)
```

The visible response for auto-allowed actions must contain only:

- Final result.
- Run id.
- Report path.
- Manifest path.
- Any concise warning if the requested filename had to be adjusted to avoid overwrite.

The visible response must not show:

- Action-contract internals.
- Execution plan dictionaries.
- Tool names unless useful.
- Policy internals unless there is a block/approval.

## TUI Integration

The TUI should render managed action results as compact result cards:

```text
Done

Created scratch.md.
Report: .harness/runs/run_x/final_report.md
Manifest: .harness/runs/run_x/manifest.json
```

The right panel should show:

```text
Latest action
Status: succeeded
Run: run_x
Report: final_report.md
```

Only show pending approval UI when `ManagedActionDecision.status == approval_required`.

## CLI Integration

Add inspection commands:

```bash
harness actions route "<instruction>" --json
harness actions run "<instruction>" --json
harness actions report <run_id>
```

Behavior:

- `harness actions route` performs deterministic route and policy preview only.
- `harness actions run` executes only if auto-allowed, otherwise returns approval-required/denied payload.
- Chat/TUI call the same functions as the CLI.

Do not duplicate execution logic between chat and CLI.

## Migration Steps

### PR A1 - Managed Action Types And Router

- Add managed action models.
- Add route table and deterministic parser.
- Add path and filename normalization helpers.
- Add route preview tests.
- No execution yet.

Acceptance:

- `create an empty .md file in this directory` routes to `create_empty_markdown_file`.
- `do scratch.md` routes to `create_empty_markdown_file` with `filename=scratch.md`.
- `create ../secret.md` is denied or unsupported before execution.

### PR A2 - Policy Layer

- Add `action_policy.py`.
- Encode risk decisions.
- Add path safety checks.
- Add overwrite policy checks.
- Add secret-like path checks.

Acceptance:

- Empty Markdown file in project root is auto-allowed.
- Existing file is not overwritten.
- `.harness/foo.md`, `.git/foo.md`, `../foo.md`, and absolute paths are denied.
- Destructive actions require approval or are denied.

### PR A3 - Executors And Reports

- Add executor registry.
- Add `create_empty_file`.
- Add `create_directory`.
- Add report generator.
- Register run, event, artifacts, report, and manifest.

Acceptance:

- Auto-allowed file creation produces:
  - created file
  - run record
  - event
  - created file artifact
  - final report
  - manifest
- Report content follows the managed action report format.

### PR A4 - Chat/TUI Integration

- Remove `_maybe_self_manage_empty_markdown_file` from `chat.py`.
- Replace with generic managed action route/policy/executor flow.
- Update TUI result rendering.
- Keep action-contract UI only for approval-required work.

Acceptance:

- Chat request creates file and returns concise result.
- TUI request creates file and auto-scrolls to concise result.
- No action-contract text is shown for auto-allowed local file creation.

### PR A5 - CLI Actions Commands

- Add `harness actions route`.
- Add `harness actions run`.
- Add `harness actions report`.
- Use the same router/policy/executor code as chat.

Acceptance:

- CLI route command is read-only.
- CLI run command executes only auto-allowed actions.
- CLI report command shows report for managed action runs.

### PR A6 - Replace Hard-Coded Paths And Tighten Tests

- Delete hard-coded empty Markdown parsing from `chat.py`.
- Ensure all local self-managed actions use action router.
- Add regression tests for unsupported and approval-required actions.
- Add golden product flow tests.

Acceptance:

- No feature-specific local action parser remains in `chat.py`.
- Chat is orchestration glue only.
- Managed action behavior is covered by router, policy, executor, chat, TUI, and CLI tests.

## Test Plan

Router tests:

- Routes empty Markdown request.
- Extracts explicit filename.
- Defaults to `scratch.md`.
- Routes empty text request.
- Routes directory creation request.
- Rejects path traversal.
- Rejects unsupported extension.

Policy tests:

- Auto-allows low-risk local file creation.
- Denies `.git`, `.harness`, parent directory, absolute path, and secret-like target.
- Requires approval for medium/high-risk actions.
- Never auto-allows destructive actions.

Executor tests:

- Creates `scratch.md`.
- Creates `scratch-2.md` when `scratch.md` exists.
- Creates directory.
- No-ops on existing directory.
- Fails when target directory name exists as file.
- Writes run, event, artifact, final report, manifest.

Chat tests:

- `create an empty .md file in this directory` returns final result only.
- `do scratch.md` returns final result only.
- No pending action contract remains for auto-allowed route.
- Unsupported request still falls back to model/action-contract path.
- Approval-required request renders approval requirement.

TUI tests:

- Managed action result renders as concise final response.
- Prompt does not switch to confirmation placeholder for auto-allowed actions.
- Pending approval still uses confirmation placeholder.
- Chat pane auto-scroll remains intact.

CLI tests:

- `harness actions route "create an empty .md file"` returns route JSON without mutation.
- `harness actions run "create an empty .md file"` creates file and evidence.
- `harness actions run` refuses unsafe paths.
- `harness actions report <run_id>` prints report.

Full regression:

```bash
pytest -q
```

## Removal Checklist

Remove these hard-coded implementation details:

- `_maybe_self_manage_empty_markdown_file` in `chat.py`.
- `_empty_markdown_filename_from_request` in `chat.py`.
- `_next_available_markdown_path` in `chat.py`.
- Any report text specific to empty Markdown file creation inside `chat.py`.

Replace with:

- `route_managed_action`.
- `decide_managed_action`.
- `execute_managed_action`.
- `render_managed_action_result`.

## Release Readiness Criteria

The implementation is release-ready when:

- There are no feature-specific local action branches in chat handlers.
- Every self-managed action has a route, policy decision, executor, evidence, and report.
- Auto-allowed behavior is data-driven and test-covered.
- Approval-required behavior is explicit and concise.
- Unsafe paths are denied before filesystem mutation.
- Existing Harness safety guarantees remain intact.
- Plain chat, TUI, and CLI use the same managed action engine.
- The full suite passes.

## Non-Goals

- Do not make hosted Codex calls auto-approved.
- Do not auto-apply isolated diffs to the active repo.
- Do not allow destructive deletes in the first release.
- Do not build a full LLM planner for local actions yet.
- Do not remove the existing action-contract system; make it internal/conditional.

## Expected End State

After this plan is implemented, Harness will behave like this:

```text
user: create an empty .md file in this directory

assistant:
Created scratch.md.

Report:
.harness/runs/run_123/final_report.md
Manifest:
.harness/runs/run_123/manifest.json
```

And for higher-risk work:

```text
user: apply that patch to the active repo

assistant:
Approval required before active repo apply-back.

Reason:
This mutates existing project files.

Next:
Type yes to approve, no to cancel, or inspect the diff.
```

This gives the desired product feel: Harness organizes safe work itself, reports the result, and only interrupts the user when policy actually requires it.
