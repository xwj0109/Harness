# Opencode-Style LLM Chat Plan

## Corrected Product Target

Harness should become a real LLM coding and research assistant in the terminal, with an opencode-like chat loop and Harness as the structured control plane underneath.

The user experience should feel like this:

```bash
harness
> explain this repo
> find where task leasing happens
> plan the changes for a better chat model
> edit the files
> show me the diff
> run tests
> apply it
```

The user should not feel like they are typing deterministic commands into a safety kernel. The LLM should be the primary conversational layer. Harness should remain the backbone that controls context, tools, filesystem access, approvals, artifacts, policies, tasks, leases, runs, diffs, and apply-back.

## Core Product Rule

LLM chat is free-form reasoning.

Harness actions are structured, reviewable operations.

The user should be able to chat with the LLM in terms of intentions, not commands. The LLM should interpret those intentions, decide what Harness capabilities are relevant, and drive the full Harness backbone through typed tool requests and action contracts.

The LLM may converse normally, summarize, explain, inspect provided context, propose plans, use Harness tools, and draft edits. It must not get raw ambient authority. Every tool call and every real side effect goes through Harness.

This means the product is not limited to a small fixed chat command set. The LLM-facing tool layer should eventually expose every registered Harness capability that is safe and useful to expose, including read-only inspection, task/objective creation, lease/run inspection, registered adapter dispatch, isolated editing, sandboxed tests, artifact review, diff review, approval requests, apply-back, and policy explanation. The distinction is not "the LLM cannot use tools"; the distinction is "the LLM can only use tools through Harness-owned schemas, validation, permissions, confirmations, and evidence."

## Corrected Architecture

```text
Terminal UI / plain chat
        |
        v
LLM chat runtime
        |
        v
Harness context packer
        |
        v
LLM response + optional structured action proposal
        |
        v
Harness action validator / action contract
        |
        v
Harness control plane
        |
        v
registered adapters / isolated edits / tests / artifacts / apply-back
```

## LLM Tool Gateway Requirement

The LLM must be able to use the Harness backbone as its tool system.

The final chat loop should support this flow:

```text
user intention
  -> LLM interprets goal
  -> LLM requests context or a Harness tool
  -> Harness validates the request
  -> Harness executes the allowed tool/action
  -> result returns to LLM
  -> LLM continues reasoning
  -> side-effecting actions require confirmation
  -> Harness records evidence
```

The tool gateway should include both safe read-only tools and gated side-effect tools.

Read-only tools can run inside the chat loop:

```text
repo_tree
read_file
search_repo
grep_symbol
show_diff
show_recent_runs
show_progress
show_capabilities
show_task
show_run
show_artifact
explain_policy
list_agents
show_agent
list_workbenches
list_model_profiles
list_tool_policies
list_memory_scopes
show_objectives
show_objective
show_task_graph
show_leases
show_lease
show_registered_adapters
show_adapter
show_approvals
show_security_summary
show_sandbox_profiles
show_trace
show_apply_back_state
explain_blocked_state
```

Side-effect tools must go through action contracts and confirmation:

```text
create_task
create_objective
request_approval
dispatch_registered_adapter
edit_isolated
run_tests
apply_back
deny_apply_back
revert_pending_change
remember
forget_memory
```

Implementation should start with read-only tools because they are the safest way to get the model grounded. That is a sequencing decision, not the product boundary. The product boundary is that the LLM can drive all Harness-owned capabilities that have a typed schema and a policy-checked execution path.

These side-effect tools should cover orchestration-level user intentions, not only coding edits. Examples:

```text
create_objective:
  "create an objective for this refactor"

create_task:
  "add a planning task before the edit"

dispatch_registered_adapter:
  "continue this lease"

request_approval:
  "ask me for the hosted-provider approval needed here"

apply_back:
  "apply the isolated diff"
```

## Harness Domain Understanding Requirement

The user should be able to discuss Harness concepts through normal chat, without switching into internal command vocabulary.

The LLM should understand and operate over everything already built in the Harness backbone:

