# Toloclaw Governance Parity Plan

Status: complete

Reference source: `.harness/reference-code/toloclaw-harness-delivery/toloclaw-harness-source`

Goal: wire Harness with the same governance model shipped in Toloclaw while keeping Harness naming, schemas, storage, and existing local-first execution paths coherent. Provider CLIs and adapters may execute work, but Harness must own permissions, sessions, state, budgets, traces, apply-back, test evidence, and merge decisions.

## Progress

- [x] Slice 1: governance package skeleton, canonical gate registry, canonical protected apply-back path policy, `harness governance gates`, and first consumers in managed-action and patch validation.
- [x] Slice 2: governed task records and CLI.
- [x] Slice 3: governance context packs.
- [x] Slice 4: governance test plan and evidence.
- [x] Slice 5: merge-check.
- [x] Slice 6: data inventory and retention audit.
- [x] Slice 7: network and isolation governance.
- [x] Slice 8: apply-back and promotion integration.
- [x] Slice 9: command catalog and operator docs.

## Non-Negotiable Governance Semantics

- Harness is the authority layer. Backends, provider CLIs, MCP tools, browser tools, Docker, and task adapters are execution surfaces only.
- Delegated implementation work uses one governed task branch or isolated worktree per task.
- Every governed task records owner agent/model, goal, base SHA, branch, worktree, allowed paths, permission profile, sandbox profile, expected artifacts, context-pack hash, tests, and merge-check verdict.
- Agents may run evidence-producing governance commands, but the operator keeps final authority over merges, pushes, protected infrastructure, provider settings, permission widening, external services, Docker/network/MCP/browser/email/calendar access, and irreversible side effects.
- Governance commands are local-first, deterministic where possible, side-effect minimal, and explicit about any evidence they write.
- Apply-back and hosted-provider approval are separate boundaries. Hosted-provider approval never implies active-repo mutation approval.
- Cleanup, promotion, merge, push, external delivery, and protected-infrastructure changes remain propose-only unless a separate explicit approval path permits them.

## Reference Contracts To Port

Port these Toloclaw concepts into Harness with equivalent behavior:

| Toloclaw contract | Harness target |
| --- | --- |
| `toloclaw.governance.gate_registry/v1` | `harness.governance.gate_registry/v1` |
| `toloclaw.task/v1` | `harness.governance_task/v1` or an extension of existing `TaskRecord` metadata |
| `toloclaw.context_pack/v1` | `harness.governance_context_pack/v1`, integrated with existing `harness.context_pack` |
| `toloclaw.test_plan/v1` | `harness.governance_test_plan/v1`, integrated with test/eval commands |
| `toloclaw.merge_check/v1` | `harness.governance.merge_check/v1` |
| `toloclaw.data_inventory/v1` | `harness.data_inventory/v1` |
| `.toloclaw/governance/...` | `.harness/governance/...` |

Do not import Toloclaw package names into runtime code. The parity is behavioral and contractual; Harness code should use `harness.*` modules and schemas.

## Hard Gates

The first Harness gate registry should preserve Toloclaw gate ids unless a Harness-specific id already exists and has the same meaning. Each gate must have an id, description, layer, severity on failure, and source.

Required gates:

