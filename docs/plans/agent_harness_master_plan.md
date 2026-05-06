# Agent Harness Master Plan

Imported from `/Users/oscarxue/Downloads/agent_harness_master_plan.md` as the repo-local planning reference. This roadmap snapshot preserves strategic direction and safety boundaries, but it is not blanket implementation approval for every listed item.

## 1. Product Definition

`agent-harness` is a local-first supervised agent runtime for controlled work on local projects. It records runs, artifacts, approvals, backend metadata, data-boundary decisions, isolated edits, test results, and safety events.

The near-term system is a safety-focused local runner. The long-term system is a 24/7 local autonomous team of specialized agents for quant finance, coding, research, job applications, writing, and other personal work.

The central rule is that agents may propose, plan, delegate, critique, edit in isolation, and produce artifacts. The harness decides what they are allowed to do.

## 2. Current State

The current repository is best described as a v0.1 safety kernel. It already provides the core local control plane and safety boundaries needed for future autonomous workflows.

Implemented capabilities include:

- Typer CLI exposed as `harness`.
- `harness init` project initialization into `.harness/`.
- SQLite persistence for runs, events, artifacts, backend metadata, and approvals.
- Run artifact directories under `.harness/runs/`.
- Backend metadata models for billing mode, execution location, data boundary, network allowance, and backend capabilities.
- Local read-only repository tools.
- Codex CLI as a supervised external agent backend.
- Hosted data-boundary approval profiles.
- Isolated Codex edit workspaces.
- Diff inspection and policy validation before apply-back.
- Explicit apply-back approval before touching the active repository.
- Native local edit loop with constrained tool protocol.
- Docker-sandboxed test execution.
- Managed Docker test image helpers.
- Path traversal protection.
- Secret-path blocking.
- Secret-scanner primitives.
- Broad regression tests covering config, security paths, approvals, protocols, backends, isolation, patching, Docker sandboxing, and CLI smoke behavior.

This should not be treated as a throwaway prototype. It is the security and evidence substrate for the later autonomous system.

## 3. Strategic Positioning

### 3.1 What the system is

A local/private agent orchestration layer with strict execution boundaries.

It should support:

- Local-first operation.
- Explicit data-boundary approvals.
- Local models where possible.
- Codex subscription as a supervised external coding backend while bootstrapping.
- Durable run evidence.
- Reviewable patches.
- Local artifact storage.
- Sandboxed test execution.
- Domain-specialized agent teams.
- Eventually, 24/7 local daemon operation.

### 3.2 What the system is not yet

It is not yet:

- A general hosted agent platform.
- A raw OpenAI API wrapper.
- A paid API fallback layer.
- A plugin marketplace.
- A browser/email/calendar automation agent.
- A multi-tenant server.
- A live trading system.
- A system that should auto-send job applications or emails.
- A system that should auto-place trades or broker orders.

## 4. Core Design Principles

1. **Local-first by default.** Local/private data should stay local unless an explicit hosted-boundary approval exists.

2. **Evidence over chat.** Every important action should produce events, artifacts, manifests, and reports.

3. **Supervised mutation.** Agents may edit isolated workspaces, but active repositories are modified only through validated, approved apply-back.

4. **No hidden fallbacks.** No hosted, paid, or networked fallback should occur without explicit policy and approval.

5. **Agent permissions are declarative.** Agents do not decide what tools they can use. Workbench, agent, and task policies define that.

6. **Hierarchy must not weaken safety.** Child agents may narrow permissions but cannot override forbidden workbench-level boundaries.

7. **Orchestration is above the harness, not beside it.** Orchestrators create and delegate tasks through the same persistence, approval, artifact, and policy system.

8. **Autonomy is bounded.** A 24/7 daemon may run safe tasks automatically, but sensitive actions must pause at approval gates.

## 5. Target Architecture

