# Agent Harness Master Plan

Imported from `/Users/oscarxue/Downloads/agent_harness_master_plan.md` as the repo-local planning reference. This roadmap snapshot preserves strategic direction and safety boundaries, but it is not blanket implementation approval for every listed item.

This version incorporates the useful parts of the edited plan at `/Users/oscarxue/Documents/plan_edit.md` and the research report at `/Users/oscarxue/Downloads/deep-research-report(1).md`: explicit control-plane/execution-plane separation, durable task state, typed tool contracts, idempotency, traceability, sandbox tiers, eval gates, and the rule that external interoperability protocols remain adapters rather than the harness control plane.

## 1. Product Definition

`agent-harness` is a local-first supervised agent runtime for controlled work on local projects. It records runs, artifacts, approvals, backend metadata, data-boundary decisions, isolated edits, test results, and safety events.

The near-term system is a safety-focused local runner. The long-term system is a 24/7 local autonomous team of specialized agents for quant finance, coding, research, job applications, writing, and other personal work.

The central rule is that agents may propose, plan, delegate, critique, edit in isolation, and produce artifacts. The harness decides what they are allowed to do.

## 2. Current State

The current repository has moved beyond the original v0.1 snapshot. v0.1 hardening is complete, v0.2 declarative specs are in progress, and the project is entering v0.3 manual task-queue work.

The important conclusion from the edited plan and research report is that the project should not rush from a simple queue into daemon autonomy. v0.3 must become the durable control-plane substrate for future daemon execution, workbenches, bounded sessions, and long-running supervised workflows.

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
- Run modes, backend descriptors, run manifests, stable JSON inspection output, `harness doctor`, and golden v0.1 evidence tests.
- Declarative v0.2-style registry/spec primitives for workbenches, agents, model profiles, tool policies, and memory scopes.
- A manual persistent task queue with task creation, listing, inspection, status movement, and safe `run-next` selection that does not execute agents or create background work.

This should not be treated as a throwaway prototype. It is the security and evidence substrate for the later autonomous system.

Near-term planning implication: preserve the existing safety kernel, retrofit the remaining policy/evidence gaps, and harden task state before building the daemon.

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
- A framework-driven swarm where agents negotiate permissions among themselves.
- A system whose internal safety model is delegated to MCP, A2A, AGENTS.md, or any other external protocol.

## 4. Core Design Principles

1. **Local-first by default.** Local/private data should stay local unless an explicit hosted-boundary approval exists.

2. **Evidence over chat.** Every important action should produce events, artifacts, manifests, and reports.

3. **Supervised mutation.** Agents may edit isolated workspaces, but active repositories are modified only through validated, approved apply-back.

4. **No hidden fallbacks.** No hosted, paid, or networked fallback should occur without explicit policy and approval.

5. **Agent permissions are declarative.** Agents do not decide what tools they can use. Workbench, agent, and task policies define that.

6. **Hierarchy must not weaken safety.** Child agents may narrow permissions but cannot override forbidden workbench-level boundaries.

7. **Orchestration is above the harness, not beside it.** Orchestrators create and delegate tasks through the same persistence, approval, artifact, and policy system.

8. **Autonomy is bounded.** A 24/7 daemon may run safe tasks automatically, but sensitive actions must pause at approval gates.

9. **Control plane owns authority.** Policy, approvals, manifests, task state, artifact registration, and apply-back boundaries belong to the harness core. Execution runners, model routers, external agent backends, and protocol adapters operate under that authority.

10. **Durability before autonomy.** Long-running workflows require explicit task attempts, leases, state transitions, checkpoints, idempotency keys, and replay policy before daemon execution is allowed.

11. **Typed tools, not ambient power.** Every tool must have a typed capability descriptor that declares schemas, side effects, boundaries, allowed run modes, approval requirements, sandbox requirements, and replay/idempotency behavior.