```text
orchestration
objectives
tasks
task graphs
leases
runs
registered adapters
agents
workbenches
model profiles
tool policies
memory scopes
approvals
security layer
policy explanations
sandbox profiles
isolated workspaces
artifacts
manifests
traces
diffs
apply-back
capabilities
daemon control-plane state
progress state
blocked-state explanations
```

The user should be able to ask things like:

```text
> what agents do we have?
> explain the security layer
> why is this task blocked?
> what artifacts came out of the last run?
> which adapter would handle this?
> create an objective for this refactor
> inspect the latest lease and continue
> show the diff from the isolated edit
> apply it
```

The LLM should answer in normal language, use Harness tools when it needs facts, and surface Harness structure only when it helps the user make a decision.

This requires two complementary pieces:

- Context: the model receives a compact, current summary of Harness state and vocabulary.
- Tools: the model can query detailed Harness state and request side-effecting Harness actions through schemas.

The model should not be expected to memorize Harness internals from its pretraining. The app must teach the model the current project state, available agents, available adapters, policies, artifacts, and current orchestration state on every relevant turn.

## Current Repo Assessment

The repo already has important pieces of the corrected architecture:

- `src/harness/backends/local_openai.py` has `LocalOpenAICompatibleBackend.complete(messages)`.
- `src/harness/config.py` already defines `local_openai_compatible` as a local-only native model backend.
- `src/harness/config.py` already defines `paid_openai_compatible` as a disabled hosted native model backend.
- `src/harness/builtin_specs/model_profiles.yaml` already has a local model profile pointing to `local_openai_compatible`.
- `src/harness/tools/readonly.py` already has early read-only tools for listing files, reading files, git status, and git diff.
- `src/harness/workflow_templates.py` already contains task graph templates that can become action builders.
- The task, lease, run, artifact, approval, isolation, apply-back, and adapter pieces are already present enough to serve as the authority layer.

The main mismatch is the chat layer:

- `src/harness/chat.py` currently treats natural language as deterministic intent routing.
- `route_chat_intent()` hard-codes phrases such as summarize, fix, plan, show progress, and apply back.
- `_deterministic_chat_guidance()` explicitly says chat does not call Codex, Docker, shell, providers, or model backends directly.
- `tests/test_operator_chat_path.py` currently encodes the old product assumption.
- The docs currently describe deterministic chat actions and passive dashboard-first behavior.

The near-term work is therefore to replace the deterministic chat brain with an LLM chat brain, while keeping Harness authority.

## System Invariants

- The LLM may converse freely.
- The LLM may use Harness tools through the LLM tool gateway.
- The LLM may understand and discuss Harness orchestration, agents, security, artifacts, policy, approvals, runs, leases, adapters, and progress through normal chat.
- The LLM may inspect only context and tool results that Harness provides.
- The LLM may not mutate the repo directly.
- The LLM may not invoke arbitrary shell.
- The LLM may not bypass Harness approvals.
- The LLM may not select hidden hosted fallback.
- The LLM may not access forbidden paths or secret-like content.
- The LLM may propose actions, but Harness validates and normalizes them.
- The LLM may request side-effecting Harness actions, but Harness decides whether they are valid, allowed, and confirmed.
- Side effects always go through registered Harness mechanisms.
- Edits happen in isolated workspaces.
- Tests happen through Harness-controlled sandbox/test mechanisms.
- Apply-back requires separate review and approval.
- Chat-side side effects link to evidence.
- Ordinary conversation should not create heavyweight task/run records.

## PR 1: Chat Model Runtime

Goal: make `harness --plain` and the TUI chat use a real LLM backend for ordinary conversation, with Codex CLI subscription chat as the practical default and local OpenAI-compatible chat as an optional local-only profile.

### Implementation

Add `src/harness/chat_model.py`.

Suggested types:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ChatContext:
    project_root: str
    model_profile: str
    mode: str
    context_blocks: list[dict[str, Any]] = field(default_factory=list)
    safety_boundaries: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ChatDelta:
    content: str