- `input_task_scope_declared`: task declares goal, owner, permissions, sandbox, and expected artifacts.
- `sandbox_capabilities_declared`: sandbox/tool capabilities are explicit and auditable.
- `no_protected_writes`: protected infrastructure paths are not modified without approval.
- `no_secret_in_diff`: added diff lines contain no secret-like values.
- `no_dangerous_subprocess_strings`: added diff lines contain no dangerous execution strings.
- `tests_pass`: required Harness tests pass before merge.
- `merge_base_resolves`: base and branch resolve to a common merge base.
- `branch_contains_current_base`: branch is not behind the current integration base.
- `no_mass_deletion_shape`: branch diff is not dominated by deletions.
- `no_core_workspace_deletions`: branch does not delete core workspace files.
- `diff_size_bounded`: branch diff is small enough for reliable local review.
- `no_vendored_third_party_diff`: branch does not mix vendored third-party material into governance merge scope.
- `context_retrieval_uses_compiler`: new workspace-context retrieval goes through Harness context retrieval/pack components or declares a justified exception.
- `context_budget_enforced`: new prompt assembly uses Harness context budget enforcement or declares a justified exception.
- `no_provider_permission_widening`: provider/backend configs do not widen authority.
- `no_unsafe_sandbox_network_change`: sandbox profiles do not enable broader network access.
- `allowed_paths_respected`: segment changes stay within declared allowed paths.
- `segment_context_pack_present`: segment has a context pack or equivalent mission brief evidence.
- `test_evidence_fresh`: segment has fresh passing test evidence.
- `applyback_bound_to_segment`: apply-back or promotion evidence is bound to a governed segment.
- `checkpoint_approved`: required mission checkpoint has approval or passing deterministic verdict.
- `isolation_transition_approved`: isolation escalation has recorded reason and approval evidence.
- `network_policy_valid`: network-enabled isolation has a scoped allowlist, logging, quarantine, and approval evidence.
- `artifact_quarantined`: artifacts from elevated isolation are quarantined before promotion.
- `promotion_paths_within_scope`: promoted changes stay within allowed path scope.
- `promotion_not_quarantined`: quarantined artifacts are not promoted into trusted workspace state.
- `promotion_tests_current`: promotion has passing, fresh test evidence bound to the task.
- `promotion_segment_bound`: promotion evidence is bound to the expected task segment.
- `promotion_network_policy_valid`: promotion uses a valid no-network or explicitly disabled network policy.
- `post_merge_audit_recorded`: merged work has durable provenance evidence.

## Protected Path Policy

Create a single source of truth for protected apply-back paths, then make merge-check, session tools, managed actions, and isolated apply-back import it instead of copying the list.

Initial protected path categories:

- Harness governance and policy code: `src/harness/policy.py`, `src/harness/action_policy.py`, `src/harness/approvals.py`, `src/harness/security.py`, `src/harness/security_explanations.py`, and new `src/harness/governance/**`.
- Runtime and adapter authority: `src/harness/core_service.py`, `src/harness/execution.py`, `src/harness/daemon_adapters.py`, `src/harness/session_tools.py`, `src/harness/sandbox/**`, `src/harness/backends/**`.
- Built-in specs and permissions: `src/harness/builtin_specs/**`.
- Config, packaging, and install control: `pyproject.toml`, lockfiles, `.github/**`, `.agents/**`, `.codex/**`, `.harness/reference-code/**`.
- Persistent state and evidence roots: `.harness/governance/**`, `.harness/approvals/**`, `.harness/autonomy/**`, `.harness/runs/**`, unless the command is explicitly evidence-writing.
- Documentation that defines safety behavior: `docs/command_catalog.md`, `docs/session_tool_catalog.md`, `docs/operator_guide.md`, `docs/smoke_checklist.md`, `docs/plans/**`.

The policy should support an explicit exception record in governance evidence rather than ad hoc bypasses.

## Implementation Slices

### Slice 1: Governance Package Skeleton

Add `src/harness/governance/` with pure, testable modules:

- `gate_registry.py`: canonical gate specs, schema payload, `require_known_gate`.
- `protected_paths.py`: protected apply-back patterns and path matching helpers.
- `models.py`: Pydantic or dataclass schemas for task, context pack, test plan, merge-check, data inventory.
- `paths.py`: `.harness/governance` directory layout and run-id helpers.

Acceptance:

- `harness governance gates --output json` emits `harness.governance.gate_registry/v1`.
- Tests prove unknown gates fail closed.
- Tests prove protected pattern consumers import the canonical tuple/list from one place.

### Slice 2: Governed Task Records

Wire Toloclaw-style governance metadata into the existing SQLite task/session model rather than creating a second task system unless the current model cannot represent the contract.

Required metadata:

- `schema_version`
- `task_id`
- `slug`
- `branch`
- `base`
- `base_sha`
- `worktree_path`
- `session_id`
- `agent`
- `model_profile`
- `permission_profile`
- `sandbox_profile`
- `goal`
- `allowed_paths`
- `expected_artifacts`
- `context_pack_hash`
- `latest_test_run_path`
- `latest_merge_check_verdict`
- `status`
- `created_at`

CLI targets:

```bash
harness governance tasks create <slug> --agent <id> --goal <text> --base main --project .
harness governance tasks list --project . --output json
harness governance tasks show <task_id> --project . --output json
harness governance tasks close <task_id> --project .
```

Acceptance:

- Task creation records all governance fields.
- Task creation creates or links a Harness session with `surface=governance`.
- Task creation refuses unknown agents, missing base refs, unsafe slugs, and dirty worktree conditions when a worktree would be created.
- Task records are sanitized before persistence.

### Slice 3: Governance Context Packs