12. **Interop is adapter-only.** AGENTS.md may provide read-only repo context. MCP may later expose tools through the typed tool gateway. A2A may later model remote agents as external backends. None of these may grant permissions, bypass approvals, or become the internal control plane.

## 5. Target Architecture

```text
agent-harness control plane
  policy engine
  approval service
  runs
  events
  artifacts
  artifact registry
  task/objective registry
  manifest writer
  approvals
  backend descriptors
  model profiles
  tool policies
  tool capability registry
  memory scopes
  sandbox profile registry
  JSON/JSONL API
  observability exporter
  compare/baseline engine

agent-harness execution plane
  manual scheduler
  daemon scheduler
  orchestrator runner
  specialist agent runner
  model router
  typed tool gateway
  isolation manager
  Docker sandbox
  sandbox runner
  test runner
  secret scanner
  patch/diff engine

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
  task attempts
  task leases
  checkpoints
  delegation records
  review gates
  approval gates
  bounded sessions

daemon layer
  local scheduler
  task queue runner
  leases
  resource limits
  heartbeat events
  approval blocking
  crash recovery
  resumability
  artifact generation
```

The control plane owns policy and evidence. The execution plane performs bounded actions under control-plane authority.

## 6. Version Roadmap

### v0.1 — Safety Kernel

Status: complete baseline; keep hardening as compatibility-preserving retrofits.

The focus is local project state, persistence, approvals, Codex supervision, isolated editing, patch apply-back, Docker tests, and safety primitives.

Completed v0.1 hardening includes:

- Add `RunMode` enum.
- Add `BackendDescriptor` contract.
- Add run `manifest.json` for every run.
- Add stable JSON/JSONL CLI output.
- Add `SECURITY.md` and formal threat model.
- Add `harness doctor`.
- Add golden end-to-end tests.

Required v0.1-compatible retrofits before v0.3 hardens:

- Add `EffectivePolicy` snapshots for runs, tasks, tools, agents, and backends.
- Make artifact records immutable by contract with `schema_version`, `sha256`, `size_bytes`, `producer`, redaction state, and derived-artifact lineage.
- Upgrade manifests to include `task_id`, `objective_id`, `trace_id`, effective-policy hash, backend descriptor hash, sandbox profile, artifact checksums, and validation results.
- Add an observability spine with local JSONL events as source of truth and OpenTelemetry-compatible IDs/fields for later export.
- Ensure redaction creates derived artifacts or blocks unsafe registration; it must not silently mutate evidence such as patch artifacts.

### v0.2 — Agent Specs and Model Profiles

Status: in progress. Introduce generic agent definitions and model routing without autonomy.

Add:

- `AgentNode`, replacing loose `AgentSpec` as the hierarchical agent model.
- `WorkbenchSpec`.
- `ModelProfile`.
- `ToolPolicy`.
- `ToolCapabilityDescriptor`.
- `MemoryScope`.
- Agent registry.
- Built-in starter agents.
- `harness agents explain <agent_id> --json`.
- `harness workbenches explain <workbench_id> --json`.
- AGENTS.md ingestion as read-only repo context.

Starter agents:

- `repo_inspector`.
- `code_editor`.
- `test_runner`.
- `quant_researcher`.
- `job_researcher`.

Rules:

- Agent/workbench specs are declarative and do not execute work.
- Effective-agent resolution must show inherited tools, model profile, memory scope, output contracts, and effective policy.
- Model profiles must declare boundary, budget, fallback behavior, approval requirements, isolation requirements, timeout, and supported capabilities.
- `fallback: none` means hard fail.
- Fallback across local/hosted, offline/networked, free/paid, or read/write boundaries requires policy permission and approval.
- AGENTS.md can provide instructions and context only. It cannot grant tools, authorize network, authorize Codex, authorize Docker, authorize active repo mutation, or override policy.
- Memory scopes stay metadata-only until artifact-backed memory and redaction policy are stable.