@dataclass(frozen=True)
class ChatResponse:
    content: str
    raw: dict[str, Any] | None = None
    action_proposals: list[dict[str, Any]] = field(default_factory=list)


class ChatModel(Protocol):
    def stream(self, messages: list[ChatMessage], context: ChatContext) -> Iterator[ChatDelta]:
        ...

    def complete(self, messages: list[ChatMessage], context: ChatContext) -> ChatResponse:
        ...
```

Initial implementations:

```text
CodexCliChatModel
  wraps CodexCliBackend.run_read_only()
  uses codex_cli subscription auth
  default for normal assistant chat when available
  read-only sandbox for conversational turns
  no active repo mutation from chat
  no paid API fallback

LocalOpenAIChatModel
  wraps LocalOpenAICompatibleBackend.complete()
  uses local_openai_compatible
  optional local_only profile
  no filesystem authority
  no tool authority
  no fallback to hosted
```

Add config:

```yaml
chat:
  default_model_profile: codex_cli
  mode: subscription
  stream: true
  allow_hosted_chat: false
  allow_codex_subscription_chat: true
```

Code-level work:

- Add `ChatConfig` to `src/harness/config.py`.
- Add `CodexCliChatModel` to `src/harness/chat_model.py`.
- Add `LocalOpenAIChatModel` to `src/harness/chat_model.py`.
- Add a resolver such as `build_default_chat_model(project_root)`.
- Update `src/harness/chat.py` so normal non-slash user input goes to the chat model first.
- Keep slash commands deterministic.
- Keep `route_chat_intent()` only as fallback or compatibility helper.
- Change deterministic guidance into chat-model-unavailable guidance.

### Acceptance Criteria

- `harness --plain` can answer arbitrary questions through the configured chat backend.
- The default configured chat backend is Codex CLI subscription chat, not a weak local model.
- Codex subscription chat runs as read-only conversation and does not mutate the active repo.
- Normal chat does not create tasks, runs, leases, approvals, Docker activity, Codex edit/adapter runs, arbitrary shell calls, or repo mutations.
- If the configured chat backend is unavailable, Harness says the chat model is unavailable.
- There is no silent fallback to `paid_openai_compatible`.
- Old deterministic routing tests are replaced or narrowed to slash/fallback behavior.

### Tests

- `test_plain_chat_uses_configured_chat_model_for_freeform_input`
- `test_codex_cli_chat_uses_read_only_subscription_backend`
- `test_plain_chat_does_not_create_harness_state_for_freeform_input`
- `test_chat_model_failure_does_not_fallback_to_paid_backend`
- `test_slash_commands_remain_deterministic`

## PR 2: Context Packer

Goal: make repo questions grounded without giving the model raw ambient access.

### Implementation

Add `src/harness/context_pack.py`.

Responsibilities:

- Read repo tree summary.
- Include selected/open files.
- Include README and primary docs.
- Include current git diff.
- Include recent Harness state.
- Include recent runs and artifact metadata.
- Include task/progress state.
- Include objective/task graph summary.
- Include active leases and blocked reasons.
- Include registered adapters and their capabilities.
- Include available agents, workbenches, model profiles, tool policies, and memory scopes.
- Include approval state and hosted/local data-boundary summary.
- Include security-layer summary and policy constraints.
- Include sandbox profile summary.
- Include isolated workspace/apply-back state where present.
- Include artifact manifest summaries and trace availability.
- Include `AGENTS.md` or Harness instructions as read-only context.
- Include safety/policy summary.
- Include a compact Harness vocabulary/instructions block so the LLM knows what objectives, tasks, leases, runs, adapters, artifacts, and apply-back mean in this repo.
- Obey context budget.
- Obey `context_excludes`.
- Block secrets and forbidden paths.
- Produce a context manifest.

Suggested types:

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextBlock:
    kind: str
    title: str
    content: str
    source: str | None = None
    token_estimate: int = 0
    truncated: bool = False


@dataclass(frozen=True)
class ContextManifest:
    project_root: str
    blocks: list[ContextBlock]
    excluded_patterns: list[str]
    blocked_paths: list[str]
    warnings: list[str]
```