Add a governance context-pack builder that reuses existing Harness context modules where possible.

Required contents:

- task metadata
- relevant Harness instructions and operator docs
- current branch diff, capped and redacted
- latest decisions/evidence summaries
- active permissions and approval profiles
- sandbox constraints and capability catalog
- gate registry
- required test plan
- explicit exclusions: secrets, raw provider logs, unrelated inbox material, unapproved protected-infrastructure rewrites
- deterministic `sha256`

CLI target:

```bash
harness governance context build --task <task_id> --project . --output json
```

Acceptance:

- Secret-looking content is redacted with `harness.security.sanitize_for_logging`.
- The context pack hash is written back to the governed task.
- Context pack generation never calls providers, mutates repo files, or executes arbitrary tools.

### Slice 4: Test Plan And Evidence

Add a governance test planner that maps task scope to required local checks.

Initial matrix:

- governance/security change: targeted governance tests, security tests, integrity check
- CLI command change: CLI tests plus command catalog contract tests
- session tool or permission change: session tool tests and policy projection tests
- adapter/runtime change: adapter, lease, core service, and runtime evidence tests
- docs-only governance change: markdown lint if available, plus contract consistency checks

CLI targets:

```bash
harness governance tests plan <task_id> --project . --output json
harness governance tests run <task_id> --project . --output json
```

Acceptance:

- Plans emit `harness.governance_test_plan/v1`.
- Test runs persist stdout/stderr summaries and full logs under `.harness/governance/tests/<run_id>/`.
- Test evidence links back to task id, base sha, branch, policy hash, and gate ids.

### Slice 5: Merge-Check

Implement `harness governance merge-check <branch> --base <base> --project . --output json` as the canonical branch integration gate.

The command collects evidence and emits a verdict. It must not push, merge, comment on PRs, modify the branch, append decisions, or call providers.

Output schema:

- `schema_version`: `harness.governance.merge_check/v1`
- `run_id`
- `generated_at`
- `branch`
- `base`
- `head_sha`
- `base_sha`
- `verdict`: `approve | request_changes | reject | error`
- `reason`
- `summary`
- `hard_gates`
- `soft_findings`
- `evidence`
- `remediations`
- `operator_authority`
- `report_links`

Hard gate checks:

- protected writes
- secret findings in added diff lines
- dangerous subprocess strings
- merge base resolves
- branch contains current base
- required tests pass
- no workspace authority drift
- no provider/backend permission widening
- no unsafe sandbox/network widening
- no mass deletion shape
- no core workspace deletions

Soft findings:

- test count drop
- new production module without corresponding test
- large diff with subject-only commits
- doc/code drift
- new top-level dependency
- diff size near limit

Exit codes:

- `0`: approve
- `2`: request changes
- `3`: reject
- `1`: operational error

Evidence layout:

```text
.harness/governance/merge-check/
  <run_id>/
    verdict.json
    pytest.log
    diff.patch
    diff_files.txt
    drift.json
    secret_scan.json
    commits.json
```

Acceptance:

- Merge-check always writes evidence and optionally prints JSON.
- A reject verdict cannot be downgraded by flags.
- `--strict` can only escalate warnings.
- Dirty worktrees, missing branches, and missing bases return operational error.
- The command uses the same protected path source as apply-back/session tooling.

### Slice 6: Data Inventory And Retention Audit

Port Toloclaw's propose-only data policy to Harness.

Retention classes:

- `canonical_decision`: keep indefinitely.
- `compact_receipt`: keep for 365 days.
- `raw_execution_log`: keep for 14 days, or 30 days for failed runs.
- `replay_debug_bundle`: keep for 30 days unless pinned to active debugging.
- `temp_isolation_manifest`: keep for 7 days unless referenced by a failed run or open task.
- `generated_preview_artifact`: keep for 30 days unless promoted in the artifact registry.
- `unknown_generated_data`: manual review.

CLI target:

```bash
harness governance data-audit --project . --output json
```

Acceptance:

- Emits `harness.data_inventory/v1`.
- Read-only in v1; no delete, move, truncate, or compress flags.
- Secret-pattern hits and private references are blockers, not cleanup candidates.
- Protected paths are never proposed for deletion.

### Slice 7: Network And Isolation Governance

Unify network-enabled execution under explicit policy evidence.

Required network policy fields:

- policy id
- mission/task id
- allowed hosts/domains
- denied hosts/domains
- proxy or mediator endpoint if used
- request log path
- download quarantine path
- approval id
- expiration

