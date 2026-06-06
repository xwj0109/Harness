# Smoke Checklist

Use this checklist after changes to the isolated Codex edit route, the Docker test runner, or the simple edit loop. Commands assume the repository root is the current directory.

## Inspect Repository State

```bash
git status --short
git log --oneline --decorate -5
```

## Run Local Unit Suite

```bash
pytest -q
```

## Verify Governance Authority Evidence

```bash
harness governance gates --output json
harness governance tasks create smoke-governance \
  --agent repo_inspector \
  --goal "Smoke governed evidence" \
  --base main \
  --project . \
  --output json
harness governance tasks show "$GOVERNANCE_TASK_ID" --project . --output json
harness governance context build --task "$GOVERNANCE_TASK_ID" --project . --output json
harness governance tests plan "$GOVERNANCE_TASK_ID" --project . --output json
harness governance tests run "$GOVERNANCE_TASK_ID" --project . --output json
harness governance data-audit --project . --output json
harness governance references-audit --project . --root ../harness-references --output json
```

Create a minimal apply-back request after replacing the placeholder ids and hash with the task/context/test evidence produced above:

```bash
cat > /tmp/harness-applyback-request.json <<'EOF'
{
  "task_id": "task_abc123",
  "segment_id": "seg_1",
  "context_pack_hash": "context-pack-sha256",
  "approval_id": "approval_abc123",
  "allowed_paths": ["src/product/**"],
  "changed_files": ["src/product/example.py"],
  "diff_summary": {
    "files": ["src/product/example.py"],
    "file_count": 1,
    "added_lines": 1,
    "removed_lines": 0
  },
  "test_evidence": {
    "task_id": "task_abc123",
    "segment_id": "seg_1",
    "context_pack_hash": "context-pack-sha256",
    "status": "pass",
    "generated_at": "2099-01-01T00:00:00Z"
  },
  "network_policy": {"mode": "disabled"}
}
EOF
harness governance applyback validate --input /tmp/harness-applyback-request.json --project . --output json
```

Expected governance properties:

- `governance gates` returns `harness.governance.gate_registry/v1` and exposes the protected apply-back pattern source.
- Governed task commands return `harness.governance_task/v1` or `harness.governance_tasks/v1` and do not start adapters or providers.
- Context/test commands write local evidence and do not grant apply-back or hosted-boundary authority.
- `governance data-audit` returns `harness.data_inventory/v1` with a propose-only cleanup plan and does not delete or repair evidence.
- `governance references-audit` returns `harness.reference_repositories_audit/v1`, includes only Git/LFS metadata plus static curated profile metadata, reports curated expected/missing/extra repository counts, required reference-pattern coverage, missing required patterns, and LFS materialized/unmaterialized file counts, and reports `manual_review_required=true`, `license_review_required=true`, `contents_included=false`, `model_context_allowed=false`, `execution_allowed=false`, `network_required=false`, and `mutation_allowed=false`.
- `governance applyback validate` returns `harness.governance_applyback_verdict/v1`, includes `policy_hash`, `approval_id`, `diff_summary`, `changed_files`, and `gate_ids`, and reports no granted permission, no future authority, and no active repo mutation.

To smoke the merge-check path on a disposable branch, run:

```bash
harness governance merge-check "$BRANCH_UNDER_REVIEW" --base main --project . --output json
```

Expected merge-check properties:

- The command returns `harness.governance.merge_check/v1` and writes local evidence under `.harness/governance/merge-check/`.
- It fails closed on protected path edits, stale branches, secret-like added text, unsafe subprocess strings, permission widening, network/sandbox widening, deletion-heavy diffs, and failing governance tests.
- It does not merge, push, comment on pull requests, call providers, acquire leases, execute adapters, start hidden work, or mutate active repo files.

## Verify Packaging and Distribution

```bash
rm -rf /tmp/harness-wheel /tmp/harness-install /tmp/harness-package-project
python3 -m pip wheel --no-deps --no-build-isolation -w /tmp/harness-wheel .
python3 -m venv --system-site-packages /tmp/harness-install
/tmp/harness-install/bin/python -m pip install --no-deps /tmp/harness-wheel/agent_harness-*.whl
/tmp/harness-install/bin/harness --help
/tmp/harness-install/bin/harness --project /tmp/harness-package-project --output json
/tmp/harness-install/bin/harness home --project /tmp/harness-package-project --output json
/tmp/harness-install/bin/harness specs --output json
/tmp/harness-install/bin/harness quickstart agent --project /tmp/harness-package-project --output json
/tmp/harness-install/bin/harness doctor --release --project /tmp/harness-package-project --output json
```

Expected packaging properties:

- The installed wheel exposes the `harness` console script.
- Package metadata reports version `1.8.0`.
- Packaged built-in YAML specs under `harness/builtin_specs/` are available after wheel install.
- `harness --output json`, `harness home`, and `harness quickstart agent` remain non-mutating in the temporary project.
- `harness doctor --release --output json` reports release-readiness metadata without backend/provider preflight, including `session_transcript_health` without transcript contents, `orchestration_readiness_release_gates` with reference metadata disabled, `orchestration_efficiency_release_gates` with reference metadata disabled, and `orchestration_synthesis_release_gates` with reference metadata disabled.
- `harness doctor --repair --output json` may clear stale session `active_run_id` pointers only when the referenced run is missing. It must report `mutation_scope=session_active_run_pointer_only`, append `session.active_run_repaired` evidence, and keep `runs_deleted=false`, `tasks_mutated=false`, `artifacts_deleted=false`, `messages_mutated=false`, `events_deleted=false`, `process_started=false`, `provider_called=false`, `network_called=false`, `filesystem_modified=false`, and `permission_granting=false`.
- Dashboard/session projections and `/sessions`, `/api/session`, `/sessions/status`, `/sessions/{id}`, and `/sessions/{id}/status` expose stale active-run pointers as `harness.session_active_run_reference/v1` with the explicit `harness doctor --repair` command. These projections must not repair the pointer, mutate runs/tasks/messages/artifacts/files, start processes, call providers, call the network, or grant permissions.
- Textual is a normal dependency for the installed app. `harness --output json` is a non-interactive probe and must not launch the terminal UI.
- The packaging smoke does not preflight backends, call providers, run Docker, create tasks, acquire leases, create runs, execute adapters, expose secrets, or use hosted/paid fallback.

## Verify v1.8 Local Agent App Readiness Path

This smoke path exercises the declarative agent lifecycle, project-local import, manual queue metadata, daemon lease inspection, and the bounded read-only adapter. Replace `task_lease_...` with the lease id returned by `daemon run-once`.

```bash
rm -rf /tmp/harness-v1-agent
harness agents scaffold smoke_v1_agent \
  --workbench quant \
  --kind specialist \
  --parent quant_research \
  --model-profile local_reasoning \
  --tool-policy read_only \
  --memory-scope quant \
  --output /tmp/harness-v1-agent \
  --output-format json
harness agents validate /tmp/harness-v1-agent --output json
harness agents preview /tmp/harness-v1-agent --output json
harness init --project .
harness agents import /tmp/harness-v1-agent --project . --output json
harness agents inspect smoke_v1_agent --project . --output json
harness agents preview-imported smoke_v1_agent --project . --output json
harness tasks add --title "v1 read-only summary" \
  --agent smoke_v1_agent \
  --workbench quant \
  --execution-adapter read_only_summary \
  --task-type read_only_repo_summary \
  --project . \
  --output json
harness daemon run-once --project . --output json
harness daemon inspect-lease "$LEASE_ID" --project . --output json
harness daemon execute-read-only "$LEASE_ID" --project . --output json
```

Expected v1.8 safety properties:

- Agent and task lifecycle commands are declarative/control-plane operations only.
- `daemon run-once` leases work but does not execute it.
- `daemon run-once` pauses registered-adapter tasks with missing descriptor-required approvals before creating an attempt, lease, run, backend preflight, or adapter dispatch.
- `daemon execute-read-only` uses only the configured Codex CLI subscription route in read-only sandbox mode and requires hosted-boundary approval for `read_only_repo_summary`.
- The MVP read-only path does not authorize Codex execution from the queue, Docker-from-queue, generic shell, hosted fallback, paid fallback, OpenAI API usage, MCP/A2A, browser/email/calendar tools, broker actions, live trading, order placement, active repo writes, external messaging, application submission, or autonomous workflows.
- The repo planning adapter is available only through exact `repo_planning/repo_planning` metadata, an active lease, a valid hosted-boundary Codex approval, and Codex read-only sandbox execution through the registered dispatcher.
- TUI command-palette and right-panel guidance surfaces are copy-only/passive and do not execute commands or call providers.
- Capability catalog, memory, and progress surfaces are local operator context only and do not grant new execution authority.

## Verify v1.8 Capability, Memory, And Progress Surfaces