Start with:

- Tree: first N files after excludes.
- `README.md` if present.
- `AGENTS.md` if present.
- Git status.
- Git diff stat and capped patch.
- Harness dashboard summary from `build_operator_context()`.
- Capability catalog summary.
- Built-in registry summary for agents, workbenches, model profiles, tool policies, and memory scopes.
- Progress summary from orchestration state.
- Recent tasks, leases, runs, and artifact metadata from the SQLite store when initialized.
- Security/policy summary and blocked-state explanation glossary.
- Safety boundary text.

Reuse:

- `DEFAULT_CONTEXT_EXCLUDES` from `src/harness/config.py`.
- Secret path checks from `src/harness/security.py`.
- Path helpers from `src/harness/paths.py`.

### Acceptance Criteria

- "explain this repo" uses actual repo context.
- "what agents do we have?", "explain the security layer", and "what artifacts came out of the last run?" are grounded in Harness state, not generic model knowledge.
- `.harness/`, `.git/`, virtualenvs, caches, build outputs, and secret-like files are excluded.
- The context packer produces a manifest explaining what was included, excluded, blocked, and truncated.
- Context packing is read-only.
- Context packing does not initialize `.harness`.

### Tests

- `test_context_pack_includes_readme_tree_diff`
- `test_context_pack_includes_harness_domain_summary`
- `test_context_pack_includes_agents_adapters_policy_and_artifact_metadata`
- `test_context_pack_obeys_context_excludes`
- `test_context_pack_blocks_secret_paths`
- `test_context_pack_does_not_initialize_project`

## PR 3: Autonomous Tool Gateway, Read-Only Execution First

Goal: introduce the LLM-facing Harness tool gateway as the single interface the assistant will use to operate the Harness backbone. This PR executes only read-only tools, but the gateway must already model the full tool surface so the product direction is clear: chat autonomously manages Harness tools through typed schemas, risk metadata, policy checks, confirmations, and evidence.

Read-only execution is the first autonomous tool class, not the final boundary of chat capability.

The desired behavior after this step:

```text
user:
  explain this repo and find where leasing happens

assistant:
  autonomously chooses repo_tree, search_repo, read_file, show_registered_adapters,
  show_task_graph, show_leases, and explain_policy as needed

assistant:
  answers in normal language with current Harness facts
```

For side-effecting intentions, this PR should not execute the action yet, but the gateway should recognize that those tools exist as gated actions:

```text
user:
  fix the failing chat tests

assistant:
  autonomously inspects with read-only tools
  identifies likely files and test scope
  requests/proposes edit_isolated or run_tests as a side-effecting Harness tool
  Harness reports that an action contract is required
```

That sets up PR 4 without making the user learn internal commands.

### Implementation

Add `src/harness/chat_tools.py`.

This module should define the common tool interface that all LLM-usable Harness capabilities will use. PR 3 should implement read-only execution first, but the registry/schema should include enough metadata to represent side-effecting Harness tools as gated tools.

Suggested types:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol


ChatToolRisk = Literal["read", "control_plane_write", "sandboxed_execution", "repo_mutation"]


@dataclass(frozen=True)
class ChatToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    risk: ChatToolRisk
    requires_confirmation: bool
    evidence_required: bool


@dataclass(frozen=True)
class ChatToolRequest:
    type: str
    tool: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ChatToolResult:
    tool: str
    ok: bool
    content: str
    data: dict[str, Any]
    evidence_refs: list[str]
    error_type: str | None = None


class ChatTool(Protocol):
    spec: ChatToolSpec

    def run(self, request: ChatToolRequest, context: ChatToolContext) -> ChatToolResult:
        ...
