# Command Catalog

This catalog groups the main operator commands by workflow. It is a navigation aid only; it does not add hidden automation or broaden the harness safety model.

Unless a command explicitly says otherwise in the operator guide, these surfaces are local control-plane or evidence commands. They do not call Codex, hosted providers, paid providers, shell tools, MCP/A2A, browser/email/calendar tools, broker APIs, or unmanaged daemon loops.

## Orientation

```bash
harness --help
harness home --project .
harness home --project . --output json
harness tui --project .
harness tui-home set-image ~/Pictures/home.png --width 80 --output json
harness quickstart agent --project .
harness quickstart agent --project . --output json
```

`home`, `tui`, and `quickstart agent` are read-only/non-mutating orientation commands. `tui` requires the optional TUI extra and starts a chat-style terminal interface with slash commands, project state, summary counts, imported agents, tasks, active leases, daemon events, recent runs, safety reminders, static generated terminal pixel art, local in-memory search over loaded dashboard and command metadata, session-local section collapse, and palette-only focus. Slash commands such as `/home`, `/quickstart`, `/tasks`, `/lease`, `/inspect-lease`, `/execute-read-only`, `/runs`, `/policy`, and `/artifacts` render workflow-grouped command templates from this catalog, mutation/safety notes, and selected command text for manual operator use. The TUI does not execute commands, spawn subprocesses, invoke a shell, copy commands to the clipboard, mutate harness state, persist UI preferences, load image files at runtime, or call providers. `tui-home set-image` is an explicit local visual-customization command that imports the provided image into tracked static TUI art files; it does not touch project runtime state, execute adapters, preflight backends, or expose image contents. `quickstart agent` prints command sequences for the MVP path but does not run them.

## Agent Authoring

```bash
harness agents scaffold my_agent \
  --workbench quant \
  --kind specialist \
  --parent quant_research \
  --model-profile local_reasoning \
  --tool-policy read_only \
  --memory-scope quant \
  --output agents/my_agent \
  --output-format json
harness agents validate agents/my_agent --output json
harness agents preview agents/my_agent --output json
```

Custom bundles are explicit-path metadata. They are not auto-discovered and are not persisted into project state until imported.

## Project Agents

```bash
harness init --project .
harness agents import agents/my_agent --project . --output json
harness agents list --project .
harness agents inspect my_agent --project .
harness agents inspect my_agent --project . --output json
harness agents preview-imported my_agent --project . --output json
harness agents remove my_agent --project . --output json
```

Imported agents remain declarative metadata. Importing an agent does not grant new tools, create tasks, create runs, or start background work.

## Built-In Specs

```bash
harness specs --output json
harness specs agent repo_inspector --output json
harness specs workbench quant --output json
harness specs preview agent commodities_researcher --output json
harness specs export --source builtin --output json
harness specs diff bundle.yaml --output json
```

Spec commands inspect or validate declarative registry state. They do not preflight backends or execute agents.

## Objectives And Tasks

```bash
harness objectives add --title "Research objective" --project . --output json
harness objectives list --project .
harness objectives inspect objective_abc123 --project . --output json
harness tasks add --title "Read-only summary" \
  --agent my_agent \
  --workbench quant \
  --execution-adapter read_only_summary \
  --task-type read_only_repo_summary \
  --project . \
  --output json
harness tasks list --project .
harness tasks inspect task_abc123 --project .
harness tasks graph --project . --output json
harness tasks status task_abc123 succeeded --project . --output json
harness tasks cancel task_abc123 --project . --output json
harness tasks retry task_abc123 --project . --output json
harness tasks run-next --project . --output json
```

Task queue commands are manual SQLite control-plane operations. `tasks run-next` leases work for inspection/adapter handoff; it does not execute agents or create runs.

## Daemon Control Plane

```bash
harness daemon run-once --project . --output json
harness daemon status --project .
harness daemon inspect-lease task_lease_abc123 --project .
harness daemon inspect-lease task_lease_abc123 --project . --output json
harness daemon recover --project . --output json
harness daemon stop --project . --output json
```

`daemon run-once` is lease-only. `daemon inspect-lease` is read-only. `daemon recover` reconciles existing linked-run evidence without creating a second run or retrying ambiguous work.

## Authorized Read-Only Adapter

```bash
harness daemon execute-read-only task_lease_abc123 --project . --output json
```

This is the only bounded real MVP adapter. It requires an existing active daemon lease and exact metadata: `execution_adapter=read_only_summary` plus `task_type=read_only_repo_summary`. It uses only the existing local-only/no-cost read-only route and read-only tools.

`daemon execute-read-only` does not authorize Codex execution, Docker-from-queue, shell access, hosted fallback, paid fallback, OpenAI API usage, MCP/A2A, browser/email/calendar tools, broker actions, live trading, order placement, active repo writes, or generic queued execution.

## Runtime Evidence

```bash
harness runs --project .
harness show run_abc123 --project . --output json
harness artifacts list run_abc123 --project .
harness artifacts inspect artifact_abc123 --project .
harness policy explain --subject-kind task --subject-id task_abc123 --project . --output json
harness tools list --project . --output json
harness tools inspect repo_read --project . --output json
harness compare run_a run_b --project . --output json
harness baseline set run_abc123 --name local --project . --output json
harness baseline compare run_def456 --baseline local --project . --output json
harness evals run --suite safety-smoke --project . --output json
harness traces export run_abc123 --format otel-json --project . --output json
```

Evidence commands report metadata, manifests, hashes, verification status, policy decisions, and trace/export envelopes. They must not print artifact file contents, secret-like data, backend settings, API keys, environment variables, or provider configuration.

## Packaging Smoke

```bash
python3 -m pip wheel --no-deps --no-build-isolation -w /tmp/harness-wheel .
python3 -m venv --system-site-packages /tmp/harness-install
/tmp/harness-install/bin/python -m pip install --no-deps /tmp/harness-wheel/agent_harness-*.whl
/tmp/harness-install/bin/harness --help
/tmp/harness-install/bin/harness specs --output json
/tmp/harness-install/bin/harness home --project /tmp/harness-project --output json
```

The wheel smoke confirms console-script wiring and packaged built-in YAML availability. It remains local-only and non-executing.