```bash
harness capabilities list --project . --output json
harness capabilities inspect dry_run --project . --output json
harness memory save-note --scope project --summary "v1.8 smoke note" --project . --output json
harness memory list --project . --output json
harness memory inspect "$MEMORY_ID" --project . --output json
harness memory forget "$MEMORY_ID" --project . --output json
harness objectives add --title "v1.8 progress smoke" --project . --output json
harness tasks add --title "v1.8 progress dry run" \
  --objective "$OBJECTIVE_ID" \
  --execution-adapter dry_run \
  --task-type phase_1a_test \
  --project . \
  --output json
harness objectives checkpoints create "$OBJECTIVE_ID" --label "v1.8 supervisor checkpoint" --reason "smoke gate before run" --project . --output json
harness objectives checkpoints gate "$OBJECTIVE_ID" --project . --output json
harness objectives checkpoints verify "$OBJECTIVE_ID" --project . --output json
harness progress --objective "$OBJECTIVE_ID" --project . --output json
harness objectives checkpoints approve "$OBJECTIVE_ID" "$CHECKPOINT_ID" --approval-id smoke_supervisor_approval --project . --output json
harness objectives checkpoints verify "$OBJECTIVE_ID" --project . --output json
harness objectives run "$OBJECTIVE_ID" --project . --autonomy safe-local --max-parallel 2 --output json
harness objectives verify-evidence "$OBJECTIVE_ID" --project . --output json
harness traces export-objective "$OBJECTIVE_ID" --format otel-json --project . --output json
```

Expected v1.8 local app properties:

- `capabilities list` returns `harness.capability_catalog/v1`, includes registered adapters such as `dry_run`, `read_only_summary`, `repo_planning`, `codex_isolated_edit`, and the record-only `session_child_task`, and does not preflight backends or execute adapters.
- `memory save-note/list/inspect/forget` returns `harness.memory_record/v1` or `harness.memory_records/v1`, stores only explicit local notes, redacts secret-looking content before persistence, and does not alter policy, approvals, or adapter eligibility.
- `objectives checkpoints create/gate/list/verify/approve/reject` returns objective checkpoint schemas, stores append-only supervisor gate evidence, verifies checkpoint event parsing/envelope/id/hash/timestamp/lifecycle integrity, and does not create leases, runs, artifacts, adapter dispatch, model context, network calls, active repo writes, or broad permission grants.
- Required pending or rejected checkpoints make `objectives run` and `daemon run-autonomous` stop with `checkpoint_blocked` before lease acquisition; approving the checkpoint with an explicit `approval_id` unblocks later dispatch.
- Approval-required or denied candidates make `objectives run` and `daemon run-autonomous` stop before creating attempts, leases, runs, backend preflight, or adapter dispatch. If pre-lease autonomy passes but guarded lease selection later sees stale approval, runtime-control, adapter-breaker, dependency, or active-lease state, it must stop with `lease_guard_stopped` evidence instead of creating execution records. The stopped objective evidence must include a persisted autonomy decision and `lease_id=null`.
- Corrupt checkpoint evidence makes `objectives checkpoints verify` fail, makes the checkpoint gate block, makes readiness fail, and prevents checkpoint create/approve/reject from appending to the untrusted chain.
- `progress --objective` returns `harness.orchestration_progress/v1`, reports ready/leased/blocked/terminal state, includes checkpoint gate status and an `objective_evidence` verification summary when objective JSONL exists, and does not create leases, runs, artifacts, adapter dispatch, or evidence repair.
- `objectives reconcile-evidence <objective_id> --dry-run --output json` previews explicit reconciliation for objectives that have persisted run records but no objective JSONL chain. Running without `--dry-run` writes only `.harness/autonomy/objectives/<objective_id>.jsonl` with reconciliation events and then verifies the chain. It must not mutate existing objectives, tasks, runs, sessions, artifacts, repository files, approvals, provider state, network state, or permissions, and it must not represent historical runs as newly dispatched autonomous work.
- `session tools` returns the full `harness.session_tools/v1` catalog with `policy.exposure`; default model-visible native tool schemas expose only low-risk read-only or session-local tools with strict top-level object schemas, and withhold approval-gated shell, write, network, extension, task-spawning, and internal invalid-call recovery tools.
- Session-tool delegated tasks persist `harness.agent_handoff_envelope/v1` metadata with envelope id, payload SHA-256, idempotency key, delegate budget, parent/child session ids, W3C-style trace context, and embedded `harness.agent_contract/v1` id/hash. `harness agents contract <agent_id> --project . --output json`, `harness agents discover --workbench coding --output json`, `harness agents allocate --workbench coding --task-type security_review --required-kind reviewer --required-tag security --required-tool-policy read_only --max-candidates 1 --output json`, `harness handoffs inspect-task <task_id> --project . --output json`, and the `task-status` session tool must reconstruct the contract/discovery/allocation/envelope passively, keep adapter/process/network/tool/agent execution disabled, keep `permission_granting=false`, keep artifact/reference/credential/source bodies excluded, and report validation errors for malformed delegation metadata or unresolved agent identity.
- `session_read_tools` tasks without explicit `allowed_tools` advertise only `read`, `glob`, `grep`, and `artifact-read` native schemas after central exposure filtering. Explicit `allowed_tools` metadata is required before broader tools such as `shell` can be advertised, must be a non-empty list of known enabled and project-policy-enabled session tool ids, and must reject malformed, unknown, disabled, config-blocked, capability-blocked, or internal-only ids before run creation. `invalid` remains internal-only.
- Live session runtime provider-tool events must fail closed to the same exposure policy: unrequested permission-gated tools such as `shell` produce a model-visible `provider_tool_not_available` tool error without creating permission records, while explicitly requested tools such as `shell` still require exact approval and config-blocked tools such as unconfigured `web-fetch` are rejected before provider stream or gateway execution.
- `objectives verify-evidence` returns `harness.objective_evidence_verification/v1` and verifies the common event envelope, event-type payload schemas including checkpoint-blocked stops, lease-guard stops, and linked execution-error outcomes, event identity, event index order, event hash-chain integrity, event timestamp order, batch lifecycle, batch-local and cumulative dispatch counts, execution-error counts, dispatch links, persisted dispatch run status and released-lease decision metadata, stopped summaries, autonomy decision/approval/outcome records against persisted state, decision/approval/outcome authority payload consistency for dispatch and execution-error records, and persisted-decision consistency for non-dispatch `autonomy_stopped` and `lease_guard_stopped` records.
- `traces export-objective` returns `harness.trace_export/v1`, reports `ok` from objective evidence verification, and includes the objective evidence event count, hash-chain status, and head SHA-256 without reading artifact bodies.
- `traces export <run_id>` returns `harness.trace_export/v1`; registered-adapter run traces include `harness.delegate_budget` with the selected adapter's `harness.delegate_budget/v1` schema version, zero validation gaps, and bounded network/filesystem/tool/runtime limits. Runs linked to a persisted lease attempt also include `harness.queue` wait timing and `harness.lease` lifecycle timing.
- Bearer-auth local server `GET /runs/{run_id}/trace`, `GET /objectives/{objective_id}/evidence`, and `GET /objectives/{objective_id}/trace` mirror those schemas for attached clients and report no execution, no adapter/provider start, no filesystem mutation, no network call, no artifact contents, and no permission grant.
- Run and objective trace event spans include sanitized payload SHA-256, byte size, and key-list metadata; secret-like payload values and keys such as `api_key` are redacted before CLI or local-server projection.
- `harness --project . --output json` continues to return `harness.chat/v1` with `registered_adapters`, `capabilities`, runtime controls, memory summary, and progress summary when initialized.
- Chat aliases `/capabilities`, `/memory`, `/remember`, `/forget`, `/progress`, “show capabilities”, “show memory”, and “where are we” remain deterministic local renderers.
- The TUI right panel displays capability and progress rows passively; app startup and dashboard refresh do not call Codex, local model endpoints, Docker, shell, network, providers, or adapter execution.

## Verify Operator Cockpit

Replace `TASK_ID`, `LEASE_ID`, and `ARTIFACT_ID` with ids produced by the v1.8 registered adapter smoke path when checking inspect text output.

```bash
harness --project . --output json
harness home --project . --output json
harness quickstart agent --project . --output json
harness --project . --plain
harness --project . --plain --codex-like
harness home --project .
harness quickstart agent --project .
harness runs --project .
harness tasks list --project .
harness agents list --project .
harness daemon status --project .
harness agents inspect smoke_v1_agent --project .
harness tasks inspect "$TASK_ID" --project .
harness daemon inspect-lease "$LEASE_ID" --project .
harness policy explain --subject-kind task --subject-id "$TASK_ID" --project .
harness artifacts inspect "$ARTIFACT_ID" --project .
```

Expected operator cockpit safety properties:

- Bare `harness` is the single primary app surface. It combines passive dashboard context with the chat/orchestrator prompt in one Textual terminal app. `harness --plain` is a line-oriented fallback for tests/dev use, not a separate product surface.
- `harness home` is read-only and does not initialize projects, import agents, create tasks, create runs, create artifacts, acquire leases, mutate daemon state, or execute adapters.
- `harness --output json` is read-only and returns `harness.chat/v1` context without launching a prompt, preflighting backends, calling providers, touching Docker, or initializing `.harness/`.
- The unified app and `--plain` fallback keep session state in memory only. `/help`, `/init`, `/mode`, `/home`, `/dashboard`, `/orchestrators`, `/use`, `/agents`, `/tasks`, `/capabilities`, `/memory`, `/progress`, `/adapters`, and `/quit` should work without traceback on an uninitialized project. `/init` is the explicit in-app setup path; `harness --output json`, dashboard refresh, and passive slash commands must not initialize. Task creation, orchestrated graph creation, lease acquisition, and registered-adapter dispatch require explicit confirmation and use the normal objective, task, daemon run-once, and daemon execute paths.
- In normal mode, chat drafts before confirmation. In `--codex-like` or `/mode codex-like`, one explicit confirmation may create the approved task/objective graph and run it in the foreground through registered adapters. Missing hosted-boundary approval should be offered as an explicit in-app approval step; apply-back remains separate and denied by default.
- Chat-first orchestration should draft the full objective/task graph before creation, including supervisor checkpoints for reviewed workflow templates. Reviewed workflow task draft payloads must include `agent_selection.schema_version=harness.workflow_agent_selection/v1`; draft and persisted task metadata must include `agent_selection_source=delegate_allocation` plus a compact `harness.delegate_allocation/v1` `delegate_allocation` receipt whose selected agent matches the task `agent_id`, whose requirements have `source=workflow_template`, whose requirement schema is `harness.workflow_agent_selection/v1`, whose requirement fields match the draft task `agent_selection`, whose bid terms keep `runtime_authority_granted=false` and `permission_granting=false`, and whose safety flags show no provider, network, tool, adapter, agent, process, filesystem, budget, or permission authority. Pending task drafts, orchestration drafts, adapter-dispatch confirmations, hosted-approval prompts, and action contracts should be persisted as inert active-session metadata and recoverable after a process restart when the same session id is resumed. Dashboard/session projections, right-pane attention rows, `/sessions`, `/api/session`, `/sessions/status`, `/sessions/{id}`, and `/sessions/{id}/status` should show a compact recoverable pending-action summary with `/confirm` and `/decline` next commands, and should show stale active-run pointers as passive `harness.session_active_run_reference/v1` health with the explicit `doctor --repair` command. Malformed or stale pending-action metadata should surface as invalid/stale audit state through the same projections, `harness sessions pending-action <session_id> --output json`, and `GET /sessions/{session_id}/pending-action`; `harness sessions clear-pending-action <session_id>` and `DELETE /sessions/{session_id}/pending-action` should clear only that proposal metadata. Recovery must still require `/confirm` or `/decline`; passive visibility and cleanup must not execute, lease, dispatch, call providers, mutate objectives/tasks/leases/runs/approvals/artifacts/messages/files, repair stale active-run pointers, or grant authority by themselves. Confirmation should create append-only approved checkpoint evidence for the objective graph before lease acquisition; retrying the same pending draft or normalized action contract should reuse the existing objective, task ids, and checkpoint evidence instead of duplicating records. That checkpoint must not grant hosted-provider authority, shell access, Docker access, network access, active-repo mutation, or apply-back. The foreground `/run` path may drive only the approved graph through `daemon run-once` and `daemon execute`; it must stop on blocked dependencies, rejection, missing hosted approval, operator `/stop`, or terminal graph completion.
- The dashboard side renders a light-theme chat-style interface, project state, summary counts, imported agents, task details, active lease details, daemon event summaries, recent runs, safety reminders, local in-memory search over loaded dashboard/command metadata, in-memory section collapse, palette-only search focus, and a copy-only command palette without initializing projects, importing agents, creating tasks, creating runs, creating artifacts, acquiring leases, mutating daemon state, executing adapters, crawling files, or searching artifact contents.
- The slash-command and command-palette surfaces show grouped command templates, mutation/safety notes, and selected command text for manual use only. They must not execute commands, spawn subprocesses, invoke a shell, copy to the clipboard, run daemon actions, execute adapters, preflight backends, run Docker, call providers, or expose artifact file contents.
- The TUI layout keeps chat and dashboard context in stable read-only sections, shows keyboard/navigation hints for `/`, `escape`, `tab`, `shift+tab`, `ctrl+p`/`F2`, prompt-unfocused `c`, prompt-unfocused `shift+c`, `enter`, `shift+enter`, and `ctrl+q`, reports no-match states, and displays only static generated terminal pixel art without persisting preferences, loading image files at runtime, mutating harness state, or adding command actions.
- Section collapse and palette-only focus are session-local TUI state only. They must not write project config, user config, `.harness/`, SQLite, static art files, or any preference file.
- `harness tui-home set-image <image> --output json` explicitly imports a local image into tracked static TUI art files and returns `harness.tui_home_image/v1`; it does not mutate `.harness/`, create tasks, create runs, acquire leases, execute adapters, preflight backends, run Docker, invoke shell tools, call providers, or expose image contents.
- `harness quickstart agent` prints commands only; it does not create files, initialize projects, import agents, create tasks, acquire leases, create runs, create artifacts, execute adapters, or start daemon work.
- The dashboard does not preflight Codex or local backends, run Docker, invoke shell tools, call providers, start schedulers, or inspect backend settings.
- JSON output uses `harness.home/v1` and `harness.quickstart_agent/v1` and does not include `api_key`, `OPENAI_API_KEY`, `base_url`, environment variables, artifact contents, or secret-like metadata.
- `harness home` and `harness quickstart agent` text output uses simple sections for scanning while keeping JSON unchanged.
- Text list/status commands use compact tab-separated headers; JSON schemas remain unchanged.
- Text inspect/explain commands use compact sections for scanning; JSON schemas remain unchanged.

## Verify Read-Only v0.2 Specs Commands

Built-in inspection:

```bash
harness specs --output json
harness specs agent repo_inspector --output json
harness specs workbench coding --output json
harness specs workbench quant --output json
harness specs agent quant_orchestrator --output json
harness specs agent statistical_validity_reviewer --output json
harness specs preview agent commodities_researcher --output json
```

Create a temporary valid custom bundle outside `.harness/`:

```bash
cat > /tmp/harness-v0-2-specs.yaml <<'EOF'
schema_version: harness.spec_bundle/v1
model_profiles:
  local_reasoning:
    id: local_reasoning
    kind: local
    backend: local_openai_compatible
tool_policies:
  read_only:
    tools:
      repo_read: allowed
    network: forbidden
    active_repo_write: forbidden
    hosted_boundary: approval_required
memory_scopes:
  project:
    id: project
agents:
  repo_inspector:
    id: repo_inspector
    kind: specialist
    role: Inspect repository evidence.
    model_profile: local_reasoning
    tool_policy: read_only
    memory_scope: project
workbenches:
  coding:
    id: coding
    description: Coding workbench.
    allowed_agents:
      - repo_inspector
    default_model_profile: local_reasoning
    forbidden_actions:
      - paid_api_fallback
      - hosted_fallback
EOF
```

Validate, export, diff, and preview the explicit bundle:

```bash
harness specs validate /tmp/harness-v0-2-specs.yaml --output json
harness specs export --source /tmp/harness-v0-2-specs.yaml --output json
harness specs diff --source /tmp/harness-v0-2-specs.yaml --output json
harness specs preview agent repo_inspector --source /tmp/harness-v0-2-specs.yaml --output json
harness specs preview workbench coding --source /tmp/harness-v0-2-specs.yaml --output json
```

Expected safety properties:

- Specs commands do not execute agents or preflight backends.
- Specs commands do not read or write `.harness/`.
- Custom bundles are explicit-path only and are not persisted.
- JSON output uses stable `schema_version` wrappers.

v0.6 Quant Workbench expectations:

- `harness specs workbench quant --output json` lists the built-in quant agent set.
- Quant specs are declarations only; they do not create tasks, schedule workflows, execute agents, run Docker, call backends, connect to brokers, place orders, or trade.
- The `quant` workbench forbids live trading, broker actions, capital allocation, order placement, hosted fallback, and paid fallback.
- Agent profiles are declarations only; they expose customization metadata such as knowledge domains, preferred outputs, review responsibilities, and forbidden actions.
- Built-in specs and profiles are packaged YAML loaded through the typed registry; there is no runtime folder auto-discovery outside the repo-packaged built-ins.

Verify v0.7 explicit agent authoring:

```bash
rm -rf /tmp/harness-agent-authoring-smoke
harness agents scaffold smoke_agent \
  --workbench quant \
  --kind specialist \
  --parent quant_research \
  --model-profile local_reasoning \
  --tool-policy read_only \
  --memory-scope quant \
  --output /tmp/harness-agent-authoring-smoke \
  --output-format json
harness agents validate /tmp/harness-agent-authoring-smoke --output json
harness agents preview /tmp/harness-agent-authoring-smoke --output json
```

Expected v0.7 safety properties:

- Agent authoring commands read or write only the explicit operator path.
- Agent authoring commands reject symlinked paths, unsupported profile files, and hard-forbidden path targets.
- Built-in specs remain immutable and custom agent ids cannot shadow built-ins.
- Custom bundles are not auto-discovered and are not persisted into `.harness/`, SQLite, tasks, objectives, runs, leases, artifacts, daemon events, or runtime registry state.
- Authoring commands do not execute agents, preflight backends, run Docker, invoke shell tools, schedule work, call providers, connect to brokers, place orders, send messages, submit applications, or mutate active repo files.
- Output is schema-versioned and does not include backend settings, `api_key`, `OPENAI_API_KEY`, `base_url`, environment variables, or secret-like data.

Verify v0.8 project-local agent import after initializing a project:

```bash
harness init --project .
harness agents import /tmp/harness-agent-authoring-smoke --project . --output json
harness agents list --project . --output json
harness agents inspect smoke_agent --project . --output json
harness agents preview-imported smoke_agent --project . --output json
harness tasks add --title "Use smoke agent" --agent smoke_agent --workbench quant --project . --output json
```

Expected v0.8 safety properties:

- Import persists validated agent/profile metadata, source path, and content hash in initialized harness persistence.
- Import does not modify packaged built-ins and rejects built-in id shadowing or duplicate project-local ids.
- Imported task references record `spec_source_kind: project` but remain non-executing metadata.
- Import/list/inspect do not execute agents, call backends, preflight providers, run Docker, invoke shell tools, create runs, create artifacts, start daemon work, or authorize new tools.
- `agents preview-imported` reports source drift without rewriting the import record.
- `agents remove` is available only for unused imported agents; it rejects built-ins and task-referenced imports.

## Verify Manual v0.3 Task Queue

The task queue requires initialized project state and writes only to `.harness/harness.sqlite`.

```bash
harness init --project .
harness objectives add --title "Queue hardening" --workbench coding --project . --output json
harness objectives list --project . --output json
harness tasks add --title "Inspect repository" --agent repo_inspector --workbench coding --project . --output json
harness tasks list --project . --output json
harness tasks graph --project . --output json
harness tasks run-next --project . --output json
harness tasks cancel task_abc123def456 --project . --output json
```

Expected safety properties:

- `run-next` evaluates descriptor approval, daemon-forbidden policy metadata, dependencies, and active leases before creating a local attempt and lease. Eligible work returns `decision=leased_task`; approval-required, dependency-blocked, policy-forbidden, or active-leased candidates appear in `pause_reasons` and must not receive a new attempt or lease.
- `run-next` does not create a run record or run artifact directory.
- `tasks graph` is read-only and reports local objectives, tasks, dependencies, and blocked reasons.
- Objective and task commands do not execute agents, preflight backends, run Docker, start daemons, or schedule work.

## Verify v0.3.5 Control-Plane Evidence

Create a local run if none exists:

```bash
harness dev create-run --goal "v0.3.5 evidence smoke" --task-type phase_1a_test --project .
RUN_ID=$(harness runs --project . --output json | python -c 'import json,sys; print(json.load(sys.stdin)["runs"][0]["id"])')
```

Inspect runtime policy, artifacts, and tool descriptors:

```bash
harness policy explain --subject-kind run --subject-id "$RUN_ID" --project . --output json
harness artifacts list "$RUN_ID" --project . --output json
harness tools list --project . --output json
harness tools inspect repo_read --project . --output json
```

Compare and baseline local run evidence:

```bash
harness compare "$RUN_ID" "$RUN_ID" --project . --output json
harness baseline set "$RUN_ID" --name smoke-local --project . --output json
harness baseline compare "$RUN_ID" --baseline smoke-local --project . --output json
```

Run local safety-smoke evals and export trace evidence:

```bash
harness evals run --suite safety-smoke --project . --output json
harness traces export "$RUN_ID" --format otel-json --project . --output json
```

Inspect the v0.4 daemon control plane without executing work:

```bash
harness daemon status --project . --output json
harness daemon run-once --project . --output json
harness daemon recover --project . --output json
harness daemon stop --project . --output json
```

Inspect the explicit v0.4.5 dry-run adapter without invoking providers or tools:

```bash
harness tasks add --title "Dry-run contract" --execution-adapter dry_run --task-type phase_1a_test --project . --output json
harness daemon run-once --project . --output json
harness daemon inspect-lease "$LEASE_ID" --project . --output json
harness daemon execute-dry-run "$LEASE_ID" --project . --output json
```

Inspect the explicit v0.5 read-only adapter:

```bash
harness tasks add --title "Read-only summary" --execution-adapter read_only_summary --task-type read_only_repo_summary --project . --output json
harness daemon run-once --project . --output json
harness daemon inspect-lease "$LEASE_ID" --project . --output json
harness daemon execute-read-only "$LEASE_ID" --project . --output json
```

Inspect registered adapter dispatch:

```bash
harness daemon adapters --project . --output json
harness tasks add --title "Dry-run via dispatcher" --execution-adapter dry_run --task-type phase_1a_test --project . --output json
harness daemon run-once --project . --output json
harness daemon inspect-lease "$LEASE_ID" --project . --output json
harness daemon execute "$LEASE_ID" --project . --output json
```

Expected safety properties for the v0.3.5 evidence commands and v0.4 daemon control-plane commands after `RUN_ID` setup:

- These commands are local evidence inspection or baseline commands.
- They do not execute tools, preflight backends, run Docker, create extra runs or artifacts, start schedulers, or schedule background work.
- `daemon run-once` may lease one eligible task or renew an active daemon-owned lease and write daemon heartbeat/event evidence, but it must not execute the task or create a run.
- `daemon run-once` must renew only coherent active leases. Inconsistent active leases must be released with `release_inconsistent_lease` evidence instead of being renewed indefinitely, and they must not block later eligible work.
- `daemon run-once` must pause approval-required or daemon-policy-forbidden tasks and report `pause_reasons` instead of failing or executing them.
- `daemon status` must expose paused task reasons so operators can debug approval, dependency, active-lease, or daemon-policy gates without reading SQLite manually.
- `daemon recover` may expire stale active leases and return tasks to `ready`, `blocked`, or `waiting_approval`, but it must not retry terminal tasks automatically.
- `daemon recover` must reconcile completed or failed linked-run evidence, fail expired leases with missing or non-terminal linked runs for operator inspection, and expire inconsistent active leases with `recover_inconsistent_lease` evidence instead of leaving stale active locks behind.
- `daemon stop` and stale-daemon status handling must use the same linked-run discipline: reconcile completed or failed linked runs, fail missing or non-terminal linked runs for inspection, and requeue only unexecuted leases.
- `tasks retry` must enforce registered adapter replay policy: safe/idempotent adapters may requeue, `requires_fresh_approval` adapters must stay failed until a valid scoped approval exists, and `not_replayable` adapters must reject retry. Accepted retries must persist `harness.task_replay_receipt/v1` transition evidence with the task idempotency key, replay policy, retry gate, prior attempt count, approval revalidation state, and active-lease duplicate guard. Every newly leased attempt must persist matching `harness.task_replay_receipt/v1` metadata with the attempt idempotency key and prior attempt count. `tasks inspect --output json` must expose a sanitized `harness.task_replay_receipts_projection/v1` summary for both receipt locations, with malformed receipts surfaced as gaps and old attempts without receipts counted as legacy-missing rather than release-blocking. Orchestration-efficiency must report replay receipt enforcement with zero gaps for newly generated attempts.
- v0.4 scheduler commands do not execute tasks, bind task attempts to runs, call backends, run Docker, create run artifacts, add hosted fallback, add paid fallback, or start unmanaged background work.
- Bounded parallel objective runs must write typed `harness.objective_batch_plan/v1` `batch_planned` objective JSONL evidence before each dispatch batch with capacity, selected task/lease pairs, resumed-vs-new selection source, dependency snapshots, schedule profiles, scheduler-policy sort-key evidence, and autonomy decision ids, then `batch_completed` evidence with batch-local dispatch count, cumulative dispatch count, and execution-error count. Selected batch-plan decision ids must resolve to persisted decision records that match run scope, task, lease, dispatch tool, adapter, task type, and decision status; scheduler profiles must recompute from durable task state; candidate task ids must remain policy-ordered by priority, critical-path depth, downstream count, creation time, and task id; fresh selections must be the policy prefix after resumed active leases; resumed leases must be ordered by acquisition time and lease id; and each selected task/lease pair must have exactly one terminal `adapter_dispatched` or `execution_error` event in that batch. Worker-level `execution_error` events must include task, lease, adapter, policy, autonomy decision, autonomous approval, and autonomous outcome ids; the referenced outcome record must be `ok=false`, and the referenced approval/outcome authority fields must remain consistent with the decision record.
- Autonomous objective runners must not execute active leases owned by another runner; they must pause with an `active_lease` reason and preserve the existing lease.
- `daemon execute-dry-run` is explicit v0.4.5 contract evidence only: it may bind one active lease to one local `phase_1a_test` run and metadata-only artifacts, but it must not call backends, run Docker, execute shell commands, access the network, mutate active repo files, or use hosted/paid fallback.
- `daemon execute-read-only` is explicit read-only adapter execution only: it may bind one active lease to one `read_only_repo_summary` run through the configured `codex_cli` subscription backend in read-only sandbox mode after hosted-boundary approval. If rejection happens before run creation, it must create no run, release the lease, and mark the attempt/task `failed` or `waiting_approval` using the same no-run finalization policy as generic registered dispatch.
- `daemon execute` is registered-adapter dispatch only: no adapter means no execution, unknown adapter fails closed, and adapter descriptors are documentation and validation metadata rather than permission grants. Descriptor-level `harness.delegate_budget/v1` drift, including a budget that is invalid or more permissive than the selected sandbox profile, must fail closed during shared registered-task validation before task creation and again before adapter execution with `reason_code=delegate_budget_mismatch` and no run manifest. Known delegate-budget task metadata must reject non-numeric values, negative runtime/model/tool/token/cost ceilings, branch fan-out below one, and requested ceilings above the descriptor budget before creating runs. No-run registered-adapter rejections must release the active lease and mark the linked attempt/task `failed` or `waiting_approval`; `duplicate_run` and `lease_owner_mismatch` must not overwrite existing run or owner state.
- `daemon inspect-lease` is read-only and may report linked task, attempt, run, manifest, dry-run eligibility, read-only eligibility, generic execution eligibility, typed `security_decision`, and recovery recommendation without creating runs or artifacts.
- `daemon inspect-lease --output json` and generic `daemon execute --output json` include sanitized `security_decision` evidence before registered-adapter execution is allowed, denied, or paused for approval. If the selected adapter sandbox profile is missing, unknown, or schema-incompatible, execution must fail closed with `reason_code=sandbox_profile_mismatch` before dispatch.
- `harness controls list`, `harness controls disable`, `harness controls enable`, `harness controls breaker-status`, and `harness controls breaker-reset` expose local runtime kill switches and adapter breakers without calling providers, touching Docker, or creating runs/tasks/artifacts.
- Disabled controls and open breakers appear as `security_decision.reason_code` values such as `control_disabled` or `breaker_open`, and capability/chat surfaces mark affected adapters unavailable rather than granting new authority. `adapter`, `task_type`, `backend`, and `hosted_boundary` controls must use the same registered-adapter descriptor matcher for direct daemon execution, capability projection, and autonomous objective scheduling.
- Run manifests and trace export include sanitized `context_provenance` and `untrusted_context_warnings` for prompts, task metadata, artifacts, generated text, and memory records without embedding artifact contents.
- Memory records are redacted when needed and marked non-authoritative for permissions, policy, approvals, hosted-boundary execution, Docker/network access, shell/tool grants, and active repo apply-back.
- `harness evals run --suite security --output json` and `harness security check --output json` expose sanitized metadata-only local detections without creating records, calling providers, touching Docker, or reading artifact bodies.
- `harness evals run --suite integrity --output json` and `harness integrity check --output json` expose local package/evidence integrity for built-in specs, adapter descriptors, workflow templates, security docs, static TUI assets, and artifact provenance without initializing projects, creating runtime records, calling providers, touching Docker, or reading forbidden paths.
- `harness evals run --suite security-layer --output json` and `harness security audit --output json` pass for the local-first completion scope and remain read-only, including run trace provenance, run-event payload metadata coverage, registered-adapter delegate-budget trace evidence, linked lease/queue trace evidence for dispatched runs, objective trace provenance, and objective-event payload metadata coverage when autonomous objective evidence exists. Registered-adapter run manifests must derive `sandbox_profile` and `delegate_budget` from the selected adapter descriptor, expose `harness.sandbox_profile/v1` and `harness.delegate_budget/v1` evidence, and report no delegate-budget validation gaps even when the task type is not in a legacy task-type mapping. On uninitialized projects they report package/static checks plus skipped runtime checks without creating `.harness/`.
- `harness evals run --suite orchestration-readiness --output json` and `harness orchestration audit --output json` return `harness.orchestration_readiness_audit/v1`, map pulled reference-system patterns to current Harness capabilities, and remain read-only. They must include pending chat action recovery, typed task delegation evidence for both `harness.agent_handoff_envelope/v1` and `harness.agent_contract/v1`, `agent_discovery_and_allocation`, schema compatibility contracts, `workflow_coordination_contracts`, `orchestration_scenario_conformance`, replay drift detection, and `agentic_security_controls`. Schema compatibility must include `agent_discovery_catalog`, `task_replay_receipt`, `workflow_template`, `workflow_agent_selection`, `workflow_coordination_catalog`, `orchestration_scenario_catalog`, `objective_batch_plan`, `sandbox_profile_catalog`, and `sandbox_profile` and report no missing critical schema ids, no duplicate ids, no unversioned ids, no unsafe authority ids, no incomplete contracts, no policy/version mismatches, and no safety issues. Agent discovery/allocation must report `harness.agent_discovery_catalog/v1` and `harness.delegate_allocation/v1`, deterministically select `security_reviewer` for read-only security review, and report no catalog, allocation, card, bid, or authority safety issues. Workflow coordination must report `harness.workflow_coordination_catalog/v1`, no missing required pattern ids, no missing required state classes, no failed patterns, no safety issues, state classes for session/workflow/memory/artifact, and pattern rows for bounded fan-out/fan-in and typed handoff. Scenario conformance must report `harness.orchestration_scenario_catalog/v1`, no missing required case ids, no missing required layers, no failed cases, no safety issues, required layers for unit/contract/replay/scenario/security/benchmark, and rows for slow branch barriers, hosted-memory denial, remote protocol fail-closed boundaries, retry/idempotency policy, and live benchmark explicit permits. Agentic security controls must report three passing risk rows for `memory_poisoning`, `insecure_inter_agent_communication`, and `cascading_failures`, keep memory context marked `memory_not_authority`, deny hosted/remote-vector/secret context transmission by default, keep handoff authority read-only with trace and payload hashes, report no risky remote-agent protocols, and report no auto-allowed adapters with unsafe replay policy. They must treat malformed/stale pending-action metadata as a warning with exact inspect/cleanup commands, and warn when an objective has persisted run evidence but no objective JSONL evidence chain. The reference hygiene check must surface required reference-pattern coverage and warn when the local reference set lacks required coverage for agent runtime, durable workflow, external protocol, tool contracts, observability, policy boundaries, state graphs, sandbox runtime, or low-level isolation. The tool-exposure check must report `session_read_tools_default_tool_ids=["artifact-read","glob","grep","read"]` with no extras, missing defaults, non-model-visible defaults, or internal `invalid` exposure. The missing-evidence warning must include exact `harness objectives reconcile-evidence <objective_id> --dry-run --output json` commands for operator preview. Draft-only objectives without runs must not trigger the missing-evidence warning. Audits must not clear pending-action metadata, backfill objective evidence, invoke objective evidence reconciliation, replay captured logs by executing side effects, or repair runtime state. They must report `reference_code_imported=false`, `reference_contents_included=false`, `provider_called=false`, `network_called=false`, `adapter_execution_started=false`, `filesystem_modified=false`, and `permission_granting=false`. On uninitialized projects they must not create `.harness/`.
- `harness evals run --suite orchestration-workflows --output json`, `harness orchestration workflows --output json`, and `GET /orchestration/workflows` return `harness.workflow_coordination_catalog/v1` plus `harness.workflow_coordination_summary/v1` for server projections. They must include durable supervisor, sequential steps, bounded parallel fan-out, typed agent handoff, human approval pause, append-only replay, external protocol boundary, and memory context boundary rows; separate session, workflow, memory, and artifact state classes; keep `reference_code_imported=false`, `reference_contents_included=false`, `provider_called=false`, `network_called=false`, `adapter_execution_started=false`, `tool_execution_started=false`, `agent_execution_started=false`, `filesystem_modified=false`, `artifact_bodies_read=false`, `model_context_allowed=false`, and `permission_granting=false`; and avoid creating `.harness/` on uninitialized projects.
- `harness evals run --suite orchestration-scenarios --output json`, `harness orchestration scenarios --output json`, and `GET /orchestration/scenarios` return `harness.orchestration_scenario_catalog/v1` plus `harness.orchestration_scenario_summary/v1` for server projections. They must include duplicate dispatch/redelivery, slow branch barrier, approval reject pause, checkpoint reject stop, missing terminal event, unsafe memory-to-hosted-model, remote protocol fail-closed, retry/idempotency, and live benchmark explicit-permit rows; cover unit, contract, replay, scenario, security, and benchmark layers; keep `reference_code_imported=false`, `reference_contents_included=false`, `provider_called=false`, `network_called=false`, `adapter_execution_started=false`, `tool_execution_started=false`, `agent_execution_started=false`, `filesystem_modified=false`, `artifact_bodies_read=false`, `model_context_allowed=false`, `live_benchmark_execution_allowed=false`, `approval_store_instantiated=false`, and `permission_granting=false`; and avoid creating `.harness/` on uninitialized projects.
- `harness evals run --suite orchestration-replay --output json` and `harness orchestration replay --output json` return `harness.orchestration_replay_audit/v1`, run the five bounded synthetic replay cases, passively reduce captured objective JSONL evidence when present, skip captured replay on uninitialized projects, and avoid creating `.harness/`. They must detect expected synthetic issues for duplicate dispatch, slow-branch barrier drift, approval-reject drift, and missing-terminal drift while keeping `provider_called=false`, `network_called=false`, `adapter_execution_started=false`, `filesystem_modified=false`, `artifact_bodies_read=false`, `model_context_allowed=false`, and `permission_granting=false`.
- `harness evals run --suite orchestration-efficiency --output json` returns `harness.orchestration_efficiency/v1` and remains read-only. It must report adapter security-versus-complexity measurements, manual queue and foreground core pre-lease descriptor approval gating, no-run registered-adapter rejection finalization, daemon renewal, expired-lease recovery, and stop/stale linked-run guards, retry/idempotency guardrails, a deterministic bounded critical-path scheduler probe, microbenchmark contracts, live-only benchmark permit contracts, and existing evidence-to-trace projection counts when runtime state exists. Live benchmark permits must use `harness.orchestration_live_benchmark_permits/v1`, report approval/budget/boundary metadata for sandbox startup and shared LLM contention, keep `automated_execution_allowed_count=0`, and keep `release_blocking_count=0`. It must report `provider_called=false`, `network_called=false`, `adapter_execution_started=false`, `filesystem_modified=false`, `permission_granting=false`, `artifact_bodies_read=false`, `reference_code_imported=false`, and `reference_contents_included=false`. On uninitialized projects it must not create `.harness/`.
- `harness evals run --suite orchestration-microbenchmarks --output json` returns `harness.orchestration_microbenchmarks/v1` and remains read-only. It must time only passive/synthetic in-process projections for handoff overhead, fan-out/fan-in scheduling, checkpoint verification when runtime state exists, tool adapter overhead, retry safety, trace projection when evidence exists, and verification-stage ROI. Timed rows must include non-blocking `harness.orchestration_microbenchmark_guardrail/v1` mean/p95 threshold metadata. It must mark sandbox startup and shared model contention as `skipped` with `measurement_mode=explicit_live_required`; each skipped live row must include `measurements.live_permit.schema_version=harness.orchestration_live_benchmark_permit/v1`, `automated_execution_allowed=false`, and `release_blocking=false`. It must report the same passive safety flags as the efficiency audit and avoid creating `.harness/` on uninitialized projects.
- `harness evals run --suite orchestration-synthesis --output json` and `harness orchestration synthesis --output json` return `harness.orchestration_synthesis/v1` and remain read-only. The report must compose readiness, efficiency, microbenchmark, replay drift, and reference-repository summaries; include adopted reference-pattern decisions such as `external_protocol_interoperability` and deliberate non-adoptions such as `no_fail_open_remote_protocol_execution` and `no_replay_side_effect_execution`; report `live_benchmarks_automatic=false` and `live_benchmarks_release_blocking=false`; and keep `reference_code_imported=false`, `reference_contents_included=false`, `provider_called=false`, `network_called=false`, `adapter_execution_started=false`, `filesystem_modified=false`, `permission_granting=false`, and `artifact_bodies_read=false`. The TUI dashboard Evidence pane must expose the compact no-reference `harness.orchestration_synthesis_summary/v1` posture row without initializing projects.
- The TUI cockpit Evidence section and `GET /orchestration/readiness` expose the same readiness summary as passive operator context. The TUI command palette includes manual entries for readiness, workflow coordination, orchestration scenarios, and synthesis. The TUI cockpit Evidence section also exposes `harness.orchestration_microbenchmarks_summary/v1` from a single bounded passive sample plus the full microbenchmark inspection command and `harness.orchestration_synthesis_summary/v1` for the compact no-reference posture. `GET /agents/discovery` exposes `harness.agent_discovery_catalog/v1` plus `harness.agent_discovery_summary/v1`; `GET /agents/allocation` exposes `harness.delegate_allocation/v1`; `GET /orchestration/scenarios` exposes `harness.orchestration_scenario_catalog/v1` plus `harness.orchestration_scenario_summary/v1`; `GET /orchestration/efficiency` exposes the passive security-versus-complexity audit, `GET /orchestration/microbenchmarks` exposes `harness.orchestration_microbenchmarks/v1` plus `harness.orchestration_microbenchmarks_summary/v1` with the same skipped live benchmark permit metadata as the CLI, and `GET /orchestration/synthesis` exposes `harness.orchestration_synthesis/v1` plus `harness.orchestration_synthesis_summary/v1`. They must not initialize uninitialized projects, import reference code, include reference source bodies, call providers, call network, execute adapters, mutate files, read artifact bodies, create task records, grant budgets, or grant permissions; reference repository metadata is opt-in with `include_references=true`.
- Blocked-state smoke should verify the same stable code appears in `daemon inspect-lease --output json`, text `daemon inspect-lease`, `daemon execute --output json`, `capabilities inspect`, chat “why is this blocked?”, and TUI/operator context for cases such as missing approval, disabled adapter, unsafe metadata, unknown adapter, sandbox profile mismatch, breaker open, and forbidden path or secret-like content.
- `daemon recover` may reconcile existing dry-run, read-only, or generic registered-adapter evidence but must not create a second run for a linked attempt.
- Registered dispatch does not authorize Docker-from-queue, shell execution, hosted fallback, paid fallback, OpenAI API usage, active repo writes without apply-back approval, MCP/A2A, browser/email/calendar tools, generic task execution, or unmanaged daemon loops.
- Output is schema-versioned and does not include backend settings, `api_key`, `OPENAI_API_KEY`, `base_url`, environment variables, or artifact file contents.
- Artifact records use explicit redaction states: clean evidence is `not_required`, secret-like text is represented by a redacted derived artifact with lineage metadata, and secret-like artifact paths are rejected.
- `harness compare "$RUN_ID" "$RUN_ID"` and baseline comparison against the same run should report no drift.