Acceptance:

- Network-enabled execution fails closed without policy and approval evidence.
- Downloaded artifacts are quarantined until inspected and promoted.
- Browser/MCP/plugin network actions map to the same network policy concept instead of bespoke approvals.

### Slice 8: Apply-Back And Promotion

Status: complete.

Make isolated edits, patch tools, direct writes, managed actions, and future worktree promotion consume the same governance rules.

Required behavior:

- Apply-back must be bound to task id, segment id or objective id, context pack hash, allowed paths, and test evidence.
- Protected path hits require explicit exception evidence.
- Promotion refuses quarantined artifacts unless a visual/security/quality review has promoted them.
- Promotion writes durable evidence but never grants future authority.

Acceptance:

- Active repo mutation paths share the same protected path matcher.
- Apply-back evidence includes policy hash, approval id, diff summary, changed files, and gate ids.
- Tests cover allowed path success, protected path failure, stale test failure, and quarantined artifact failure.

### Slice 9: Command Catalog And Operator Docs

Status: complete.

Update docs and discoverability after the implementation is real.

Files to update:

- `docs/command_catalog.md`
- `docs/operator_guide.md`
- `docs/session_tool_catalog.md` if session-tool policy projections change
- `docs/smoke_checklist.md`

Acceptance:

- Docs describe governance as an authority layer, not a helper command.
- Command examples match actual CLI names and output schemas.
- Operator docs state that merge-check does not merge, push, comment, or call providers.

## Test Plan

Add focused tests before broad integration tests:

- `tests/test_governance_gate_registry.py`
- `tests/test_governance_protected_paths.py`
- `tests/test_governance_tasks.py`
- `tests/test_governance_context_pack.py`
- `tests/test_governance_test_plan.py`
- `tests/test_governance_merge_check.py`
- `tests/test_governance_data_inventory.py`
- `tests/test_governance_cli.py`
- `tests/test_governance_applyback_integration.py`

Fixture needs:

- tiny git repo with base and feature branches
- diff with planted secret
- diff with dangerous subprocess string
- diff touching protected path
- diff dominated by deletions
- stale branch fixture
- new module without test
- permission widening fixture
- sandbox/network widening fixture

Minimum command verification:

```bash
python -m pytest tests/test_governance_gate_registry.py tests/test_governance_protected_paths.py -q
python -m pytest tests/test_governance_tasks.py tests/test_governance_context_pack.py tests/test_governance_test_plan.py -q
python -m pytest tests/test_governance_merge_check.py tests/test_governance_cli.py -q
python -m pytest tests/test_session_tools.py tests/test_session_tool_catalog_contract.py tests/test_local_server.py -q
python -m pytest tests/test_event_broker.py tests/test_process_supervisor.py tests/test_provider_adapters.py tests/test_session_runtime.py -q
python -m harness integrity check --project . --output json
python -m harness security check --project . --output json
```

## Migration Notes

- Prefer extending the existing `SQLiteStore`, task records, session records, approval records, and artifact/run evidence before adding JSON-only side stores.
- Keep evidence under `.harness/governance/` for operator inspection, but do not rely on ignored local files as the only source of state when existing SQLite projections should own it.
- Preserve current CLI behavior while adding `harness governance ...`; avoid changing existing command output schemas except by additive fields.
- Use Harness schemas and names in new code. Mention Toloclaw only in docs and tests as provenance for the parity target.
- Treat `.harness/reference-code/toloclaw-harness-delivery` as read-only reference material.

## Delivery Order

1. Gate registry and protected path policy.
2. Governed task metadata and CLI.
3. Context pack builder.
4. Test planner and evidence writer.
5. Merge-check command.
6. Data audit command.
7. Network/isolation policy unification.
8. Apply-back/promotion integration.
9. Command catalog, operator guide, smoke checklist updates.

## Done Definition

- `harness governance gates --output json` exposes the canonical gate registry.
- A governed task can be created, inspected, given a context pack, assigned a test plan, and closed.
- `harness governance merge-check <branch> --base <base> --project . --output json` emits stable evidence and fails closed on the Toloclaw hard gates.
- All active repo write paths consume the same protected path policy.
- Data audit is read-only and propose-only.
- Network and elevated isolation require scoped policy evidence.
- Existing session tools, core service, approvals, runtime evidence, security checks, and integrity checks remain compatible.
- The command catalog and operator docs describe the governance model without overstating automation authority.