```text
agent-harness core
  runs
  events
  artifacts
  approvals
  backend descriptors
  model profiles
  tool policies
  memory scopes
  isolation manager
  Docker sandbox
  secret scanner
  JSON/JSONL protocol

workbench layer
  quant workbench
  personal workbench
  coding workbench
  research workbench
  writing workbench
  future workbenches

agent hierarchy
  workbench orchestrator
  agent groups
  specialist agents
  specialist profiles
  reviewers

orchestration layer
  objectives
  plans
  tasks
  dependencies
  delegation records
  review gates
  approval gates
  swarm sessions

daemon layer
  local scheduler
  task queue runner
  resource limits
  heartbeat events
  approval blocking
  artifact generation
```

## 6. Version Roadmap

### v0.1 — Safety Kernel

Current state. The focus is local project state, persistence, approvals, Codex supervision, isolated editing, patch apply-back, Docker tests, and safety primitives.

Remaining v0.1 work:

- Add `RunMode` enum.
- Add `BackendDescriptor` contract.
- Add run `manifest.json` for every run.
- Add stable JSON/JSONL CLI output.
- Add `SECURITY.md` and formal threat model.
- Add `harness doctor`.
- Add golden end-to-end tests.

### v0.2 — Agent Specs and Model Profiles

Introduce generic agent definitions and model routing without autonomy.

Add:

- `AgentSpec`.
- `WorkbenchSpec`.
- `ModelProfile`.
- `ToolPolicy`.
- `MemoryScope`.
- Agent registry.
- Built-in starter agents.

Starter agents:

- `repo_inspector`.
- `code_editor`.
- `test_runner`.
- `quant_researcher`.
- `job_researcher`.

### v0.3 — Task Queue and Manual Scheduler

Add persistent tasks, dependencies, and manual task execution before a daemon.

Commands:

```bash
harness tasks add tasks/idea.md --agent quant_researcher
harness tasks list
harness tasks inspect <task_id>
harness tasks run-next
```

### v0.4 — Local Daemon

Add conservative local 24/7 operation.

Commands:

```bash
harness daemon start --project .
harness daemon stop --project .
harness daemon status --project .
```

Initial daemon constraints:

- One objective at a time.
- One task at a time by default.
- No auto-sending external messages.
- No active repo writes.
- No broker/trading actions.
- Codex only through approval and isolation.
- Pause on approval gates.
- Write heartbeat events.

### v0.5 — Quant Workbench

Introduce quant finance agents and structured workflows.

Initial agents:

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

Initial quant tasks:

- Summarize a paper into a strategy hypothesis.
- Convert a strategy idea into a backtest specification.
- Generate a data requirement checklist.
- Draft a backtest module in isolated workspace.
- Run Docker tests.
- Produce a risk review.
- Compare backtest outputs.

Hard boundary: no live trading, broker integration, capital allocation, or order placement.

### v0.6 — Personal Workbench

Introduce personal productivity and job-application agents.

Initial agents:

- `personal_orchestrator`.
- `job_researcher`.
- `cv_matcher`.
- `cover_letter_drafter`.
- `recruiter_outreach_drafter`.
- `application_reviewer`.
- `factuality_reviewer`.
- `tone_reviewer`.

Hard boundary: drafts only. No automatic sending, submitting, uploading, or messaging.

### v0.7 — Multi-Agent Workflows

Introduce plans, task dependencies, review gates, and orchestrator-managed workflows.

Example quant workflow:

```text
objective: research futures curve carry signal
  -> commodities_researcher produces research brief
  -> data_engineer produces data requirements
  -> backtest_engineer proposes implementation plan
  -> implementation_engineer edits in isolated workspace
  -> Docker tests run
  -> risk_reviewer critiques
  -> statistical_validity_reviewer critiques
  -> final report produced
  -> human approval required for promotion/apply-back
```

### v0.8 — Bounded Swarm Sessions

Introduce bounded multi-agent debate and parallel review sessions.

Swarm sessions must have:

- Moderator/orchestrator.
- Fixed participants.
- Maximum rounds.
- Maximum runtime.
- Maximum parallel tasks.
- Explicit output contracts.
- Persisted artifacts.
- No hidden recursive delegation.