## Build Local Docker Test Image

```bash
harness tests image validate --project .
harness tests image build --project .
```

Equivalent raw Docker build command:

```bash
docker build -f Dockerfile.harness-test -t harness-test:local .
```

## Run Direct Docker Tests

```bash
harness tests run --project . -- python -m pytest -q
```

The command requires approval. Approve only after confirming the prompt shows a sanitized temporary workspace mounted to `/workspace`, not the active project root.

## Verify Latest Docker Run Artifacts

```bash
LATEST=$(ls -td .harness/runs/run_* | head -1)
cat "$LATEST/test_result.json"
cat "$LATEST/final_report.md"
```

## Optional: Codex Isolated Edit Smoke

The following commands create commits. Run them only in a disposable branch or when you intentionally want smoke-test commits.

Create a scratch file and commit it:

```bash
cat > scratch_codex_edit.py <<'EOF'
def greet():
    return "hello"
EOF
git add scratch_codex_edit.py
git commit -m "Add scratch file for Codex edit smoke test"
```

Create or refresh the required hosted data-boundary approval profile:

```bash
harness approvals add --backend codex_cli --data-boundary hosted_provider --project . --task-types codex_code_edit --duration-days 1
```

Run Codex in an isolated workspace:

```bash
harness run "Modify only scratch_codex_edit.py. Add a docstring inside greet(). Do not create, delete, or modify any other files." --project . --task-type codex_code_edit --keep-isolation
```

When prompted, use `view full diff`, `deny all changes`, or `approve all changes` according to the smoke objective. Denial should leave the active file unchanged.

Optional queued Codex dispatcher smoke:

```bash
harness tasks add --title "Codex queued scratch edit" --execution-adapter codex_isolated_edit --task-type codex_code_edit --project . --output json
harness daemon run-once --project . --output json
harness daemon inspect-lease "$LEASE_ID" --project . --output json
harness daemon execute "$LEASE_ID" --project . --output json
```

The queued smoke also requires hosted-boundary approval, but hosted-boundary approval is not apply-back approval. Apply-back remains denied by default unless an explicit apply-back approval provider is wired into the operator path.

## Model Registry Smoke

Active implementation plan: `docs/plans/model_provider_completion_execution_plan.md`. This smoke section covers the completed model/provider path: metadata projections, selection/default resolution, discovery cache, provider-account lifecycle, credential redaction, protocol-adapter conformance, usage/cost evidence, retry/abort evidence, TUI picker behavior, and runtime fail-closed gates. Catalog reads still do not prove runtime readiness; execution must revalidate the selected provider/model, approvals, credentials, and protocol adapter before a provider client is constructed.

Run the model catalog metadata commands:

