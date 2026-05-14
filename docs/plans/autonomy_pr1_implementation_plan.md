# Autonomy PR 1 Implementation Plan

This plan is for the next implementation session. It focuses on the first safe slice of the autonomy roadmap: adding the autonomy policy and decision foundation without changing runtime chat behavior.

## Goal

Implement the foundation for policy-backed autonomous authorization.

By the end of this session, Harness should support inspecting built-in autonomy profiles and evaluating mock action requests against a pure autonomy evaluator.

Target CLI shape:

```bash
harness autonomy policy inspect --project . --profile safe-local --output json
```

Target evaluator result shape:

```json
{
  "status": "auto_allowed",
  "profile": "safe-local",
  "tool": "create_task",
  "reasons": [
    "tool risk is allowed by profile",
    "boundary is local_control_plane"
  ],
  "requires_human": false
}
```

This PR must not auto-execute anything. Manual confirmation remains the default runtime behavior.

## Scope

In scope:

- Add autonomy domain models.
- Add built-in autonomy profiles.
- Add a pure evaluator that can explain decisions.
- Add CLI inspection for built-in profiles.
- Add focused tests proving the evaluator cannot expand authority.
- Add a short documentation note if there is an obvious place.

Out of scope:

- Executing pending action contracts automatically.
- Changing chat confirmation behavior.
- Adding autonomous chat loops.
- Adding objective runners.
- Changing adapter descriptors.
- Auto-dispatching Codex isolated edits.
- Changing apply-back behavior.
- Changing daemon behavior.
- Expanding approval profile behavior.

## Step 1: Re-Orient In The Repo

Start by finding the current policy, chat, approvals, and execution patterns:

```bash
rg "ActionContract|action_contract|pending_action|approval|SecurityDecision|EffectivePolicy|ChatToolRisk|Boundary" src tests
rg "Typer|app.command|policy inspect|approvals" src/harness
rg "MAX_CHAT_TOOL_CALLS|tool_request|dispatch_adapter|apply_back" src/harness
```

Files likely worth opening first:

- `src/harness/chat.py`
- `src/harness/chat_tools.py`
- `src/harness/chat_model.py`
- `src/harness/execution.py`
- `src/harness/policy.py`
- `src/harness/approvals.py`
- CLI entrypoint file, likely `src/harness/cli.py` or equivalent
- Existing tests around chat tools, execution policy, approvals, and CLI

The goal of this step is to reuse existing enums, schemas, policy hashes, approval concepts, and CLI conventions. Do not redesign these files in PR 1.

## Step 2: Reuse Existing Types Where Possible

Before creating new enums, check whether the repo already has equivalents for:

- Risk level.
- Boundary kind.
- Approval status.
- Policy decision.
- Adapter id.
- Task type.
- Backend id.
- Sandbox profile.
- Security decision.
- Effective policy.

Prefer importing existing types where stable.

Only add autonomy-specific types when they represent new concepts:

- `AutonomyPolicy`
- `AutonomyDecision`
- `AutonomousApprovalRecord`
- `AutonomyBudget`
- `AutonomyScope`
- `AutonomyViolation`
- `AutonomyProfileId`
- `AutonomyDecisionStatus`

If the repo already uses plain strings instead of enums in the relevant area, follow the local pattern. Do not introduce a broad enum refactor in this PR.

## Step 3: Add `src/harness/autonomy.py`

Create a new module focused on pure policy evaluation.

Recommended shape:

```python
from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class AutonomyDecisionStatus(StrEnum):
    AUTO_ALLOWED = "auto_allowed"
    APPROVAL_REQUIRED = "approval_required"
    DENIED = "denied"
    BUDGET_EXCEEDED = "budget_exceeded"
    POLICY_MISMATCH = "policy_mismatch"
    REQUIRES_HUMAN_BOUNDARY = "requires_human_boundary"


class AutonomyScope(StrEnum):
    PROJECT = "project"
    WORKBENCH = "workbench"
    AGENT = "agent"
    OBJECTIVE = "objective"
    TASK = "task"


class AutonomyBudget(BaseModel):
    max_model_turns: int = 20
    max_tool_calls: int = 60
    max_side_effect_actions: int = 10
    max_runtime_seconds: int = 900
    max_adapter_dispatches: int = 3
    max_new_tasks: int = 10
    max_consecutive_failures: int = 2
    max_cost_usd: Decimal | None = None


class AutonomyPolicy(BaseModel):
    schema_version: Literal["harness.autonomy_policy/v1"] = "harness.autonomy_policy/v1"
    id: str
    scope: AutonomyScope = AutonomyScope.PROJECT

    allowed_tools: list[str] = Field(default_factory=list)
    allowed_adapters: list[str] = Field(default_factory=list)
    allowed_task_types: list[str] = Field(default_factory=list)
    allowed_boundaries: list[str] = Field(default_factory=list)

    auto_confirm_risks: list[str] = Field(default_factory=list)
    pause_on_risks: list[str] = Field(default_factory=list)
    forbidden_risks: list[str] = Field(default_factory=list)

    budget: AutonomyBudget = Field(default_factory=AutonomyBudget)

    require_evidence: bool = True
    require_idempotency: bool = True
    require_sandbox: bool = True

    allow_create_objectives: bool = False
    allow_create_tasks: bool = False
    allow_memory_writes: bool = False
    allow_adapter_dispatch: bool = False
    allow_active_repo_mutation: bool = False


class AutonomyDecision(BaseModel):
    schema_version: Literal["harness.autonomy_decision/v1"] = "harness.autonomy_decision/v1"
    status: AutonomyDecisionStatus
    policy_id: str
    tool_name: str | None = None
    adapter_id: str | None = None
    task_type: str | None = None
    boundary: str | None = None
    risk: str | None = None
    reasons: list[str] = Field(default_factory=list)
    requires_human: bool = False
    evidence_required: bool = True
```