### v1.0 — Autonomous Supervised Agent Team

The v1 target is a 24/7 local autonomous supervised agent team running on dedicated hardware.

The system should support:

- Local model routing.
- Multiple workbenches.
- Hierarchical specialist agents.
- Local daemon.
- Objective/task queue.
- Orchestrator agents.
- Review agents.
- Bounded swarm sessions.
- Approval gates.
- Local artifact memory.
- Safe Codex escalation where approved.
- Human-controlled sensitive actions.

## 7. Workbench Design

A workbench is a domain package on top of the generic harness core.

Each workbench defines:

- Domain identity.
- Allowed agents.
- Allowed tools.
- Default model profile.
- Memory scope.
- Approval policy.
- Output conventions.
- Forbidden actions.

Example:

```yaml
id: quant
description: Quant finance research and engineering workbench

allowed_agents:
  - quant_orchestrator
  - quant_researcher
  - commodities_researcher
  - equities_researcher
  - data_engineer
  - backtest_engineer
  - risk_reviewer

allowed_tools:
  - repo_read
  - artifact_read
  - artifact_write
  - isolated_edit
  - docker_tests

memory_scope: quant
default_model_profile: local_reasoning

approval_policy:
  external_network: required
  repo_write: required
  active_repo_apply: required
  codex_hosted_boundary: required
  email_send: forbidden
  broker_action: forbidden
```

## 8. Hierarchical Agent Tree

The system should support nested agent branches.

Conceptually:

```text
Workbench
  └── AgentGroup
        └── SpecialistAgent
              └── SpecialistProfile
```

For quant:

```text
quant_workbench
  ├── quant_orchestrator
  ├── quant_research
  │     ├── macro_researcher
  │     ├── commodities_researcher
  │     │     ├── futures_curve_researcher
  │     │     ├── energy_researcher
  │     │     └── metals_researcher
  │     ├── equities_researcher
  │     ├── fixed_income_researcher
  │     └── volatility_researcher
  ├── quant_development
  │     ├── research_engineer
  │     ├── data_engineer
  │     ├── backtest_engineer
  │     ├── low_level_optimizer
  │     └── production_reviewer
  ├── trading_analysis
  │     ├── execution_researcher
  │     ├── risk_reviewer
  │     ├── portfolio_reviewer
  │     └── transaction_cost_reviewer
  └── review
        ├── methodology_reviewer
        ├── leakage_reviewer
        ├── statistical_validity_reviewer
        └── implementation_auditor
```

For personal work:

```text
personal_workbench
  ├── personal_orchestrator
  ├── job_applications
  │     ├── job_researcher
  │     ├── cv_matcher
  │     ├── cover_letter_writer
  │     ├── recruiter_outreach_drafter
  │     └── application_reviewer
  ├── writing
  │     ├── latex_editor
  │     ├── essay_reviewer
  │     ├── technical_writer
  │     └── proofreader
  └── admin
        ├── calendar_planner
        ├── email_drafter
        └── task_organizer
```

## 9. Agent Inheritance and Permission Rules

Agent hierarchy should be implemented through inheritance and overrides.

Resolution order:

```text
workbench defaults
  -> agent group defaults
    -> specialist agent overrides
      -> specialist profile overrides
        -> task-specific overrides
```

Child agents may narrow permissions but must not broaden workbench-level restrictions.

Permission monotonicity:

```text
forbidden          cannot be overridden by child
approval_required can remain approval_required or become forbidden
allowed           can remain allowed or become narrower
```

Example parent group:

```yaml
id: quant_research
workbench: quant
kind: agent_group

defaults:
  model_profile: local_reasoning
  memory_scope: quant
  tools:
    - artifact_read
    - artifact_write
    - repo_read
  permissions:
    write_active_repo: false
    run_tests: false
    external_network: approval_required
    broker_action: forbidden
```

Example specialist:

```yaml
id: commodities_researcher
parent: quant_research
kind: specialist_agent

role: >
  Researches commodities markets, futures curves, seasonality,
  carry, inventory dynamics, macro drivers, and strategy hypotheses.

overrides:
  knowledge_domains:
    - commodities
    - futures
    - energy
    - metals
    - agriculture

outputs:
  - research_brief.md
  - hypothesis.json
  - data_requirements.md
```

## 10. Orchestrator Design

The orchestrator is an agent that plans, delegates, waits, reviews, and synthesizes. It must not directly bypass the safety layer.

The orchestrator can:

- Create objectives.
- Create tasks.
- Delegate to agents.
- Read artifacts.
- Request reviews.
- Request human approval.
- Produce synthesis reports.

The orchestrator cannot:

- Apply patches directly.
- Edit active repos directly.
- Send emails automatically.
- Submit job applications.
- Place trades.
- Bypass data-boundary approvals.
- Bypass Docker/test approvals.
- Override child-agent safety policies.

Example orchestrator spec:

```yaml
id: quant_orchestrator
kind: orchestrator
workbench: quant

role: >
  Breaks quant objectives into bounded tasks, assigns specialist agents,
  requests reviews, and produces final synthesis.

permissions:
  create_tasks: true
  delegate_tasks: true
  read_artifacts: true
  write_artifacts: true
  write_active_repo: false
  run_tests: false
  send_external: false
  broker_action: false

limits:
  max_delegation_depth: 2
  max_parallel_tasks: 2
  max_tasks_per_objective: 12
  require_human_final_approval: true
```

## 11. Swarm Design

Swarm behavior should be bounded, artifact-driven, and moderated by an orchestrator.

A swarm session is not an infinite group chat. It is a controlled multi-agent work session with a fixed purpose.

Example:

```yaml
id: quant_signal_review_swarm
workbench: quant

participants:
  - commodities_researcher
  - backtest_engineer
  - risk_reviewer
  - statistical_validity_reviewer

moderator: quant_orchestrator

limits:
  max_rounds: 3
  max_parallel_tasks: 4
  max_runtime_minutes: 30

output_contracts:
  - consensus_summary.md
  - disagreements.json
  - next_actions.json

approval_policy:
  external_network: required
  code_edit: forbidden
  broker_action: forbidden
```

Swarm output must collapse into artifacts:

- `consensus_summary.md`.
- `disagreements.json`.
- `recommended_next_tasks.json`.
- `final_orchestrator_decision.md`.

## 12. Core Models to Add

### 12.1 Run and backend models

```python
class RunMode(str, Enum):
    INSPECT = "inspect"
    PLAN = "plan"
    EDIT_LOCAL = "edit-local"
    EDIT_ISOLATED = "edit-isolated"
    TEST_SANDBOX = "test-sandbox"
    APPLY = "apply"
    BACKGROUND = "background"
```

```python
class BackendDescriptor(BaseModel):
    name: str
    kind: BackendKind
    billing_mode: BillingMode
    execution_location: ExecutionLocation
    data_boundary: DataBoundary
    network_allowed: bool
    capabilities: dict[str, bool]
    allowed_modes: list[RunMode]
    requires_approval: list[str]
```

### 12.2 Workbench and agent models

```python
class WorkbenchSpec(BaseModel):
    id: str
    description: str
    allowed_agents: list[str]
    allowed_tools: list[str]
    memory_scope: str
    default_model_profile: str
    approval_policy: dict[str, str]
```

```python
class AgentNode(BaseModel):
    id: str
    parent_id: str | None = None
    workbench_id: str
    kind: Literal["group", "agent", "specialist", "orchestrator", "reviewer"]
    role: str
    model_profile: str | None = None
    memory_scope: str | None = None
    tools: list[str] | None = None
    permissions: dict[str, str] | None = None
    approval_policy: dict[str, str] | None = None
    output_contracts: list[str] | None = None
    tags: list[str] = []
```

### 12.3 Objective, plan, and task models

