# v0.6 Slice 2 Agent Group Inheritance Plan

## Summary

Implement explicit agent-group semantics and permission inheritance for packaged built-in specs. The current `src/harness/builtin_specs/` folder hierarchy is organizational only; this slice should make parent/group relationships inspectable and resolvable while preserving monotonic safety boundaries.

This is still declarative control-plane work. It must not add execution adapters, scheduling, backend preflight, Docker-from-queue, shell access, broker actions, live trading, order placement, hosted fallback, paid fallback, OpenAI API usage, MCP/A2A, browser/email/calendar tools, active repo writes, or automatic task generation.

Status: implemented, pending release hygiene.

## Key Changes

- Extend the spec model only as needed to represent group inheritance:
  - Use existing `AgentKind.GROUP` for group declarations.
  - Keep `AgentSpec.parent` as the explicit hierarchy link.
  - Add optional declarative defaults only if needed for group specs, such as default `model_profile`, `tool_policy`, `memory_scope`, `tags`, or `outputs`.
- Add packaged quant group specs that mirror the master-plan branches:
  - `quant_research`.
  - `quant_development`.
  - `trading_analysis`.
  - `review`.
- Wire current quant specialist agents to explicit parent groups where the hierarchy is decision-complete.
- Add a resolver for effective agent specs:
  - Workbench constraints remain the outer boundary.
  - Parent group defaults may fill missing child values.
  - Child values may narrow but must not broaden safety constraints.
  - Resolved output must show the inheritance chain and final effective model profile, tool policy, memory scope, tags, outputs, and safety-relevant policy evidence.
- Preserve current CLI behavior and add inspection only where it improves explainability:
  - Prefer extending `harness specs preview agent <agent_id> --output json` to include parent chain and resolved defaults.
  - Do not add a new command unless preview output cannot represent the effective view cleanly.

## Safety Rules

- Permission monotonicity is mandatory:
  - `forbidden` cannot be overridden by a child.
  - `approval_required` can remain approval-required or become forbidden.
  - `allowed` can remain allowed or become stricter.
- Workbench forbidden actions remain absolute for all child groups and agents.
- Group inheritance must not broaden network, active repo write, hosted boundary, paid provider, broker action, live trading, order placement, Docker execution, shell access, MCP/A2A, or browser/email/calendar permissions.
- The packaged loader must still read only repo-packaged built-in spec files and explicit custom bundle paths. No runtime folder auto-discovery outside packaged built-ins is authorized.

## Test Plan

- Model and registry tests:
  - Group agents validate with `kind: group`.
  - Parent references resolve for all grouped quant agents.
  - Missing parent references fail with stable errors.
  - Parent cycles are rejected.
  - Effective agent resolution returns deterministic parent chains and resolved defaults.
  - Children cannot broaden parent or workbench safety boundaries.
- CLI/spec tests:
  - `harness specs agent quant_research --output json` exposes a group declaration.
  - `harness specs preview agent commodities_researcher --output json` includes the parent chain and resolved effective fields.
  - `harness specs preview workbench quant --output json` remains stable and includes grouped agents without changing schema versions unless explicitly required.
  - Spec commands remain read-only and do not create `.harness/`, preflight backends, run Docker, execute agents, or expose secrets.
- Regression:
  - `pytest -q tests/test_registry_v0_2.py tests/test_specs_v0_2.py tests/test_spec_loader_v0_2.py tests/test_cli_smoke.py tests/test_effective_policy_v0_3_5.py`
  - `pytest -q`
  - `git diff --check`
  - Forbidden target check for `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, and `secrets/`.

## Assumptions

- v0.6 Slice 2 is a spec semantics slice, not an execution slice.
- Folder hierarchy should become explainable through explicit `parent` links, not implicit path-based inheritance.
- Current v0.6 quant agents remain built-in metadata and do not execute, route work, create tasks, or schedule workflows.
- True executable quant workflows, task templates, Docker-from-queue, isolated edits, and backtest comparison workflows require later decision-complete plans.
- No package version bump is required until a separate v0.6 release-hygiene decision.