### v0.3 — Task Queue and Manual Scheduler

Status: current focus.

Add persistent objectives, tasks, dependencies, task attempts, leases, deterministic state transitions, expected outputs, run linkage, and manual scheduling before a daemon.

The task queue is not just a to-do list. It is the future daemon substrate.

Commands:

```bash
harness objectives add tasks/objective.md --workbench coding
harness objectives list
harness objectives inspect <objective_id>
harness tasks add tasks/idea.md --agent quant_researcher
harness tasks list
harness tasks inspect <task_id>
harness tasks run-next
harness tasks run <task_id>
harness tasks cancel <task_id>
harness tasks retry <task_id>
harness tasks graph <objective_id> --json
```

Required task states:

```text
created
ready
blocked
waiting_approval
leased
running
succeeded
failed
cancelled
skipped
```

Required v0.3 behavior:

- A task cannot run until dependencies are satisfied.
- Dependency cycles are rejected.
- `run-next` selects only ready tasks whose policy allows execution or whose approvals already exist.
- `run-next` creates a lease before execution work is added.
- Every task attempt links to a run when execution exists.
- Every task has an `idempotency_key`.
- Required output contracts must be registered as artifacts before a task can succeed.
- Invalid state transitions emit `task_transition_denied`.
- Approval-required tasks pause instead of failing silently.
- Duplicate task execution is prevented by leases and idempotency.

Failure modes to test:

- Dependency cycle.
- Missing required artifact.
- Task selected without policy resolution.
- Task selected without required approval.
- Duplicate `run-next`.
- Retry after partial output.
- Agent not registered.
- Model profile unavailable.
- Backend descriptor missing.
- Task references forbidden tool.
- Task asks for active repo write in plan mode.

### v0.3.5 — Control-Plane Stabilization

Add this milestone before v0.4. The daemon should not be built on a weak manual scheduler.

Add:

- `harness compare <run_a> <run_b>`.
- `harness baseline set <run_id> --name local-green`.
- `harness baseline compare <run_id> --baseline local-green`.
- `harness evals run --suite safety-smoke`.
- `harness traces export <run_id> --format otel-json`.
- Policy regression suite.
- Sandbox regression suite.
- Task replay tests.

Exit criteria:

- Baseline compare detects changed policy, backend boundary, sandbox profile, artifact checksums, and test result.
- Safety-smoke evals block release on safety regression.
- Trace export links run, task, agent, backend, tool, sandbox, approval, and artifact spans.

### v0.4 — Local Daemon

Add conservative local 24/7 operation. Implement the daemon as a scheduler over durable task state, not as an agent loop.

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
- Acquire and renew leases.
- Recover safely after process kill, backend timeout, Docker timeout, SQLite lock contention, expired approval, expired lease, or artifact-write crash.

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

Multi-agent means artifact-producing task graph, not free-form group chat. Only add multi-agent workflows after v0.3/v0.4 durability is stable.

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

### v0.8 — Bounded Sessions

Introduce bounded multi-agent debate and parallel review sessions. Internally prefer `BoundedSession` over `swarm` terminology because the important property is enforced limits and artifact output.

Bounded sessions must have:

- Moderator/orchestrator.
- Fixed participants.
- Maximum rounds.
- Maximum runtime.
- Maximum parallel tasks.
- Explicit output contracts.
- Persisted artifacts.
- No hidden recursive delegation.
- Termination condition.
- Artifact requirements.

### v0.9 — Tool Adapter Layer

Add external protocol adapters only after native policy, task, and tool contracts are stable.

Adapter boundaries:

- Native harness tools.
- AGENTS.md read-only context adapter.
- MCP read-only adapter.
- MCP sandboxed adapter.
- A2A remote-agent adapter.

MCP rules:

- MCP servers run sandboxed.
- MCP tools receive least privilege.
- MCP cannot grant harness permissions.
- MCP descriptors are translated into `ToolCapabilityDescriptor`.
- Networked MCP requires approval.

A2A rules:

- A2A is only for independently deployed remote agents.
- A2A agents are treated as external backends.
- A2A cannot receive repo data without data-boundary approval.
- A2A outputs are artifacts requiring validation.

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
- Bounded sessions.
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

## 11. Bounded Session Design

Bounded session behavior should be artifact-driven and moderated by an orchestrator.

A bounded session is not an infinite group chat. It is a controlled multi-agent work session with a fixed purpose.

Example:

```yaml
id: quant_signal_review_session
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

Bounded session output must collapse into artifacts:

- `consensus_summary.md`.
- `disagreements.json`.
- `recommended_next_tasks.json`.
- `final_orchestrator_decision.md`.

## 12. Core Models to Add

### 12.1 Run, policy, artifact, and backend models

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

```python
class PolicyLevel(str, Enum):
    FORBIDDEN = "forbidden"
    APPROVAL_REQUIRED = "approval_required"
    ALLOWED = "allowed"
```

```python
class BoundaryKind(str, Enum):
    LOCAL_ONLY = "local_only"
    HOSTED_PROVIDER = "hosted_provider"
    NETWORKED = "networked"
    ACTIVE_REPO = "active_repo"
    FILESYSTEM = "filesystem"
    PERSONAL_DATA = "personal_data"
    FINANCIAL_ACTION = "financial_action"
    EXTERNAL_MESSAGE = "external_message"
```

```python
class EffectivePolicy(BaseModel):
    schema_version: str = "harness.effective_policy/v1"
    subject_id: str
    subject_kind: Literal["run", "task", "agent", "tool", "backend"]
    resolved_at: datetime
    levels: dict[str, PolicyLevel]
    sources: list[PolicySource]
    required_approvals: list[str]
    forbidden_reasons: list[str] = []
    monotonicity_checked: bool
```

```python
class ArtifactRecord(BaseModel):
    schema_version: str = "harness.artifact/v1"
    artifact_id: str
    run_id: str
    task_id: str | None = None
    kind: ArtifactKind
    path: str
    size_bytes: int
    sha256: str
    producer: str
    created_at: datetime
    content_type: str | None = None
    redaction_state: Literal["raw", "redacted_copy", "blocked_secret"]
    parent_artifact_ids: list[str] = []
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
    schema_version: str = "harness.agent_node/v1"
    id: str
    parent_id: str | None = None
    workbench_id: str
    kind: Literal["group", "agent", "specialist", "orchestrator", "reviewer"]
    role: str
    instructions_ref: str | None = None
    model_profile: str | None = None
    memory_scope: str | None = None
    allowed_tools: list[str] | None = None
    permissions: dict[str, PolicyLevel] | None = None
    approval_policy: dict[str, PolicyLevel] | None = None
    output_contracts: list[str] = []
    limits: AgentLimits = AgentLimits()
    tags: list[str] = []
```

```python
class ModelProfile(BaseModel):
    schema_version: str = "harness.model_profile/v1"
    id: str
    backend_id: str
    model: str
    boundary: BoundaryKind
    requires_approval: list[str] = []
    requires_isolation: bool = False
    network_allowed: bool = False
    fallback: str | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_cost_usd: Decimal | None = None
    timeout_seconds: int
    supports_tools: bool
    supports_json_output: bool
    supports_streaming: bool
    local_only: bool
```

```python
class ToolCapabilityDescriptor(BaseModel):
    schema_version: str = "harness.tool/v1"
    id: str
    description: str
    input_schema_ref: str
    output_schema_ref: str
    side_effect_level: Literal["none", "artifact_write", "workspace_write", "active_repo_write", "external"]
    boundaries: list[BoundaryKind]
    allowed_modes: list[RunMode]
    approval_required: list[str] = []
    idempotency: Literal["pure", "idempotent", "non_idempotent"]
    replay_policy: Literal["replay_from_record", "rerun_allowed", "rerun_forbidden"]
    sandbox_required: bool
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
class TaskStatus(str, Enum):
    CREATED = "created"
    READY = "ready"
    BLOCKED = "blocked"
    WAITING_APPROVAL = "waiting_approval"
    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
