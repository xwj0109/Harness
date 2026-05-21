# Harness Session Tool Catalog

Harness exposes model-visible session tools through the `harness.session_tools/v1` catalog. The catalog is a public contract for operators, UI clients, tests, and provider adapters. It is not a permission grant. Side-effecting tools still require exact Harness permission records before execution.

The per-tool policy object uses schema `harness.session_tool_policy_projection/v1`. UI clients should render this projection directly instead of inferring policy from tool names.

## Public Tool IDs

Navigation and read-only project context:

- `pwd`
- `cd`
- `ls`
- `read`
- `glob`
- `find`
- `grep`
- `git-diff`
- `repo-overview`
- `artifact-read`

Code intelligence:

- `lsp-diagnostics`
- `lsp-symbols`
- `lsp-definition`
- `lsp-references`

Session control:

- `todo`
- `question`
- `plan-enter`
- `plan-exit`
- `policy-explain`
- `invalid`

Local execution:

- `shell`
- `pty`
- `docker-test`

Workspace mutation:

- `patch`
- `edit`
- `write`
- `direct-write`
- `managed-action`

External context:

- `web-fetch`
- `web-search`
- `repo-clone`

Extensions:

- `mcp`
- `mcp-resource`
- `plugin-tool`
- `skill-load`

Multi-agent:

- `task`
- `task-status`

## Tool Classes

The `tool_class` field groups tools by policy boundary.

| Tool class | Tools | Default behavior |
|---|---|---|
| `read_only_project` | `pwd`, `ls`, `read`, `glob`, `find`, `grep`, `git-diff`, `repo-overview`, `artifact-read`, `lsp-diagnostics`, `lsp-symbols`, `lsp-definition`, `lsp-references`, `policy-explain` | Enabled inside the project boundary without approval. |
| `session_local` | `cd`, `todo`, `question`, `plan-enter`, `plan-exit`, `skill-load`, `task-status`, `invalid` | Mutates only session-local Harness state. `skill-load` remains permission-gated because it injects instructions. |
| `active_repo_write` | `patch`, `edit`, `write`, `direct-write`, `managed-action` | Requires exact active-repo-write permission unless a tool mode is explicitly non-mutating. |
| `execution` | `shell`, `pty`, `docker-test`, `task` | Requires exact execution permission or remains disabled/planning-only. |
| `external_network` | `web-fetch`, `web-search`, `repo-clone` | Requires explicit external-network configuration and permission. |
| `extension_boundary` | `mcp`, `mcp-resource`, `plugin-tool` | Requires configured extension metadata, visible origin/scope, and permission. |

## Policy Projection Fields

Each descriptor includes a `policy` object with these fields:

- `schema_version`: always `harness.session_tool_policy_projection/v1`.
- `tool_id`: stable public tool id.
- `enabled`: whether this tool is usable in the current projected context.
- `disabled_reason`: actionable explanation when `enabled=false`.
- `execution_supported`: whether Harness has an implementation path for the tool id.
- `planning_only`: whether the tool records plans/evidence instead of performing the side effect.
- `permission_required`: whether execution requires an exact permission record.
- `permission_key`: stable permission namespace for UI grouping and audit.
- `required_config`: project configuration needed for the tool boundary.
- `required_client_capability`: client capability needed before the tool can be treated as enabled.
- `required_model_capability`: model capability needed before the tool can be treated as enabled.
- `boundary_kind`: Harness permission boundary such as `local_only`, `active_repo_write`, `shell`, `external_network`, `mcp`, or `pty`.
- `risk`: `low`, `medium`, or `high`.
- `replay_policy`: replay/evidence policy such as `event_and_preview`, `artifact_for_large_output`, `permission_event_only`, or `rerun_forbidden`.
- `policy_source`: currently `session_tool_descriptor`.
- `maturity`: one or more maturity labels.
- `policy_reasons`: extra explanatory strings supporting the projection.

Maturity labels:

- `implemented`: the descriptor has an implementation path in the current gateway.
- `disabled_by_default`: the descriptor is public, but the tool is disabled until a policy boundary is complete.
- `planning_only`: the tool records evidence/plans rather than performing the side effect.
- `config_missing`: required project configuration is missing.
- `client_unsupported`: the current client capability is not available or not projected.
- `model_unsupported`: the current model capability is not available or not projected.

## UI Rendering Rules

Render tool availability from `policy.enabled`, not descriptor `enabled` alone. Descriptor `enabled` is the static catalog default; `policy.enabled` is the context-aware answer after config and capability checks.

When `policy.permission_required=true`, show the permission key, boundary, risk, and replay policy before the operator approves. Permission cards include `descriptor_ref` and `policy` so clients can link back to `/tools/{tool_id}` or `/sessions/{session_id}/tools/{tool_id}`.

When `policy.planning_only=true`, label the tool as evidence-producing or plan-producing. Do not present it as performing the side effect.

When `policy.disabled_reason` is present, show it as the primary explanation. It should be actionable, such as missing `web_tools.enabled`, missing `mcp.servers`, or disabled plugin registry prerequisites.

## Governance Evidence In Tool Outputs

Session tools are not separate from governance. Active repository write and external-network tools must surface the same authority evidence that the governance commands validate.

`patch` and `direct-write` are planning-only in the session-tool gateway. They validate targets through the canonical protected apply-back matcher and write plan artifacts instead of mutating files. Their plan metadata includes a `governance_applyback` object with schema `harness.governance_applyback_preflight/v1`. Clients should render it as apply-back readiness evidence, not as permission to write. It contains:

- `ready`: always `false` for deferred session-tool plans.
- `reason`: the deferred apply-back reason, such as `patch_apply_back_deferred` or `direct_write_apply_back_deferred`.
- `policy_hash`: the hash of the preflight governance policy payload.
- `approval_id`: the approval id when known.
- `changed_files`: proposed changed paths.
- `diff_summary`: file count plus added and removed line counts.
- `gate_ids`: governance gates considered by the preflight.
- `hard_gates`: individual pass/fail gate evidence.
- `operator_authority`: explicit fields showing that no permission, future authority, or active repo mutation was granted.

`edit` and `write` use the same protected path matcher before any active repo mutation path can proceed. They still require the exact active-repo-write permission path when `mode=apply`; `mode=plan` records artifacts and metadata without writing.

`web-fetch`, `web-search`, and `repo-clone` produce governance network policy/check/request/quarantine evidence when execution is allowed. Network policy evidence is scoped to the session task, approval id, target, allowlist, request log, and quarantine path. Downloaded or cloned artifacts remain quarantined until a later review/promotion flow approves them.

## API And CLI Surfaces

The shared projection is exposed through:

- CLI: `harness session tools --output json`
- Chat command: `/tools`
- Server: `GET /tools`
- Server: `GET /tools/{tool_id}`
- Server: `GET /sessions/{session_id}/tools`
- Server: `GET /sessions/{session_id}/tools/{tool_id}`

All of these surfaces should expose the same `harness.session_tools/v1` shape and the same `harness.session_tool_policy_projection/v1` policy object.

## Golden Fixtures And Migration Guardrails

The contract tests compare compact golden fixtures for:

- default project configuration
- web-enabled configuration
- MCP cached-resource configuration
- skill-enabled configuration

The fixtures intentionally snapshot:

- stable public ids
- required policy fields
- representative tools from every policy class
- default and config-dependent enablement
- disabled reasons and maturity labels

When intentionally changing a tool id, removing a policy field, changing default enablement, or changing a config-dependent disabled reason, update the golden fixture in the same change and explain the migration impact in the plan or release notes.

Do not remove fields from `harness.session_tools/v1` or `harness.session_tool_policy_projection/v1` without a compatibility plan. Additive fields are safer, but they still need tests when UI clients depend on them.