```python
class ObjectiveSpec(BaseModel):
    id: str
    workbench_id: str
    title: str
    instruction: str
    priority: int = 0
    max_runtime_minutes: int | None = None
    requires_human_final_approval: bool = True
```

```python
class PlanSpec(BaseModel):
    id: str
    objective_id: str
    planner_agent_id: str
    tasks: list[TaskSpec]
    dependencies: list[TaskDependency]
    review_gates: list[ReviewGate] = []
```

```python
class TaskSpec(BaseModel):
    id: str
    objective_id: str | None = None
    workbench_id: str
    agent_id: str
    instruction: str
    inputs: list[str] = []
    expected_outputs: list[str] = []
    mode: RunMode
    status: str = "created"
```

```python
class DelegationRecord(BaseModel):
    parent_task_id: str
    child_task_id: str
    delegated_by_agent_id: str
    delegated_to_agent_id: str
    reason: str
```

## 13. Model Routing Strategy

The system should support multiple backend/model profiles.

Initial profiles:

```yaml
models:
  local_small:
    backend: local_compatible
    endpoint: http://127.0.0.1:11434/v1
    model: qwen2.5-coder:1.5b
    boundary: local_only

  local_coder:
    backend: local_compatible
    endpoint: http://127.0.0.1:11434/v1
    model: qwen2.5-coder:3b
    boundary: local_only

  codex_supervised:
    backend: codex_cli
    boundary: hosted_provider
    requires_approval: true
    requires_isolation: true
```

Routing policy:

```yaml
routes:
  repo_summary:
    default: local_small
    fallback: none

  quant_research_note:
    default: local_reasoning
    fallback: none

  simple_code_edit:
    default: local_coder
    fallback: none

  serious_code_edit:
    default: codex_supervised
    requires:
      - hosted_provider_approval
      - isolated_workspace

  job_application_draft:
    default: local_reasoning
    requires:
      - no_auto_send
```

On an 8 GB M1 MacBook, local models should be used for small tasks:

- Repo summaries.
- Classification.
- Task routing.
- Drafting.
- Small code edits.
- Test failure explanation.

Codex should be used for:

- Non-trivial code edits.
- Refactors.
- Test repair.
- Implementation work.

Codex must always go through isolated workspaces and hosted-boundary approval.

## 14. JSON and Automation Protocol

The CLI should remain human-friendly but also expose stable machine-readable output.

Add:

```bash
harness runs --json
harness show <run_id> --json
harness events <run_id> --jsonl
harness artifacts <run_id> --json
harness diff <run_id> --json
harness tests run --json -- python -m pytest -q
harness backends preflight --json
harness approvals list --json
harness tasks list --json
harness objectives list --json
```

Rules:

- JSON output must be schema-versioned.
- JSON output must not include ANSI formatting.
- Event streams should be JSONL.
- CLI tests should snapshot representative JSON outputs.

## 15. Reproducibility Manifest

Every run should write `manifest.json`.

Example:

```json
{
  "schema_version": "harness.manifest/v1",
  "run_id": "run_...",
  "harness_version": "0.3.0",
  "repo": {
    "vcs": "git",
    "commit": "...",
    "branch": "...",
    "dirty": false
  },
  "task": {
    "type": "codex_code_edit",
    "mode": "edit-isolated",
    "task_hash": "..."
  },
  "backend": {
    "name": "codex_cli",
    "kind": "external_agent",
    "data_boundary": "hosted_provider",
    "execution_location": "mixed",
    "billing_mode": "subscription"
  },
  "approval": {
    "approval_id": "...",
    "profile": "hosted_provider_codex",
    "expires_at": "..."
  },
  "workspace": {
    "kind": "isolated_copy",
    "base_commit": "...",
    "workspace_manifest_sha256": "..."
  },
  "docker": {
    "used": true,
    "image": "agent-harness-test:local",
    "image_digest": "...",
    "network": "none"
  },
  "artifacts": [
    {
      "kind": "patch",
      "path": "diff.patch",
      "sha256": "..."
    }
  ]
}
```

