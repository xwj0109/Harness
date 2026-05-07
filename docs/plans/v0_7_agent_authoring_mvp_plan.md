# v0.7 Agent Authoring MVP Plan

## Summary

Make user-authored agents a first-class declarative capability. v0.7 should let operators scaffold, validate, and preview custom agents without editing Python, while keeping all behavior non-executing and explicit-path only.

This milestone replaces the earlier Personal Workbench declarative target. Personal agents can be added later as content; agent authoring is the reusable platform capability needed for v1 MVP.

## Key Changes

- Add an explicit local agent bundle shape:

```text
agents/
  my_agent/
    agent.yaml
    profiles/
      default.yaml
```

- Add CLI scaffolding for declarative files only:

```bash
harness agents scaffold my_agent \
  --workbench quant \
  --kind specialist \
  --parent quant_research \
  --model-profile local_reasoning \
  --tool-policy read_only \
  --memory-scope quant \
  --output agents/my_agent
```

- Add CLI validation and preview:

```bash
harness agents validate agents/my_agent --output json
harness agents preview agents/my_agent --output json
```

- Validation should merge the explicit agent bundle with built-ins in memory only:
  - built-ins stay immutable;
  - custom ids cannot shadow built-ins;
  - custom parent, workbench, model, tool, and memory references must resolve;
  - custom profiles must reference the custom agent or an allowed referenced agent;
  - permission monotonicity must be preserved.
- Preview should return schema-versioned JSON with parsed agent spec, profiles, parent chain, effective agent view, validation errors or warnings, and source path.
- Do not persist custom agents to `.harness/` in this slice. Import/persistence can be a later decision.

## Safety Boundaries

- Agent authoring is metadata only.
- `scaffold` writes only to the operator-specified output directory and must reject forbidden destinations.
- `validate` and `preview` read only explicit paths supplied by the operator.
- No auto-discovery of arbitrary folders.
- No task/objective creation, scheduling, daemon work, runs, artifacts, leases, backend preflight, model calls, Docker, shell tools, broker actions, live trading, order placement, capital allocation, hosted fallback, paid fallback, OpenAI API usage, MCP/A2A, browser/email/calendar tools, external messaging, application submission, uploads, or active repo writes.
- Reject output paths or metadata references containing `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, or `secrets/`.
- Custom specs cannot broaden parent, group, or workbench policy boundaries.

## Test Plan

- Model/loader tests:
  - valid custom agent bundle loads from explicit path;
  - valid custom profile loads with the agent;
  - missing `agent.yaml` fails with stable error;
  - malformed YAML fails with stable error;
  - duplicate profile ids fail;
  - custom agent id shadowing a built-in id fails;
  - missing parent/model/tool/memory/workbench references fail;
  - permission broadening fails;
  - forbidden output/profile paths fail.
- CLI tests:
  - `harness agents scaffold ...` creates `agent.yaml` and optional `profiles/default.yaml` in the requested destination;
  - scaffold rejects `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, and `secrets/` destinations;
  - `harness agents validate <path> --output json` returns stable schema-versioned JSON;
  - `harness agents preview <path> --output json` returns effective agent/profile metadata;
  - validation and preview do not create `.harness/`, tasks, runs, artifacts, leases, or daemon events;
  - commands do not preflight backends and do not expose `api_key`, `OPENAI_API_KEY`, `base_url`, environment variables, backend settings, or secret-like data.
- Regression:
  - `pytest -q tests/test_registry_v0_2.py tests/test_specs_v0_2.py tests/test_spec_loader_v0_2.py tests/test_spec_effective_preview_v0_2.py tests/test_cli_smoke.py`
  - `pytest -q`
  - `git diff --check`
  - forbidden target check for `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, and `secrets/`.

## Assumptions

- v0.7 should prioritize user-authored agents over more built-in domain content.
- Personal Workbench expansion is deferred because static personal agents are content, not core MVP capability.
- Agent import/persistence into project state is deferred; v0.7 MVP is scaffold, validate, and preview.
- Custom agent execution is not authorized by this milestone.
- No package version bump is required until a separate release-hygiene checkpoint.