Adjust names and field types to fit the existing codebase. The model above is a target shape, not a strict requirement.

## Step 4: Add Built-In Profiles

Add a function like:

```python
def builtin_autonomy_policies() -> dict[str, AutonomyPolicy]:
    ...
```

Initial profiles:

- `manual`
- `safe-local`
- `supervised-codex`
- `daemon-safe`

### `manual`

Expected behavior:

- Read-only decisions can be reported as allowed if the evaluator is used directly.
- Side-effect action contracts require approval.
- Adapter dispatch requires approval.
- Active repo mutation is not auto-allowed.

### `safe-local`

Expected behavior:

- Auto-allow read-only tools.
- Auto-allow local Harness control-plane writes.
- Auto-allow the `dry_run` adapter.
- Auto-allow progress, review, and summary style local records.
- Pause hosted, provider, or Codex boundaries.
- Deny active repo apply-back.

### `supervised-codex`

Expected behavior:

- Include `safe-local` behavior.
- Allow `repo_planning` and `codex_isolated_edit` only when the caller provides evidence of scoped hosted approval.
- Keep active repo apply-back denied or approval-required.

### `daemon-safe`

Expected behavior:

- Use the same basic risk posture as `safe-local`.
- Use stricter budgets.
- Be suitable for later daemon objective loops.

For PR 1, hosted approval can be represented as evaluator input. It does not need to be fully wired to the approvals store.

## Step 5: Add Evaluator Input

Add an input model similar to:

```python
class AutonomyEvaluationInput(BaseModel):
    tool_name: str
    risk: str
    boundary: str
    adapter_id: str | None = None
    task_type: str | None = None
    has_scoped_approval: bool = False
    would_mutate_active_repo: bool = False
    requires_network: bool = False
    requires_paid_or_hosted_boundary: bool = False
    idempotency_key: str | None = None
    evidence_contract: str | None = None
```

The evaluator should remain pure and testable. It should not inspect chat state or execute anything.

## Step 6: Implement `evaluate_autonomy`

Add a pure decision function:

```python
def evaluate_autonomy(
    policy: AutonomyPolicy,
    request: AutonomyEvaluationInput,
) -> AutonomyDecision:
    ...
```

Use a conservative evaluation order:

1. Runtime kill switch or disabled profile means approval-required or denied.
2. Active repo mutation is denied unless policy explicitly permits it.
3. Unknown tool is denied or approval-required depending on profile.
4. Forbidden risk is denied.
5. Human boundary risk is approval-required.
6. Hosted, paid, or network boundary requires scoped approval.
7. Adapter dispatch requires adapter allowlist.
8. Task creation requires task type allowlist.
9. Idempotency is required for side effects if policy requires it.
10. Evidence contract is required if policy requires it.
11. Budget exhaustion returns `budget_exceeded`.
12. If all checks pass, return `auto_allowed`.

Important invariant:

```text
Autonomy can narrow authority, pause, or deny.
Autonomy cannot broaden EffectivePolicy, sandbox permissions, approval scope, or adapter capabilities.
```

Represent this directly in code comments and tests.

## Step 7: Add CLI Policy Inspect

Add a CLI command following the existing Typer style.

Preferred command:

```bash
harness autonomy policy inspect --project . --profile safe-local --output json
```

If the current CLI structure does not support that nesting cleanly, use the repo's existing command pattern and keep naming close:

```bash
harness autonomy inspect --profile safe-local --output json
```

The command should:

- Load a built-in profile by id.
- Optionally consider project config if the repo already has project config loading.
- Print JSON or text using existing output helpers.
- Fail clearly for unknown profiles.

Text output can be concise:

```text
Profile: safe-local
Scope: project
Auto tools: ...
Auto adapters: dry_run
Paused risks: hosted_provider, active_repo_mutation
Denied risks: external_side_effect, paid_action, personal_message
Budgets: ...
```

JSON output should serialize the Pydantic model directly.

## Step 8: Add Tests

Focus tests on the evaluator and CLI.

Core tests:

- `test_builtin_autonomy_profiles_exist`
- `test_manual_profile_requires_approval_for_side_effect`
- `test_safe_local_allows_read_only_tool`
- `test_safe_local_allows_local_control_plane_task_creation`
- `test_safe_local_denies_active_repo_apply_back`
- `test_safe_local_denies_unknown_adapter`
- `test_supervised_codex_pauses_without_hosted_approval`
- `test_supervised_codex_allows_codex_isolated_edit_with_scoped_approval`
- `test_autonomy_requires_idempotency_for_side_effects`
- `test_autonomy_requires_evidence_when_policy_requires_evidence`
- `test_autonomy_budget_exhaustion_returns_budget_exceeded`
- `test_autonomy_cannot_broaden_effective_policy`
- `test_autonomy_policy_inspect_json_cli`

Recommended test style:

```text
1. Construct a policy from a built-in profile.
2. Construct `AutonomyEvaluationInput`.
3. Call `evaluate_autonomy`.
4. Assert `decision.status`.
5. Assert the decision reasons are explainable.
```

Do not use a real model. Do not dispatch real adapters. Do not mutate repo state.

## Step 9: Add Minimal Documentation

Add a short section where the repo already documents policy, approvals, or chat behavior.

Suggested copy:

```markdown
### Autonomy Profiles

Harness autonomy profiles do not grant new authority. They decide whether a
validated action contract can proceed without live confirmation inside the
current EffectivePolicy, sandbox, approval scope, leases, budgets, and adapter
capabilities.

The default profile is `manual`, which preserves interactive confirmation.
The initial non-manual profiles are `safe-local`, `supervised-codex`, and
`daemon-safe`.
```

Avoid promising daemon behavior or autonomous implementation before those PRs land.

## Step 10: Verify

Run focused checks first:

```bash
pytest tests/test_autonomy.py
pytest tests/test_cli*.py -k autonomy
```

Then run nearby suites:

```bash
pytest tests -k "policy or approval or chat_tools or execution or autonomy"
```

If the full suite is reasonably fast:

```bash
pytest
```

Manually inspect CLI output:

```bash
harness autonomy policy inspect --project . --profile manual --output json
harness autonomy policy inspect --project . --profile safe-local --output json
harness autonomy policy inspect --project . --profile supervised-codex
```

## Expected Final State

The final report for this implementation session should be able to say:

```text
Implemented the autonomy policy foundation without changing chat execution behavior.

Added:
- AutonomyPolicy, AutonomyBudget, AutonomyDecision, and evaluator input models.
- Built-in profiles: manual, safe-local, supervised-codex, daemon-safe.
- Pure evaluate_autonomy(...) decision function.
- CLI policy inspection.
- Tests for allowed, paused, denied, budget, idempotency, evidence, and active repo mutation cases.

Not changed:
- Chat still requires confirmation for side-effect action contracts.
- No adapter is newly auto-dispatched.
- No active repo mutation path changed.
```

## Suggested Commit

Use one focused commit:

```text
Add autonomy policy decision foundation
```

Expected commit contents:

- `src/harness/autonomy.py`
- CLI wiring file
- `tests/test_autonomy.py`
- CLI autonomy test file, if separate
- Documentation update, if appropriate

Avoid formatting churn and unrelated test repairs.

## Risks

The main risk is accidentally wiring the new evaluator into runtime behavior before the policy model is stable. Keep PR 1 observational and inspectable only.

The second risk is duplicating existing policy concepts. If the repo already has `SecurityDecision`, `EffectivePolicy`, `BoundaryKind`, or risk enums, reuse them or add thin adapters. Autonomy should sit above existing controls, not become a competing policy engine.

The third risk is overfitting profile names to future behavior. In this PR, `supervised-codex` can exist as a declared profile, but its evaluator should require explicit `has_scoped_approval=True` in test inputs before allowing anything hosted or Codex-specific.

## Checklist

- [ ] Read current policy, approvals, chat tool, execution, and CLI patterns.
- [ ] Identify existing risk, boundary, and approval types to reuse.
- [ ] Add `src/harness/autonomy.py` with models and built-in profiles.
- [ ] Implement pure `evaluate_autonomy(...)` with conservative decision ordering.
- [ ] Add CLI policy inspect command.
- [ ] Add evaluator unit tests.
- [ ] Add CLI JSON output test.
- [ ] Add a short docs section if the repo has an obvious place.
- [ ] Run focused tests.
- [ ] Run nearby policy, chat, and execution tests.
- [ ] Confirm no runtime chat behavior changed.

## Follow-On PR 2 Preview

Once PR 1 lands, the next implementation session should wire existing pending action contracts through the evaluator:

```text
pending_action_contract
  -> evaluate_autonomy(...)
  -> auto_allowed: execute and record AutonomousApprovalRecord
  -> approval_required: preserve current confirmation path
  -> denied: return blocked/denied explanation
```

That second PR is where behavior should start to change, behind an explicit `--autonomous` or selected autonomy profile.