## 16. Security Plan

Add formal security documentation:

```text
SECURITY.md
docs/threat-model.md
docs/data-boundaries.md
docs/approval-model.md
docs/docker-sandbox.md
docs/codex-supervision.md
docs/non-goals.md
```

The security model should state:

- Single-operator local trust model.
- The harness protects the active repo from backend/tool actions.
- Codex is an external supervised agent, not an introspected model.
- Approval profiles are security boundaries.
- Patch apply-back is the mutation boundary.
- Docker sandboxing reduces risk but is not perfect containment.
- The harness does not provide multi-tenant security.
- Prompt injection is a vulnerability only when it bypasses policy, approval, sandbox, or artifact boundaries.

Add redaction for:

- Terminal display.
- `events.jsonl`.
- `transcript.jsonl`.
- `final_report.md`.
- Backend stdout/stderr.
- Test stdout/stderr.

Patch artifacts should not be silently mutated by redaction. If a patch appears to contain secrets, the harness should warn or block according to policy.

## 17. Docker Sandbox Profiles

Introduce named sandbox profiles.

```text
strict
  network: none
  env passthrough: none
  non-root required
  no writable source mount
  timeout required
  memory/cpu limits required

default
  network: none
  env passthrough: none
  non-privileged
  sanitized temp workspace

networked
  requires explicit approval
  network enabled
  recorded as elevated boundary

custom-image
  requires Dockerfile validation
  image digest recorded
```

Commands:

```bash
harness tests profiles
harness tests run --profile strict -- python -m pytest -q
```

## 18. Compare and Baseline Layer

Add local run comparison.

Commands:

```bash
harness compare <run_a> <run_b>
harness baseline set <run_id> --name local-green
harness baseline compare <run_id> --baseline local-green
```

Compare:

- Run status.
- Backend boundary.
- Approval profile.
- Files changed.
- Patch size.
- Blocked path attempts.
- Test status.
- Test duration.
- Docker image.
- Network mode.
- Secret scan result.
- Artifact checksums.

## 19. Testing Strategy

The repo already has strong regression coverage. The next step is golden end-to-end tests.

Fixture projects:

```text
tests/fixtures/simple_python_project/
tests/fixtures/dirty_repo_project/
tests/fixtures/secret_paths_project/
tests/fixtures/docker_test_project/
tests/fixtures/codex_mock_project/
```

Golden flows:

```text
init -> read_only_repo_summary -> inspect artifacts
init -> simple_code_edit with fake backend -> patch -> run_tests -> report
init -> codex_code_edit with fake Codex subprocess -> isolated diff -> deny apply -> unchanged repo
init -> codex_code_edit with fake Codex subprocess -> approve apply -> changed repo
init -> docker test denied -> no docker invocation
init -> docker test approved -> stdout/stderr/result artifacts
```

Acceptance criteria:

- Golden tests run in CI.
- Golden tests assert artifact names.
- Golden tests verify active repo mutation boundaries.
- Golden tests verify event order.
- Golden tests verify manifest contents.

## 20. Immediate PR Sequence

### PR 1 — RunMode and policy matrix

- Add `RunMode` enum.
- Map task routes to allowed modes.
- Map tools to allowed modes.
- Add tests for forbidden combinations.

### PR 2 — Backend descriptors

- Add `BackendDescriptor` model.
- Make Codex and local-compatible backends emit descriptors.
- Record descriptor snapshots on runs/manifests.
- Add `harness backends preflight --json`.

### PR 3 — Artifact contract and manifest

- Add `ArtifactKind` enum.
- Add artifact `sha256`, `size`, `producer`, `schema_version`.
- Write `manifest.json` for every run.
- Add manifest tests.

### PR 4 — JSON/JSONL CLI

- Add `--json` to runs, show, artifacts, approvals, backends.
- Add `events --jsonl`.
- Add snapshot tests for JSON output.

### PR 5 — Security docs and redaction