```

Initial read-only executable tool set:

```text
repo_tree
read_file
search_repo
show_diff
show_recent_runs
show_progress
show_capabilities
show_task
show_run
show_artifact
explain_policy
list_agents
show_agent
list_workbenches
list_model_profiles
list_tool_policies
list_memory_scopes
show_objectives
show_objective
show_task_graph
show_leases
show_lease
show_registered_adapters
show_adapter
show_approvals
show_security_summary
show_sandbox_profiles
show_trace
show_apply_back_state
explain_blocked_state
```

These should wrap Harness-native read-only capabilities. They should not expose arbitrary shell.

Initial gated side-effect tool specs should also be visible to the model, but not executable in PR 3:

```text
create_objective
create_task
create_task_graph
request_approval
dispatch_registered_adapter
edit_isolated
run_tests
apply_back
deny_apply_back
revert_pending_change
remember
forget_memory
```

These tools should return or require an action-contract path when requested. They must not execute from the raw model request in PR 3.

Potential reuse points:

- `src/harness/tools/readonly.py`
- `src/harness/operator_context.py`
- `src/harness/progress.py`
- `src/harness/tool_capabilities.py`
- `src/harness/security_explanations.py`

Initial protocol can be model-text based because the current local backend only exposes plain chat completions:

```json
{
  "type": "harness.tool_request/v1",
  "tool": "read_file",
  "arguments": {
    "path": "src/harness/chat.py"
  }
}
```

Harness parses the request, validates it, executes the allowed tool, appends the result to the transcript, and asks the model to continue.

The registry should distinguish:

```text
read tools:
  execute immediately after validation

control-plane write tools:
  return action_contract_required in PR 3
  create normalized action contracts and require confirmation in PR 4

sandboxed execution tools:
  return action_contract_required in PR 3
  create normalized action contracts and require confirmation in PR 4

repo mutation tools:
  return action_contract_required in PR 3
  run only through isolated edit/apply-back mechanisms and require confirmation in PR 4+
```

### Acceptance Criteria

- The model can inspect files across multiple turns.
- The model can answer Harness-domain questions about agents, orchestration, security, approvals, artifacts, adapters, and progress by using tools.
- The model receives specs for both read-only tools and gated side-effect tools.
- Read-only tools execute autonomously after validation.
- Gated side-effect tools do not execute yet; they return a structured "action contract required" result.
- Unknown tools fail closed.
- Forbidden paths are blocked by Harness.
- Tool results render in the chat UI as compact observations.
- Tool calls do not create tasks, leases, runs, approvals, or repo mutations.
- The gateway abstraction can represent the full Harness tool surface without bypassing action contracts.

### Tests

- `test_chat_tool_read_file_returns_allowed_file`
- `test_chat_tool_read_file_blocks_secret`
- `test_chat_tool_unknown_tool_rejected`
- `test_chat_loop_can_process_tool_request_then_answer`
- `test_chat_tool_specs_include_risk_and_confirmation_metadata`
- `test_chat_tools_cover_core_harness_domain_surfaces`
- `test_side_effect_tool_specs_are_visible_but_not_executable`
- `test_side_effect_tool_request_returns_action_contract_required`

## PR 4: Side-Effect Tool Contracts

Goal: convert side-effecting LLM tool requests into validated Harness action contracts, then let the user confirm them. This is where chat starts managing the full Harness backbone beyond read-only inspection.

### Implementation

Add `src/harness/action_proposals.py`.

Suggested schema:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ActionProposal:
    source_tool_request: ChatToolRequest
    intent: str
    summary: str
    arguments: dict[str, Any]
    raw_model_payload: dict[str, Any]


@dataclass(frozen=True)
class ActionContract:
    id: str
    tool: str
    risk: ChatToolRisk
    summary: str
    normalized_arguments: dict[str, Any]
    required_confirmations: list[str]
    required_approvals: list[str]
    execution_plan: list[dict[str, Any]]
    evidence_plan: list[str]
    allowed_next_commands: list[str]
    requires_confirmation: bool
```

Expected model tool request:

```json
{
  "type": "harness.tool_request/v1",
  "tool": "edit_isolated",
  "arguments": {
    "goal": "Fix the failing chat tool parser tests",
    "files_hint": [
      "src/harness/chat_tools.py",
      "tests/test_chat_tools.py"
    ]
  }
}
```

Harness rules:

- Parse model tool requests as untrusted input.
- Validate tool name against the gateway registry.
- Validate side-effect tool arguments against schema.
- Validate adapters and task types against registered Harness descriptors.
- Normalize titles and descriptions.
- Recompute required approvals from Harness policy.
- Reject unknown adapters.
- Reject permission broadening.
- Reject arbitrary shell.
- Reject ambient filesystem write.
- Render the normalized `ActionContract`, not the raw model request.

This PR is where the LLM tool gateway expands beyond read-only inspection. Side-effecting tool requests become normalized action contracts.

Examples:

```json
{
  "type": "harness.tool_request/v1",
  "tool": "edit_isolated",
  "arguments": {
    "goal": "Update the chat layer to use the local model runtime",
    "files_hint": ["src/harness/chat.py", "src/harness/chat_model.py"]
  }
}
```

```json
{
  "type": "harness.tool_request/v1",
  "tool": "run_tests",
  "arguments": {
    "scope": "chat",
    "suggested_command": "pytest tests/test_chat_model.py tests/test_operator_chat_path.py"
  }
}
```

Harness must not execute these directly from the model request. It should normalize them into an action contract, render that contract to the user, require confirmation where needed, and then execute through the existing Harness control plane.

### Acceptance Criteria

- The assistant can say "I can make this change in an isolated workspace" and show a real Harness action proposal.
- The assistant can request side-effecting Harness tools such as `create_task`, `edit_isolated`, `run_tests`, and `apply_back`, but Harness converts those requests into validated action contracts.
- Confirming the action creates Harness records through existing task/objective paths.
- Model-proposed unknown adapters are rejected.
- Model-proposed permission broadening is ignored or rejected.
- Hosted-provider requirements are computed by Harness, not trusted from the model.

### Tests

- `test_action_proposal_validates_known_adapter`
- `test_action_proposal_rejects_unknown_adapter`
- `test_action_proposal_cannot_broaden_permissions`
- `test_action_contract_recomputes_required_approvals`
- `test_confirmed_action_creates_real_task_records`
- `test_side_effect_tool_request_becomes_action_contract`
- `test_side_effect_tool_request_does_not_execute_without_confirmation`

## PR 5: Autonomous Act Loop

Goal: make the session feel like one assistant workflow where the LLM can manage Harness tools autonomously after the user confirms the action boundary.

Status: implemented as a bounded chat mode. `/act <request>` routes through the LLM tool gateway with read-only tool loops enabled and side effects converted into action contracts. `/test`, `/diff`, `/apply`, and `/revert` are first-class chat commands that map back into Harness control-plane behavior rather than broad shell access.

### Modes

```text
normal:
  LLM can answer using packed context.
  May autonomously request read-only chat tools.
  May request side-effect Harness tools, which become action contracts.
  No side effects without confirmation.

act:
  LLM can run bounded read-only tool loops automatically.
  May request side-effect Harness tools.
  Side-effect tool requests become action contracts.
  Side effects execute only after confirmation.
  The LLM may sequence confirmed Harness-backed operations.
  Edits happen through isolated adapters.
  Tests happen through sandbox mechanisms.
  Apply-back requires separate approval.
```

### Slash Commands

```text
/plan
/act
/diff
/test
/apply
/revert
```

Mappings:

- `/plan`: ask LLM for a plan, optionally producing an `ActionProposal`.
- `/act`: allow bounded read-only inspect loop and action proposal.
- `/diff`: show latest isolated diff artifact or current git diff.
- `/test`: propose or run Harness-backed sandbox test action.
- `/apply`: apply-back review flow.
- `/revert`: revert pending isolated/apply-back state where Harness has a record.

This should reuse existing pending draft, orchestration draft, confirmation, apply-back, objective/task graph, and adapter dispatch mechanisms in `src/harness/chat.py`.

The trigger should become model/action-contract driven, not intent-string driven.

### Acceptance Criteria

- "fix this failing test" can become a bounded assistant loop.
- The assistant can inspect relevant files before proposing work.
- The assistant can use the full Harness backbone through the LLM tool gateway.
- The user sees a clear action contract before mutation.
- Edits still occur in isolated workspace.
- Tests still use sandbox mechanisms.
- Apply-back remains a separate approval.

