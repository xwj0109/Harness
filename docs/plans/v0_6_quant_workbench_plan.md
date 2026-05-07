# v0.6 Quant Workbench Plan

## Summary

v0.6 starts the Quant Workbench as a declarative control-plane milestone. It expands the built-in quant agent and workbench registry so operators can inspect the intended quant research roles and workflow boundaries before any quant-specific execution pipeline exists.

This slice does not add a new execution adapter, autonomous workflow engine, broker integration, live trading, active repo writes, Docker-from-queue, hosted fallback, paid fallback, OpenAI API usage, MCP/A2A, browser/email/calendar tools, or generic shell access.

## Slice 1: Declarative Foundation

Status: complete.

The first slice expands the built-in `quant` workbench using the existing v0.2 spec primitives:

- `quant_orchestrator`.
- `quant_researcher`.
- `commodities_researcher`.
- `equities_researcher`.
- `volatility_researcher`.
- `data_engineer`.
- `backtest_engineer`.
- `low_level_optimizer`.
- `risk_reviewer`.
- `leakage_reviewer`.
- `statistical_validity_reviewer`.

All v0.6 quant agents remain built-in spec metadata. They are inspectable through `harness specs` and policy preview surfaces, but they do not execute, schedule, route, or create tasks by themselves.

The `quant` workbench must continue to forbid:

- `live_trading`.
- `broker_action`.
- `capital_allocation`.
- `order_placement`.
- `paid_api_fallback`.
- `hosted_fallback`.

## Intended Quant Workflow Templates

The initial quant workflow templates are planning targets only:

- Summarize a paper into a strategy hypothesis.
- Convert a strategy idea into a backtest specification.
- Generate a data requirement checklist.
- Draft a backtest module in isolated workspace.
- Run approved Docker tests.
- Produce a risk review.
- Produce leakage and statistical-validity reviews.
- Compare local backtest evidence.

Later slices must define separate decision-complete plans before any of these templates become executable queue workflows.

## Slice 1.5: Packaged Hierarchical Built-In Specs

Status: complete.

Move built-in registry declarations from inline Python into packaged YAML files under `src/harness/builtin_specs/`.

The folder layout mirrors the master-plan workbench tree where that is already decision-complete:

```text
src/harness/builtin_specs/
  model_profiles.yaml
  tool_policies.yaml
  memory_scopes.yaml
  workbenches/
    coding.yaml
    quant.yaml
    personal.yaml
  agents/
    coding/
    quant/
    personal/
```

`src/harness/registry.py` remains the typed loader boundary. It loads only packaged repo files from this directory, walks agent and workbench files deterministically, rejects duplicate ids, and validates the final registry through `SpecRegistry`.

The folder hierarchy is organizational in this slice. It does not yet implement inherited group defaults, semantic branch permissions, or task-specific override resolution.

## Safety Boundaries

The v0.6 declarative foundation preserves the current local/private data boundary:

- No live trading, broker integration, capital allocation, or order placement.
- No automatic task generation from objectives.
- No new daemon execution path.
- No Codex queue execution.
- No Docker execution from quant tasks.
- No shell access, hosted fallback, paid fallback, OpenAI API usage, MCP/A2A, or browser/email/calendar tooling.
- No direct reads or edits of `.harness/`, `.git/`, `.env*`, `*.pem`, `*.key`, `*.sqlite`, or `secrets/`.

## Verification

Focused checks:

```bash
pytest -q tests/test_registry_v0_2.py tests/test_specs_v0_2.py tests/test_cli_smoke.py
pytest -q
git diff --check
```

Manual inspection:

- `harness specs workbench quant --output json`.
- `harness specs agent quant_orchestrator --output json`.
- `harness specs agent statistical_validity_reviewer --output json`.
- Confirm spec output does not expose backend settings, `api_key`, `OPENAI_API_KEY`, `base_url`, environment variables, or secret-like data.
