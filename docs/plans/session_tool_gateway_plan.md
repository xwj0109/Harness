# Session Tool Gateway Implementation Plan

## Summary

Implement Harness model-visible tools as a focused session tool gateway, not as a general autonomy expansion. The goal is to make tool calls behave like a conventional coding harness while preserving Harness control-plane contracts: typed tools, explicit permission gates, durable events, redacted evidence, and no ambient authority.

The key product distinction is:

- `cwd` is session-local, project-relative, durable state used by tools.
- `/project` and `/workspace` are active root switches with a separate safety model.
- Shell support is a permissioned, auditable, bounded session shell tool, not unrestricted generic shell access.
- Git diffs are first-class read-only session tool output, scoped by the same cwd resolver.

## Release Gates

- All model-visible tools route through the session-tool registry and `execute_session_tool`; no bespoke chat/TUI helper path for `read`, `grep`, `cd`, `git-diff`, or `shell`.
- `CwdResolver` is the only cwd/path normalization authority.
- All cwd checks resolve symlinks before boundary checks.
- Cwd update and `session.cwd_changed` event append are atomic.
- Shell approvals are exact normalized capability grants, not broad command approvals.
- Shell tool calls are non-idempotent by default and must not be auto-replayed.
- Do not claim shell commands are read-only except for a deliberately tiny allowlist.
- Plugin tools, raw MCP execution, and PTY remain disabled.

## PR 1: Session Cwd Model, Resolver, `cd`, `pwd`, and `git-diff`

Add a `SessionCwd` model and a single `CwdResolver`.

The resolver accepts:

```text
project_root
session_cwd
call_cwd
requested_path
context_excludes
```

It returns:

```text
normalized_project_relative_cwd
resolved_abs_path
permission_status
blocked_reason
```

Rules:

- Precedence is `explicit tool cwd > session cwd > "."`.
- Explicit tool cwd never mutates session cwd.
- Session cwd is stored project-relative, with `"."` as default.
- Use canonical absolute paths after resolving symlinks.
- Reject paths outside the active project.
- Reject secret-like paths.
- Context-excluded directories require permission before becoming cwd.
- On macOS/case-insensitive filesystems, do not rely only on string-prefix checks.

Add `pwd`, `cd`, and `git-diff`:

- `pwd` reports project root, session cwd, and resolved absolute cwd.
- `cd` changes session cwd without starting a process.
- `cd` records a durable event and updates session metadata transactionally.
- `git-diff` is read-only, starts only git read commands, and persists diff artifacts before display.

Event shape:

```json
{
  "type": "session.cwd_changed",
  "session_id": "...",
  "project_root": "...",
  "old_cwd": ".",
  "new_cwd": "src",
  "requested_path": "./src",
  "resolved_abs_path": "/repo/src",
  "actor": "operator|model",
  "tool_call_id": "..."
}
```

## PR 2: Existing Session Tools Inherit Cwd

Update existing tools before adding shell execution risk.

Affected tools:

```text
read
glob
grep
repo-overview
docker-test
git-diff
```

Behavior:

- If a tool has no explicit cwd/path base, it runs relative to session cwd.
- If the tool supplies explicit cwd, resolve it through `CwdResolver` for that call only.
- Read/search tools still honor context excludes and secret-path filtering.
- `docker-test` persists the resolved project-relative cwd in its plan artifact.

## PR 3: Route Chat/TUI Tool Calls Through Session Tools

Make session tools the canonical model-visible tool system.

Canonical path:

```text
model emits harness.tool_request/v1
  -> parse tool id + args
  -> lookup SessionToolDescriptor
  -> validate input schema
  -> resolve cwd if needed
  -> policy/permission gate
  -> execute_session_tool
  -> persist result event/artifact
  -> render result
```

Required UI/CLI behavior:

- `/tools` lists session tool descriptors.
- `/cd <path>` calls the `cd` session tool.
- `/pwd` calls the `pwd` session tool.
- `/project` and `/workspace` switch active root explicitly.
- Chat status shows current project root and session cwd.
- Permission-required tools pause with a clear permission id and next command.
- User-visible output is rendered only after persisted events/artifacts exist.

## PR 4: Permissioned Session Shell Tool

Enable `shell` as a permission-required session tool only after cwd/read-tool behavior is stable.

Descriptor framing:

```text
id: shell
side_effect: execution
boundary_kind: shell
permission_required: true
replay_policy: rerun_forbidden
```

Input:

```json
{
  "command": "pytest -q",
  "cwd": "src",
  "timeout_seconds": 30
}
```

Permission target must include:

```text
project_fingerprint
session_id or project_session_id
resolved_cwd
command
timeout_seconds
shell_executable
env_policy
network_policy
```

Execution requirements:

- First call creates a pending permission and does not execute.
- Allowed matching call executes once.
- Output is redacted before preview/artifact registration.
- Persist command, cwd, shell executable, timeout, env policy, network policy, exit code, timed-out flag, stdout/stderr preview, and artifact ids.
- Large stdout/stderr goes to artifacts.
- Evidence shows `process_started=true` and `shell_execution_started=true`.
- Do not mark shell commands read-only unless a tiny explicit allowlist exists.

Special-case only the simple form:

```text
cd <single path>
```

That form routes to the `cd` tool and starts no process.

Do not special-case:

```text
cd foo && pytest
cd foo; rm -rf bar
builtin cd foo
(cd foo)
export X=1; cd foo
```

Those are normal shell commands and require shell permission.

## PR 5: `/project` and `/workspace` Root Switching

Add explicit active-root switching as a separate state transition from `cd`.

Behavior:

```text
/project /repo-a -> attach existing session for repo-a if present, else create session
/workspace /repo-b -> same semantics, alias unless intentionally differentiated
/cd /repo-b while in /repo-a -> project switch proposal, not cwd mutation
/cd .. inside /repo-a/subdir -> cwd mutation only if still inside root
```

Rules:

- Project identity uses canonical project root plus `.harness` metadata when initialized.
- If target root is not initialized, show the path and require `/init`.
- Do not silently initialize.
- Rebuild dashboard/chat/TUI context after switching root.
- Keep global workspace registry and remote attach out of scope.

## PR 6: Server Route Parity and Golden Regressions

Bring CLI, TUI, chat, and HTTP surfaces onto the same tool gateway.

Server changes:

- Add a session tool-call route equivalent to CLI `harness session tool`.
- Route `/sessions/{session_id}/shell` through the shell session tool.
- Expose session cwd in session status/projection endpoints.
- Preserve permission queue and permission reply behavior.
- Keep PTY routes disabled/fail-closed.

Docs:

- Add this plan as `docs/plans/session_tool_gateway_plan.md`.
- Update `docs/operator_guide.md` with the `cwd` vs project root distinction.
- Document shell as permissioned, auditable, and bounded, not generic ambient shell.

## Test Plan

Cwd resolver tests:

```text
cd src                         # ok
cd ./src                       # ok
cd src/..                      # normalizes
cd .                           # ok
cd ..                          # ok only if still inside project
cd ../outside                  # reject or project-switch proposal
cd symlink_to_outside          # reject
cd .harness                    # permission/deny per policy
cd build                       # permission if context-excluded
cd secrets                     # reject
cd path/with/.env              # reject
```

Symlink/path attack tests:

```text
ln -s /tmp outside_link
cd outside_link                         # reject

mkdir -p safe
ln -s ~/.ssh safe/ssh_link
read safe/ssh_link/config               # reject

ln -s ../outside src/escape
grep pattern src/escape                 # reject

cd SAFE on case-insensitive FS if safe/ differs by case
```

Tool inheritance tests:

```text
cd src
read app.py                             # reads src/app.py
grep pattern .                          # searches src/
glob "*.py"                             # lists under src/
read {"path":"app.py","cwd":"tests"}    # explicit cwd overrides for one call
pwd                                     # still reports src
git-diff                                # scopes to src/
```

Shell permission tests:

```text
shell cwd=src command="pytest -q"       # creates pending permission only
approve permission
same shell call                         # executes and persists evidence
change cwd to .
same command                            # needs new permission
timeout 30 approved, timeout 300 later  # needs new permission
default shell approved, shell override  # needs new permission
cd src via shell simple form            # routes to cd, no process
cd src && pytest                        # requires shell permission
```

Server/TUI/chat tests:

```text
harness.tool_request/v1 read -> session tool
harness.tool_request/v1 cd -> session tool
harness.tool_request/v1 shell -> permission pause
/sessions/{id}/tool -> same session tool gateway
/sessions/{id}/shell -> same permissioned shell gateway
/cd, /pwd, /project, /workspace render stable operator-facing output
TUI status updates project root and cwd after transitions
```

Regression tests:

```text
existing read/glob/grep evidence remains read-only
patch/direct-write remain plan-only and do not mutate active workspace
web-fetch/web-search still require external-network permission
mcp-resource still reads cached resources only
plugin-tool, raw mcp, and pty remain disabled
```

## Assumptions and Defaults

- This work is a focused session-tool gateway implementation, not a broad autonomy expansion.
- Shell execution is allowed only through exact permission and auditable evidence.
- `cd` inside a project is session state, not process execution.
- Moving outside the current project is a project/workspace switch proposal.
- Active repo mutation remains governed by existing apply-back and mutation policy.
- All user-visible tool output must be backed by persisted events/artifacts before display.
- The PR series should remain split so the shell feature has a clean rollback point.