### Tests

- `test_act_mode_allows_readonly_tool_loop`
- `test_act_mode_can_request_side_effect_harness_tools`
- `test_act_mode_requires_confirmation_for_edit`
- `test_apply_requires_separate_confirmation`
- `test_fix_request_creates_action_contract_not_immediate_mutation`

## PR 6: Workflow Templates Become Action Builders

Goal: keep `src/harness/workflow_templates.py`, but stop treating it as the main intelligence.

Status: implemented for `create_task_graph` action contracts. The LLM can request a known `template_id`; Harness validates the template, expands it into normalized tasks, preserves dependencies, rejects unknown templates, and creates records only after confirmation.

Current role:

```text
natural language phrase -> deterministic template -> task graph
```

New role:

```text
LLM action proposal -> Harness selects/normalizes template -> task graph
```

Example:

```text
LLM:
  This is a coding change. Use coding_fix template with planning first and isolated edit second.

Harness:
  Validates requested template.
  Normalizes task graph.
  Applies policy.
  Shows action contract.
```

Keep initial templates:

- repo summary
- repo planning
- coding fix

Possible later templates:

- failing test fix
- refactor with tests
- docs update
- security review
- dependency investigation

### Acceptance Criteria

- Existing templates remain testable deterministically.
- The LLM can choose from templates.
- The LLM cannot invent unrestricted graph behavior.
- Harness validates the final graph.

### Tests

- `test_llm_can_select_known_workflow_template`
- `test_unknown_workflow_template_rejected`
- `test_template_output_policy_checked_before_confirm`

## PR 7: TUI Becomes Primary Product Surface

Goal: make the Textual app feel like a coding/research assistant, not a dashboard with a prompt.

Target layout:

```text
left:
  file/context/task tree

center:
  LLM chat

right:
  action contract / runs / artifacts / diff / approvals

bottom:
  status, model, mode, policy boundary
```

Necessary changes:

- Chat transcript becomes the central object.
- Tool calls render as compact observations.
- Action proposals render as structured panels.
- Confirmation prompts are prominent.
- Current model profile is visible.
- Local endpoint unavailable state is clear.
- The old command palette can remain, but it is not the main conceptual model.
- The right panel starts with assistant state and action state before project/task details.
- The UI describes read-only tools as autonomous assistant tools and side effects as action contracts.

### Acceptance Criteria

- Bare `harness` opens into assistant-first UI.
- The user can operate without knowing task, lease, objective, or adapter command names.
- The right panel reveals Harness structure only when there is an action, run, artifact, diff, approval, or blocked state.

### Tests

- Update existing TUI smoke tests.
- Add state/render tests for model, mode, action proposal, and approval state where feasible.

## PR 8: Optional Hosted Chat

Goal: add hosted chat only after local chat works.

Product rule:

```text
configured non-paid chat failure -> tell user chat model unavailable
not -> silently fall back to paid hosted API
```

Config:

```yaml
chat:
  default_model_profile: codex_cli
  allow_hosted_chat: false
  hosted_chat_requires_approval: true
```

Hosted enablement requires:

- explicit config
- `paid_openai_compatible.settings.enabled: true`
- data-boundary approval
- clear UI/status indicator
- no hidden fallback

### Acceptance Criteria

- Hosted chat cannot be used by default.
- Hosted chat requires config and approval.
- Context sent to hosted chat is manifest-backed and policy-filtered.
- The user can see when hosted chat is active.

### Tests

- `test_paid_chat_disabled_by_default`
- `test_paid_chat_requires_explicit_config`
- `test_paid_chat_requires_data_boundary_approval`
- `test_no_silent_hosted_fallback`

## PR 9: Chat Evidence

Goal: preserve trust without making every conversation a heavyweight Harness task.

Add lightweight chat evidence:

```text
.harness/chat_sessions/<session_id>.jsonl
```

Events:

```text
session_started
model_profile_used
context_manifest_created
user_message
assistant_message
tool_request
tool_result
action_proposal_raw
action_contract_normalized
user_confirmation
task_created
run_created
artifact_linked
apply_back_requested
apply_back_confirmed
session_ended
```

Important distinction:

- Ordinary conversation gets lightweight chat evidence.
- Actual work still gets tasks, runs, and artifacts.

### Acceptance Criteria

- Chat actions can be audited.
- Context manifest is recorded.
- Tool calls are recorded.
- User confirmations are recorded.
- Linked task/run/artifact ids are recorded.
- Conversational turns do not create task records.

### Tests

- `test_chat_session_writes_jsonl_evidence`
- `test_context_manifest_linked_to_model_call`
- `test_action_confirmation_recorded`
- `test_conversation_does_not_create_task_records`

## Docs And Tests To Update

Docs that currently encode the old product target:

- `README.md`
- `docs/operator_guide.md`
- `docs/command_catalog.md`
- `docs/smoke_checklist.md`

Old claims to remove or revise:

- deterministic chat actions
- chat does not call model backends directly
- passive dashboard first
- codex-like mode as a special testing-oriented foreground mode rather than the core UX

Replacement framing:

```text
Harness is a local-first terminal LLM assistant.
The chat layer uses a configured local model by default.
Harness controls context, tools, approvals, side effects, evidence, and apply-back.
```

Tests that need rework:

- `tests/test_operator_chat_path.py`
- TUI tests that assert passive-only dashboard behavior.
- Tests that monkeypatch `LocalOpenAICompatibleBackend` to ensure it is not called during chat.

Those tests should be inverted for free-form chat, while still asserting no model calls for `--output json`, passive metadata probes, and deterministic slash commands that should remain local-only.

## Suggested Implementation Order

- [x] Add `ChatModel` abstraction and Codex/local model chat paths.
- [x] Add `chat` config.
- [x] Change plain chat non-slash input to call configured chat model.
- [x] Keep deterministic slash commands.
- [x] Add context packer.
- [x] Wire context packer into model messages.
- [x] Add read-only tool request/response loop.
- [x] Add action proposal schema and validation.
- [x] Route confirmed action contracts into task/objective/adapter/test/apply-back machinery.
- [x] Add autonomous `/act` loop and slash mappings for plan/diff/test/apply/revert.
- [x] Convert workflow templates into validated action builders.
- [x] Rework TUI around assistant-first interaction.
- [ ] Add hosted chat only after local path is stable.
- [ ] Add chat evidence.

## First PR File Scope

Likely files:

```text
src/harness/chat_model.py
src/harness/config.py
src/harness/chat.py
src/harness/cli/main.py
tests/test_chat_model.py
tests/test_operator_chat_path.py
docs/operator_guide.md
docs/command_catalog.md
README.md
```

## Second PR File Scope

Likely files:

```text
src/harness/context_pack.py
src/harness/chat_model.py
src/harness/chat.py
tests/test_context_pack.py
docs/operator_guide.md
```

## Third PR File Scope

Likely files:

```text
src/harness/chat_tools.py
src/harness/tools/readonly.py
src/harness/chat.py
tests/test_chat_tools.py
```

## Non-Goals

- Do not let `codex_cli` chat mutate the active repo directly.
- Do not expose arbitrary shell as a chat tool.
- Do not make every chat turn a task/run.
- Do not preserve deterministic `route_chat_intent()` as the primary brain.
- Do not silently call hosted models when local chat fails.
- Do not let model-generated JSON directly create tasks or runs.

## Final Product Definition

Harness is a local-first opencode-style coding and research assistant.

The LLM is the operator-facing intelligence.

Harness is the authority layer.

The user experience is natural chat. Internally, Harness decides what context the model sees, what tools it can call, what requires confirmation, where edits happen, what gets recorded, and how changes reach the active repo.

The user should be able to ask about orchestration, agents, security, artifacts, runs, leases, adapters, approvals, policy, and progress directly in chat. The LLM should use the context packer and Harness tool gateway to answer with current project facts and to drive the existing backbone when action is needed.