```bash
harness providers list --project . --output json
harness providers status --project . --output json
harness models list --project . --output json
harness models providers --project . --output json
harness models inspect codex/gpt-5.5 --project . --output json
harness models validate codex/gpt-5.5 --project . --output json
harness models validate missing_provider/not-a-real-model --project . --output json || true
harness models validate openai/gpt-5.3-codex --project . --output json || true
harness models protocols --project . --output json
harness models preferences --project . --output json
harness models default codex_cli/gpt-5.5 --project . --output json
harness models favorite codex_cli/gpt-5.5 --project . --output json
harness models unfavorite codex_cli/gpt-5.5 --project . --output json
harness models config validate --project . --output json
harness models refresh local_openai_compatible --project . --output json
harness models refresh local_openai_compatible --clear-cache --project . --output json
```

Run the exact model/provider operator path against a throwaway project:

```bash
tmp_provider_path="${TMPDIR:-/tmp}/harness-model-provider-path-smoke"
rm -rf "$tmp_provider_path"
harness init --project "$tmp_provider_path"
harness providers list --project "$tmp_provider_path" --output json
harness models list --project "$tmp_provider_path" --output json
harness models validate codex/gpt-5.5 --project "$tmp_provider_path" --output json
harness models validate missing_provider/not-a-real-model --project "$tmp_provider_path" --output json || true
OPENAI_API_KEY=sk-redacted \
  harness providers login paid_openai_compatible \
    --credential-kind env \
    --env-var OPENAI_API_KEY \
    --project "$tmp_provider_path" \
    --output json
harness providers accounts paid_openai_compatible --project "$tmp_provider_path" --output json
session_id="$(
  python - "$tmp_provider_path" <<'PY'
from pathlib import Path
import sys
from harness.memory.sqlite_store import SQLiteStore

store = SQLiteStore.open_initialized(Path(sys.argv[1]))
print(store.create_session(title="Model provider smoke").id)
PY
)"
harness session model "$session_id" local/qwen3-coder --project "$tmp_provider_path" --output json
harness session inspect "$session_id" --project "$tmp_provider_path" --output json
harness models refresh local_openai_compatible --project "$tmp_provider_path" --output json
```

Run the session/runtime and TUI picker smoke coverage:

```bash
pytest -q \
  tests/test_session_runtime.py::test_session_runtime_uses_session_selected_model_with_resolution_event \
  tests/test_session_runtime.py::test_runtime_blocks_missing_env_credential_before_network
pytest -q \
  tests/test_tui_codex_mode.py::test_model_picker_renders_protocol_alias_boundary_and_blocked_state \
  tests/test_tui_codex_mode.py::test_model_picker_sections_current_favorites_recent_connected_local_hosted_blocked \
  tests/test_tui_codex_mode.py::test_model_picker_shows_provider_connect_action_for_missing_credentials
pytest -q \
  tests/test_session_timeline.py::test_session_timeline_renders_usage_and_cost_evidence \
  tests/test_tui_backend_wiring.py::test_cli_tui_server_catalogs_share_active_registry_status
```

Manual TUI picker inspection:

```bash
harness --project "$tmp_provider_path"
```

In the TUI, open `ctrl+x m` or type `/models`, then inspect `/model` search and `/model <number>` selection. Verify the selected row detail panel shows canonical ref, protocol, source, context, output limit, reasoning, variants, modalities, tools, boundary, credentials, cost, blocked reasons, and provider action hints. Verify the picker groups current, favorites, recents, connected, local, hosted, and blocked rows in that order and that selecting a row persists session metadata only.

In the TUI, open `/models`, `/model`, or `ctrl+x m` and verify provider connect is part of the model picker rather than a separate provider browser. Select a credential-blocked model/provider row and press `Ctrl+A`; verify the account/auth-method dialog shows supported methods such as API key, environment variable, OAuth/manual code, AWS methods, static local, or Codex login as applicable. Also type `/provider` and verify it opens the same model picker flow instead of a separate `Connect a provider` list. For API-key connect, enter a test key and verify the dialog masks input, the transcript/status/dialog do not contain the raw key, evidence reports `credential_value_included=false`, `credentials_included=false`, `provider_execution_started=false`, `model_execution_started=false`, and `network_accessed=false`, and the UI returns to the model picker filtered to that provider. For env connect, verify only the env var name is stored and the env value is not read or rendered. For local-only methods such as `static_local`, `codex_login`, `aws_env`, or `aws_profile`, verify the account row is created without a secret prompt and without provider/model execution.

Run a custom local provider/config smoke in a throwaway project:

```bash
tmp_models_project="${TMPDIR:-/tmp}/harness-model-config-smoke"
rm -rf "$tmp_models_project"
harness init --project "$tmp_models_project"
mkdir -p "$tmp_models_project/.harness"
cat > "$tmp_models_project/.harness/models.yaml" <<'EOF'
providers:
  smoke_local:
    display_name: Smoke Local
    enabled: true
    data_boundary: local_only
    base_url: http://localhost:11434/v1
    protocol: openai_chat
    credential:
      kind: static_local
    models:
      smoke-model:
        display_name: Smoke Model
        api_id: smoke-model
        context_window: 8192
        max_output_tokens: 1024
        input_modalities: [text]
        output_modalities: [text]
        tool_support: true
        reasoning_support: native
        cost:
          input_per_1m: 1.0
          output_per_1m: 2.0
        status: active
EOF
harness models config validate --project "$tmp_models_project" --output json
harness models inspect smoke_local/smoke-model --project "$tmp_models_project" --output json
harness models validate smoke_local/smoke-model --project "$tmp_models_project" --output json
```

Run the explicit provider account lifecycle against a throwaway project:

```bash
tmp_project="${TMPDIR:-/tmp}/harness-provider-smoke"
rm -rf "$tmp_project"
harness init --project "$tmp_project"
OPENAI_API_KEY=sk-redacted \
  harness providers login paid_openai_compatible --project "$tmp_project" --output json
harness providers accounts paid_openai_compatible --project "$tmp_project" --output json
harness providers status --project "$tmp_project" --output json
harness providers activate-account paid_openai_compatible <account_id> --project "$tmp_project" --output json
harness providers logout paid_openai_compatible --project "$tmp_project" --output json
```

Expected safety properties:

- List, providers, inspect, validate, and protocols do not call providers, preflight endpoints, read credentials, start adapters, or grant hidden fallback.
- `harness providers list`, `harness models list`, known-model validation, unknown-model validation, env-provider connect, session model selection, selected-session runtime tests, missing-credential runtime tests, local refresh, and TUI picker inspection are all covered by the smoke sequence above.
- `harness models protocols --output json` includes `anthropic_messages`, `bedrock_converse`, `codex_cli`, `google_generative`, `openai_chat`, `openai_responses`, and `openai_codex_responses`.
- Alias inspection records both `raw_model_ref` and `canonical_model_ref`.
- Disabled hosted aliases, such as `openai/gpt-5.3-codex`, fail validation with `provider_disabled`.
- Model validation blocks unsupported context, output, reasoning, modality, and tool requests before provider execution.
- `harness models preferences`, `favorite`, `unfavorite`, and `default` mutate only local preference records and preserve `provider_execution_started=false`, `model_execution_started=false`, `network_accessed=false`, and `no_hidden_fallback=true`.
- Runtime default model resolution emits `session.model_resolution` before validation, records the source of the selected candidate, and never tries later defaults as fallback when the selected candidate is blocked.
- Missing defaults fail with `model_ref_missing`; invalid or disabled operator/workspace/session/workbench defaults fail closed with validation blocked reasons and keep `hidden_provider_fallback=false`, `hidden_model_fallback=false`, and `no_hidden_fallback=true`.
- `harness models refresh local_openai_compatible --project . --output json` may call only the validated local `/models` endpoint.
- `harness models refresh paid_openai_compatible --project . --output json` fails closed before network access unless `--approve-hosted` is explicitly provided for that refresh.
- Discovery `--approve-hosted` is not a runtime execution approval; sessions using hosted, paid, or external-router/data-boundary providers must still block before credentials/network unless the matching runtime approval exists.
- `harness models refresh <provider_id> --with-credentials` resolves credentials only for explicit discovery, keeps credential values redacted, and fails before network when required credentials are missing.
- Successful explicit refresh persists discovered rows as `source=discovered`; later catalog reads include them without another provider call, and `--clear-cache` removes only those discovered rows without touching built-ins, custom config, aliases, accounts, credentials, favorites, defaults, or approvals.
- `.harness/models.yaml`, when present, is reloaded by catalog listing and validation; local custom providers must use loopback or explicitly approved LAN URLs, hosted custom providers require explicit approval before they can be enabled, and credential/header fields must not contain raw values.
- `harness providers login` records redacted account metadata only; it does not print or persist raw credential values, call providers, start execution, or grant hidden fallback.
- `harness providers accounts`, `harness providers activate-account`, and `harness providers logout` expose explicit account lifecycle state with `credentials_included=false` and `network_accessed=false`.
- CLI and local-server account/status JSON may show env var names as credential references, but TUI model/provider projections must redact those names as `env:<redacted>` and must not contain `OPENAI_API_KEY` or raw env/header values.
- Provider connect/disconnect through CLI or local-server auth routes mutates only local account/secret-store state: it does not select a model, refresh discovery, validate credentials with a provider call, grant hosted/data-boundary approval, or start provider/model execution.
- TUI provider connect is launched from `/models` and uses the same account/secret-store actions as CLI/server connect, masks API-key entry, clears typed secret buffers after submission, persists redacted evidence, and returns to the model picker only after account state has been written. `/provider` is only a compatibility alias into that model-picker flow.
- API-key and OAuth token writes store secret material only in `.harness/provider_secrets.json` under local `0600` file permissions; command output and provider account events report account id, status, credential kind, write/removal state, and `credential_value_included=false` without printing stored, env, header, OAuth, or removed values.
- Protocol adapter conformance and handoff tests run offline with `pytest tests/test_protocol_adapters.py tests/test_cross_provider_handoff.py -q`; failures must identify the adapter and unsupported canonical part.
- Session/runtime usage evidence should include `normalized_usage` and, when model cost metadata is present, `estimated_cost`; provider-reported cost should remain under `provider_reported_cost`.
- Session timeline rendering should show token usage and cost evidence when available, including explicit estimated-cost source/estimated marker and provider-reported cost as a separate field.
- In the TUI, `/models` and `/model` show selected-model details, protocol, source, canonical ref for aliases, context limit, max output, reasoning support, modalities, tool support, provider boundary, credentials, cost, and blocked reasons without starting model execution.
- In the TUI, `/models` shows provider connect/auth-method choices without starting provider/model execution; API-key/env/OAuth/local-account actions are explicit account actions, not model validation or credential tests.
- TUI model picker ordering is current session model, favorites, recents, connected providers, local providers, hosted providers, then disabled or blocked providers; selecting an executable row persists session metadata and validation evidence only, while blocked selections leave the session model unchanged and record blocked reasons.
- TUI model picker action keys for favorite/default/inspect/provider refresh/provider connect/provider disconnect route through explicit model preference or provider-management actions; opening, filtering, navigating, and selection keep `provider_execution_started=false`, `model_execution_started=false`, `network_accessed=false`, `permission_granting=false`, and `no_hidden_fallback=true`.