```

```python
class TaskSpec(BaseModel):
    schema_version: str = "harness.task/v1"
    id: str
    objective_id: str | None = None
    workbench_id: str
    agent_id: str
    title: str
    instruction: str
    inputs: list[str] = []
    expected_outputs: list[str] = []
    mode: RunMode
    status: TaskStatus
    priority: int = 0
    idempotency_key: str
    max_attempts: int = 1
    timeout_seconds: int | None = None
    required_approvals: list[str] = []
    created_at: datetime
    updated_at: datetime
```

```python
class TaskAttempt(BaseModel):
    schema_version: str = "harness.task_attempt/v1"
    id: str
    task_id: str
    run_id: str | None = None
    attempt_number: int
    status: TaskStatus
    lease_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    failure_code: str | None = None
    failure_message: str | None = None
```

```python
class TaskLease(BaseModel):
    schema_version: str = "harness.task_lease/v1"
    lease_id: str
    task_id: str
    owner: str
    acquired_at: datetime
    expires_at: datetime
    heartbeat_at: datetime | None = None
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

Every run should write `manifest.json`. The current `harness.manifest/v1` contract should be upgraded compatibly toward `harness.manifest/v1.1` before daemon work.

Example:

```json
{
  "schema_version": "harness.manifest/v1.1",
  "run_id": "run_...",
  "task_id": "task_...",
  "objective_id": "obj_...",
  "trace_id": "trace_...",
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
    "task_hash": "...",
    "idempotency_key": "...",
    "attempt": 1
  },
  "agent": {
    "agent_id": "...",
    "workbench_id": "...",
    "effective_policy_sha256": "..."
  },
  "backend": {
    "descriptor_sha256": "...",
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
    "profile": "strict",
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
  ],
  "checks": {
    "path_policy": "passed",
    "secret_scan": "passed",
    "tests": "passed",
    "policy_validation": "passed"
  }
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

The edited plan is worth adopting, but as a reordered implementation sequence rather than a wholesale rewrite. Since v0.1 hardening exists and v0.3 has started, the next PRs should retrofit missing control-plane contracts while continuing task-queue work.

### PR A — Effective Policy and Manifest Retrofit

- Add `PolicyLevel`.
- Add `EffectivePolicy`.
- Add deterministic policy resolution tests.
- Upgrade manifest toward `v1.1`.
- Add `trace_id`, `task_id`, and `objective_id` fields where available.
- Add `policy explain` JSON output.

### PR B — Artifact and JSON Contract Retrofit

- Add or upgrade `ArtifactRecord`.
- Add artifact `sha256`, `size_bytes`, `producer`, `schema_version`, and redaction state.
- Enforce immutable registered artifacts.
- Add JSON/JSONL output for runs, events, artifacts, approvals, policy, and doctor flows.
- Add snapshot tests.

### PR C — Agent, Workbench, and Model Resolution

- Stabilize `AgentNode`.
- Stabilize `WorkbenchSpec`.
- Stabilize `ModelProfile`.
- Add effective-agent resolution.
- Add `agents explain` and `workbenches explain`.
- Add fallback-boundary tests.
- Add read-only AGENTS.md ingestion.

### PR D — Tool Capability Gateway

- Add `ToolCapabilityDescriptor`.
- Wrap initial tools: `repo_read`, `artifact_read`, `artifact_write`, `isolated_edit`, `diff_inspect`, `secret_scan`, `docker_test`, `policy_explain`, and `approval_request`.
- Add side-effect level, idempotency, replay policy, allowed modes, and sandbox requirements.
- Add tool-policy tests.

### PR E — Task Schema and State Machine

- Add or upgrade `ObjectiveSpec`.
- Add or upgrade `TaskSpec`.
- Add `TaskAttempt`.
- Add `TaskDependency`.
- Add `TaskLease`.
- Add deterministic task transition validation.
- Add SQLite migrations.

### PR F — Task CLI Completion

- Add `objectives add/list/inspect`.
- Add `tasks add/list/inspect`.
- Add `tasks graph --json`.
- Add `tasks cancel`.
- Add `tasks retry`.
- Preserve the invariant that task inspection commands do not execute agents, preflight backends, run Docker, or start background work.

### PR G — Manual Scheduler Hardening

- Harden `run-next`.
- Add dependency satisfaction checks.
- Add lease acquisition.
- Add policy gate.
- Add approval gate.
- Add task attempt creation.
- Bind task attempts to runs once execution is introduced.

### PR H — Task Artifacts and Output Contracts

- Add expected-output validation.
- Register task artifacts.
- Fail tasks when required artifacts are missing.
- Record output-contract status in manifest/report evidence.

### PR I — Failure and Replay Tests

- Add duplicate `run-next` test.
- Add blocked dependency test.
- Add approval-required pause test.
- Add crash-after-artifact test.
- Add retry idempotency test.
- Add missing required artifact test.
- Add invalid transition test.

### PR J — Compare, Baseline, Evals, and Traces

- Add compare.
- Add baseline set/compare.
- Add safety-smoke eval suite.
- Add local OTEL-compatible trace export.
- Add regression gates for policy, manifest, artifacts, sandbox, and apply-back.

### PR K — Minimal Local Daemon

- Add daemon process after v0.3.5 exit criteria pass.
- Add heartbeat.
- Add task lease renewal.
- Add approval blocking.
- Add crash recovery.
- Start with one task at a time.

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
- Long-running multi-agent bounded sessions.
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

## 22. Non-Goals Until Native Control-Plane Contracts Are Stable

Do not add these yet:

- MCP as an internal architecture.
- A2A.
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

Read-only AGENTS.md ingestion is allowed earlier because it is repo context, not a permission source. MCP may be considered later only as a tool-adapter boundary after `ToolCapabilityDescriptor`, sandbox policy, approval gates, and task evidence are stable.

## 23. Near-Term Definition of Done

The next milestone is complete when a new user can:

1. Install `agent-harness`.
2. Initialize a local repo.
3. Inspect effective policy.
4. Run an inspect task.
5. Create an objective.
6. Create tasks with dependencies.
7. Select safe work with `run-next`.
8. Pause cleanly on approval-required work.
9. Run a supervised Codex isolated edit.
10. Inspect the diff.
11. Approve or deny apply-back.
12. Run Docker tests with a sandbox profile.
13. Inspect a complete manifest/report/events/artifacts set.
14. Compare a run to a baseline.
15. See every approval, backend boundary, artifact checksum, task transition, and safety decision recorded locally.

## 24. Long-Term Definition of Done

The long-term system is complete when a dedicated local device can run a supervised 24/7 autonomous team of specialized agents that can:

- Maintain a local task queue.
- Run local models by default.
- Escalate to Codex only under explicit hosted-boundary approval.
- Operate separate quant, coding, personal, writing, and research workbenches.
- Use hierarchical specialized agents.
- Delegate work through an orchestrator.
- Conduct bounded sessions.
- Produce durable artifacts and reports.
- Pause at approval gates.
- Keep memory separated by domain.
- Never silently send external messages, submit applications, mutate active repos, or take financial actions.

The system should become autonomous in planning and artifact production, not autonomous in sensitive real-world action.

Autonomy is allowed to grow in planning, artifact production, review, and safe local execution. It must not grow into irreversible real-world action without a separate future plan and explicit approval model.
