# v0.6.3 Agent Structure MVP Plan

## Summary

Finish the reusable agent model foundation before adding workflows. This slice adds packaged agent profiles, richer read-only agent preview metadata, and stable extension points so quant and personal agents can be customized later without introducing workflow execution.

This is declarative control-plane work only. It must not create tasks, objectives, runs, leases, artifacts, schedules, daemon work, workflow instances, or execution adapters.

Status: complete.

Completion note: Agent profiles are implemented and verified as non-executing metadata. Packaged profile loading, profile validation, preview/export output, starter quant and personal profiles, and operator documentation are in place.

## Key Changes

- Add typed agent-profile metadata with stable id, owning agent id, description, knowledge domains, preferred outputs, review responsibilities, forbidden actions, tags, and metadata.
- Load packaged profile YAML from `src/harness/builtin_specs/agents/**/profiles/*.yaml`.
- Keep profiles attached to existing built-in agents; profiles do not create agents and do not change permissions.
- Enrich `harness specs preview agent <agent_id> --output json` with profile metadata when present.
- Include profiles in built-in spec export and registry JSON output with deterministic ordering.
- Keep the initial profile set small and structural, for example:
  - `commodities_researcher.default`.
  - `risk_reviewer.default`.
  - `job_researcher.default`.
- Do not add concrete quant workflow templates in this slice. User-customized workflows and automatic task creation remain deferred.

## Safety Boundaries

- Agent profiles are metadata only.
- Profiles must not broaden agent, group, or workbench safety boundaries.
- Profiles must not reference `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, or `secrets/`.
- No backend preflight, model calls, Docker, shell tools, broker actions, live trading, capital allocation, external messaging, hosted fallback, paid fallback, OpenAI API usage, MCP/A2A, browser/email/calendar tools, active repo writes, task creation, or scheduling.
- Packaged loading remains under repo-tracked `src/harness/builtin_specs/`; no runtime auto-discovery outside packaged built-ins.

## Test Plan

- Model and registry tests:
  - Agent profiles load deterministically from packaged YAML.
  - Profile ids are unique.
  - Every profile references an existing built-in agent.
  - Missing agent references fail with stable errors.
  - Forbidden path references fail validation.
  - Profile metadata does not broaden safety boundaries.
- CLI/spec tests:
  - Agent preview includes attached profile metadata for `commodities_researcher`.
  - Agents without profiles still preview successfully.
  - Registry/export JSON includes `agent_profiles`.
  - Spec commands remain read-only and do not create `.harness/`, tasks, runs, artifacts, leases, daemon events, or backend preflight.
- Regression:
  - `pytest -q tests/test_registry_v0_2.py tests/test_specs_v0_2.py tests/test_spec_loader_v0_2.py tests/test_spec_effective_preview_v0_2.py tests/test_cli_smoke.py`
  - `pytest -q`
  - `git diff --check`
  - Forbidden target check for `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, and `secrets/`.

## Assumptions

- The uncommitted workflow-template plan is superseded by this agent-structure plan.
- The MVP priority is reusable agent structure, not specific quant workflows.
- v0.7 personal agents can reuse this same profile structure.
- No package version bump is required until a later release-hygiene checkpoint.