Release gate:

```bash
harness doctor --release --project . --output json
```

Expected model-provider release gate properties:

- `model_provider_release_gates` reports `catalog_secret_values_included=false`, `picker_provider_calls_allowed=false`, `provider_execution_started=false`, `model_execution_started=false`, `network_accessed=false`, `credentials_included=false`, `hidden_provider_fallback=false`, `hidden_model_fallback=false`, and `no_hidden_fallback=true`.
- `model_provider_release_gates.registered_protocols` includes all seven built-in protocols and `unregistered_protocol_blocks_before_execution=true`.
- `session_transcript_health` reports `schema_version=harness.session_transcript_health/v1`, fails release on malformed session transcript JSONL, and keeps `contents_included=false`, `filesystem_modified=false`, and `permission_granting=false`.
- `harness session inspect`, `harness resume`, `/sessions`, `/api/session`, `/sessions/status`, `/sessions/{id}`, `/sessions/{id}/status`, the dashboard, session pane, and right pane expose `transcript_health` with `schema_version=harness.session_events_read/v1` and do not expose malformed transcript line bodies or secret-looking strings.
- `orchestration_readiness_release_gates` reports `schema_version=harness.orchestration_readiness_audit/v1`, includes `pending_chat_action_recovery`, `agent_discovery_and_allocation`, `schema_compatibility_contracts`, `workflow_coordination_contracts`, `orchestration_scenario_conformance`, `replay_drift_detection`, and `agentic_security_controls` in `check_ids`, uses `reference_metadata_included=false`, and keeps `provider_called=false`, `network_called=false`, `adapter_execution_started=false`, `filesystem_modified=false`, and `permission_granting=false`. Invalid or stale pending-action metadata should make this check `warn`, not clear the metadata.
- `orchestration_efficiency_release_gates` reports `schema_version=harness.orchestration_efficiency/v1`, includes `adapter_security_complexity_tradeoff`, `bounded_critical_path_scheduler`, `live_benchmark_permits`, `microbenchmark_contracts`, and `replay_retry_idempotency` in `check_ids`, reports `adapter_rejection_finalization_enforced=true`, `read_only_compatibility_rejection_finalization_enforced=true`, `daemon_renewal_inconsistent_lease_guard_enforced=true`, `daemon_recovery_expired_lease_guard_enforced=true`, `daemon_shutdown_linked_run_guard_enforced=true`, and `lease_mutation_authority_guard_enforced=true` under adapter security measurements, uses `reference_metadata_included=false`, and keeps `provider_called=false`, `network_called=false`, `adapter_execution_started=false`, `filesystem_modified=false`, `permission_granting=false`, and `artifact_bodies_read=false`.
- `orchestration_synthesis_release_gates` reports `schema_version=harness.orchestration_synthesis/v1`, includes source statuses for readiness, efficiency, and microbenchmarks, includes adopted reference-pattern ids such as `external_protocol_interoperability` plus deliberate non-adoption ids such as `no_fail_open_remote_protocol_execution` and `no_replay_side_effect_execution`, uses `reference_metadata_included=false`, and keeps `provider_called=false`, `network_called=false`, `adapter_execution_started=false`, `filesystem_modified=false`, `permission_granting=false`, `artifact_bodies_read=false`, `live_benchmark_execution_allowed=false`, `mutation_allowed=false`, and `model_context_allowed=false`.
- Runtime context-overflow retry evidence should preserve the same provider/model and emit `harness.runtime_compaction/v1` with retained/dropped message ids, retained/dropped group ids, `provider_summarization_used=false`, `hidden_provider_fallback=false`, `network_called=false`, `filesystem_modified=false`, and `permission_granting=false`.

Model/provider release-readiness checklist:

- [ ] CLI, local server, TUI, and session runtime all project the same provider/model catalog state for built-in, custom-config, static-catalog, and discovered rows.
- [ ] Local provider flow is documented and smoke-tested: configure `.harness/models.yaml`, validate config, list/inspect/validate the model, refresh local discovery, select the model, and run with the selected model.
- [ ] Hosted provider flow is documented and smoke-tested: connect an env or API-key account, verify redacted account state, approve the hosted and paid/data-boundary runtime gates where required, select the model, and block before network when approval or credentials are missing.
- [ ] Credential redaction is verified across CLI JSON, local-server JSON, TUI projections, session events, provider events, discovery cache metadata, account events, and release-gate output.
- [ ] Provider connect/disconnect actions mutate only account and secret-store state; they do not refresh discovery, select models, call providers, grant approval, or start execution.
- [ ] Model picker behavior is verified for ordering, search, details panel fields, blocked rows, favorite/default/inspect actions, provider connect/refresh/disconnect hints, and no provider/model execution while navigating.
- [ ] Default model resolution emits `session.model_resolution`, records the selected source, validates the selected candidate, and fails closed instead of trying later defaults as fallback.
- [ ] Discovery is explicit, cache-backed, source-labeled, clearable per provider, local-endpoint validated, hosted-approval gated, and credential-backed only when `--with-credentials` is requested.
- [ ] Runtime policy gates block before credential resolution and provider client construction for missing hosted approval, paid-provider approval, data-boundary approval, missing credentials, unknown providers/models, disabled providers/models, unsupported capabilities, and missing protocol adapters.
- [ ] Retry evidence preserves the same provider, model, alias/canonical ref, variant, protocol, reasoning/options, and request metadata across attempts; retry scheduling records attempt, delay, category, retryable, and no hidden fallback.
- [ ] Protocol adapter coverage includes offline payload, streaming, usage/cost, tool-call, provider-error, abort, partial-response, and cross-provider handoff tests for every registered protocol.
- [ ] Release docs are current: `docs/operator_guide.md`, `docs/command_catalog.md`, `docs/smoke_checklist.md`, and `docs/plans/model_provider_completion_execution_plan.md` describe only implemented behavior and no advertised dead controls.
- [ ] Required checks pass: `pytest -q tests/test_docs_phase_3d.py`, the focused model/provider smoke tests listed above, `pytest -q tests/test_protocol_adapters.py tests/test_cross_provider_handoff.py`, `pytest -q`, and `harness doctor --release --project . --output json`.

Verify the active file after denial or apply-back:

```bash
cat scratch_codex_edit.py
git status --short
```

Optional cleanup. These commands also create a commit:

```bash
git rm scratch_codex_edit.py
git commit -m "Remove scratch Codex smoke file"
```

## Expected Safety Properties

- `codex_code_edit` edits only an isolated workspace until explicit apply-back approval.
- Denying apply-back leaves the active project unchanged.
- Direct Docker tests mount only a sanitized temporary workspace to `/workspace`.
- Docker test network is disabled by default.
- Docker test denial does not call Docker.
- `run_tests` is model-visible only for `simple_code_edit`.
- No command commits or pushes unless an optional smoke step explicitly runs `git commit`.