- Add `SECURITY.md`.
- Add threat model docs.
- Add display/artifact redaction layer.
- Add redaction tests.

### PR 6 — Docker sandbox profiles

- Add sandbox profile model.
- Add strict/default/networked profiles.
- Record profile in test result and manifest.
- Require approval for networked mode.

### PR 7 — AgentSpec and WorkbenchSpec

- Add `WorkbenchSpec`.
- Add hierarchical `AgentNode`.
- Add inheritance and effective-agent resolution.
- Add starter workbenches and agents.

### PR 8 — Model profiles

- Add `ModelProfile`.
- Add local model profile support.
- Add Codex supervised profile.
- Add route policy.

### PR 9 — Task queue

- Add `ObjectiveSpec` and `TaskSpec`.
- Add task records to SQLite.
- Add `tasks add/list/inspect/run-next`.

### PR 10 — Compare and baseline

- Add compare command.
- Add local baseline pointer table.
- Add baseline compare.
- Add JSON output.

### PR 11 — Golden E2E suite

- Add fixture repos.
- Add fake Codex subprocess/backend.
- Add golden flow assertions.
- Run in CI.

### PR 12 — Local daemon

- Add daemon process.
- Add heartbeat events.
- Add stop/status commands.
- Enforce approval gates.
- Start with single-task execution.

## 21. Local Testing Plan on Current Hardware

Current device: 8 GB RAM M1 MacBook.

Use the machine for:

- Architecture testing.
- CLI behavior.
- Persistence.
- Safety boundaries.
- Mock backend tests.
- Small local model tests.
- Toy fixture edits.
- Codex supervised isolated edits.

Avoid using the current machine for:

- Heavy quant backtesting.
- Large local models.
- Long-running multi-agent swarms.
- Large parallel Docker workloads.

Recommended local testing layers:

1. Mock backend tests.

```bash
pytest -q
```

2. Small local model route tests.

```bash
harness run \
  --task-type read_only_repo_summary \
  --backend local_compatible \
  --project .
```

3. Codex subscription tests.

```bash
harness approvals add \
  --backend codex_cli \
  --data-boundary hosted_provider \
  --project . \
  --task-types codex_code_edit \
  --duration-days 1

harness run \
  --task-type codex_code_edit \
  --project . \
  --task-file tasks/small_fix.md
```

Use Codex only for isolated edit runs. Keep tasks small, deterministic, and reviewed.

## 22. Non-Goals Until After v0.7

Do not add these yet:

- MCP.
- Plugin marketplace.
- Browser control.
- Email/calendar integration.
- Generic shell tool.
- Hosted API fallback.
- OpenAI API fallback.
- Multi-user server.
- Web dashboard.
- TUI.
- Workflow DAG engine before task queue is stable.
- Persistent personal memory before memory scopes are stable.
- Unlimited subagents.
- Live trading or broker actions.
- Automatic job application sending.

## 23. Near-Term Definition of Done

The next milestone is complete when a new user can:

1. Install `agent-harness`.
2. Initialize a local repo.
3. Run an inspect task.
4. Run a supervised Codex isolated edit.
5. Inspect the diff.
6. Approve or deny apply-back.
7. Run Docker tests.
8. Inspect a complete manifest/report.
9. See every approval, backend boundary, artifact checksum, and safety decision recorded locally.

## 24. Long-Term Definition of Done

The long-term system is complete when a dedicated local device can run a supervised 24/7 autonomous team of specialized agents that can:

- Maintain a local task queue.
- Run local models by default.
- Escalate to Codex only under explicit hosted-boundary approval.
- Operate separate quant, coding, personal, writing, and research workbenches.
- Use hierarchical specialized agents.
- Delegate work through an orchestrator.
- Conduct bounded swarm sessions.
- Produce durable artifacts and reports.
- Pause at approval gates.
- Keep memory separated by domain.
- Never silently send external messages, submit applications, mutate active repos, or take financial actions.

The system should become autonomous in planning and artifact production, not autonomous in sensitive real-world action.
