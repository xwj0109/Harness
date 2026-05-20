from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path

from harness.models import (
    AgentLane,
    ArtifactRecord,
    GraphEdge,
    GraphEvent,
    GraphNode,
    LiveOrchestrationGraph,
    ObjectiveRecord,
    ObjectiveStatus,
    OrchestrationInstance,
    RunEventType,
    RunRecord,
    TaskAttempt,
    TaskDependency,
    TaskDependencyType,
    TaskLease,
    TaskLeaseStatus,
    TaskRecord,
    TaskStatus,
)
from harness.orchestration_layout import GraphPositionMap, assign_stable_graph_layout
from harness.orchestration_state import OrchestrationStateSnapshot
from harness.orchestration_validate import display_artifact_path, validate_live_orchestration_graph
from harness.security import sanitize_for_logging


TERMINAL_TASK_STATUSES = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.SKIPPED}


def project_orchestration_graphs(
    snapshot: OrchestrationStateSnapshot,
    *,
    selected_node_by_orchestration: dict[str, str | None] | None = None,
    previous_positions_by_orchestration: dict[str, GraphPositionMap] | None = None,
) -> tuple[list[OrchestrationInstance], list[LiveOrchestrationGraph]]:
    selected_node_by_orchestration = selected_node_by_orchestration or {}
    previous_positions_by_orchestration = previous_positions_by_orchestration or {}
    instances: list[OrchestrationInstance] = []
    graphs: list[LiveOrchestrationGraph] = []
    for objective in sorted(snapshot.objectives, key=lambda item: item.updated_at, reverse=True):
        instance = project_orchestration_instance(snapshot, objective)
        graph = project_live_orchestration_graph(
            snapshot,
            objective,
            selected_node_id=selected_node_by_orchestration.get(instance.orchestration_id),
            previous_positions=previous_positions_by_orchestration.get(instance.orchestration_id),
        )
        instances.append(instance)
        graphs.append(graph)
    return instances, graphs


def project_orchestration_instance(
    snapshot: OrchestrationStateSnapshot,
    objective: ObjectiveRecord,
) -> OrchestrationInstance:
    tasks = _objective_tasks(snapshot, objective.id)
    agents = _assigned_agents(tasks)
    active_task = _active_task(tasks, snapshot.leases)
    attention_task = _attention_task(tasks)
    return OrchestrationInstance(
        orchestration_id=objective.id,
        objective_id=objective.id,
        title=str(sanitize_for_logging(objective.title)),
        state=_objective_state(objective, tasks, snapshot.leases),
        assigned_workbench=objective.workbench_id or _first_string(task.workbench_id for task in tasks),
        assigned_agents=agents,
        active_task_id=active_task.id if active_task is not None else None,
        attention_task_id=attention_task.id if attention_task is not None else None,
        last_event_seq=_last_event_seq(snapshot, objective.id),
        updated_at=_latest_updated_at(objective, tasks),
    )


def project_live_orchestration_graph(
    snapshot: OrchestrationStateSnapshot,
    objective: ObjectiveRecord,
    *,
    selected_node_id: str | None = None,
    previous_positions: GraphPositionMap | None = None,
) -> LiveOrchestrationGraph:
    tasks = _objective_tasks(snapshot, objective.id)
    task_by_id = {task.id: task for task in tasks}
    latest_attempt_by_task = _latest_attempts_by_task(snapshot.attempts)
    latest_lease_by_task = _latest_leases_by_task(snapshot.leases)
    runs_by_id = {run.id: run for run in snapshot.runs}
    runs_by_task = _runs_by_task(snapshot.runs)
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    active_nodes: list[str] = []
    attention_nodes: list[str] = []

    objective_node_id = _objective_node_id(objective.id)
    nodes.append(
        GraphNode(
            id=objective_node_id,
            kind="objective",
            title=str(sanitize_for_logging(objective.title)),
            state=_objective_graph_state(objective, tasks, snapshot.leases),
            entity_id=objective.id,
            entity_kind="objective",
            lane_id="orchestrator",
            active=bool(tasks),
            symbol=_symbol_for_state(_objective_graph_state(objective, tasks, snapshot.leases)),
            detail_rows=[
                f"Objective: {objective.title}",
                f"State: {_objective_graph_state(objective, tasks, snapshot.leases)}",
                f"Command: harness progress --objective {objective.id} --project {snapshot.project_root} --output json",
            ],
        )
    )

    for task in _task_order(tasks, snapshot.dependencies):
        task_node_id = _task_node_id(task.id)
        task_state = _task_graph_state(task, latest_lease_by_task.get(task.id), latest_attempt_by_task.get(task.id))
        active = task_state == "running"
        attention = task_state in {"blocked", "failed"}
        if active:
            active_nodes.append(task_node_id)
        if attention:
            attention_nodes.append(task_node_id)
        nodes.append(
            GraphNode(
                id=task_node_id,
                kind="task",
                title=str(sanitize_for_logging(task.title)),
                state=task_state,
                entity_id=task.id,
                entity_kind="task",
                lane_id=_lane_id_for_task(task),
                active=active,
                attention=attention,
                symbol=_symbol_for_state(task_state),
                detail_rows=_task_detail_rows(snapshot.project_root, task, latest_lease_by_task.get(task.id), latest_attempt_by_task.get(task.id)),
                metadata={
                    "agent_id": task.agent_id,
                    "workbench_id": task.workbench_id,
                    "execution_adapter": task.metadata.get("execution_adapter"),
                    "task_type": task.metadata.get("task_type"),
                },
            )
        )
        edges.append(_edge("contains", objective_node_id, task_node_id))
        for gate_node in _approval_gate_nodes(task):
            nodes.append(gate_node)
            attention_nodes.append(gate_node.id)
            edges.append(_edge("requires_approval", gate_node.id, task_node_id))
        for blocker_node in _blocker_nodes(task):
            nodes.append(blocker_node)
            attention_nodes.append(blocker_node.id)
            edges.append(_edge("blocked_by", blocker_node.id, task_node_id))

        run = _task_run(task, latest_attempt_by_task.get(task.id), runs_by_id, runs_by_task)
        if run is not None:
            run_node_id = _run_node_id(run.id)
            run_state = _run_graph_state(run)
            if run_state == "running":
                active_nodes.append(run_node_id)
            if run_state == "failed":
                attention_nodes.append(run_node_id)
            nodes.append(
                GraphNode(
                    id=run_node_id,
                    kind="adapter_run",
                    title=_run_title(task, run),
                    state=run_state,
                    entity_id=run.id,
                    entity_kind="run",
                    lane_id=_lane_id_for_task(task),
                    active=run_state == "running",
                    attention=run_state == "failed",
                    symbol=_symbol_for_state(run_state),
                    detail_rows=[
                        f"Run: {run.id}",
                        f"Status: {run.status}",
                        f"Adapter: {task.metadata.get('execution_adapter') or 'unknown'}",
                        f"Command: harness runs --project {snapshot.project_root}",
                    ],
                )
            )
            edges.append(_edge("dispatches", task_node_id, run_node_id))
            _append_artifact_and_verification_nodes(
                snapshot=snapshot,
                run=run,
                task=task,
                run_node_id=run_node_id,
                nodes=nodes,
                edges=edges,
                active_nodes=active_nodes,
                attention_nodes=attention_nodes,
            )

    _append_dependency_edges(snapshot.dependencies, task_by_id, edges)
    _append_artifact_handoff_edges(snapshot, task_by_id, latest_attempt_by_task, runs_by_id, runs_by_task, edges)
    lanes = _lanes_for_graph(nodes, tasks)
    graph = LiveOrchestrationGraph(
        orchestration_id=objective.id,
        revision=max(1, _last_event_seq(snapshot, objective.id)),
        nodes=_dedupe_nodes(nodes),
        edges=_dedupe_edges(edges),
        lanes=lanes,
        active_node_ids=_dedupe(active_nodes),
        attention_node_ids=_dedupe(attention_nodes),
        timeline_tail=_timeline_tail(snapshot, objective.id, runs_by_task, tasks),
    )
    selected = selected_node_id if selected_node_id in {node.id for node in graph.nodes} else None
    if selected is None:
        selected = (graph.attention_node_ids or graph.active_node_ids or [graph.nodes[0].id if graph.nodes else None])[0]
    graph = graph.model_copy(update={"selected_node_id": selected})
    graph = assign_stable_graph_layout(validate_live_orchestration_graph(graph), previous_positions=previous_positions)
    return graph


def _objective_tasks(snapshot: OrchestrationStateSnapshot, objective_id: str) -> list[TaskRecord]:
    return [task for task in snapshot.tasks if task.objective_id == objective_id]


def _assigned_agents(tasks: list[TaskRecord]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for task in tasks:
        agent = task.agent_id or task.workbench_id
        if agent and agent not in seen:
            seen.add(agent)
            result.append(str(agent))
    return result


def _active_task(tasks: list[TaskRecord], leases: list[TaskLease]) -> TaskRecord | None:
    active_task_ids = {lease.task_id for lease in leases if lease.status == TaskLeaseStatus.ACTIVE}
    for task in tasks:
        if task.id in active_task_ids or task.status in {TaskStatus.RUNNING, TaskStatus.LEASED}:
            return task
    return next((task for task in tasks if task.status == TaskStatus.READY), None)


def _attention_task(tasks: list[TaskRecord]) -> TaskRecord | None:
    return next(
        (
            task
            for task in tasks
            if task.status in {TaskStatus.BLOCKED, TaskStatus.WAITING_APPROVAL, TaskStatus.FAILED}
            or task.required_approvals
        ),
        None,
    )


def _objective_state(objective: ObjectiveRecord, tasks: list[TaskRecord], leases: list[TaskLease]) -> str:
    if objective.status == ObjectiveStatus.COMPLETED or (tasks and all(task.status == TaskStatus.SUCCEEDED for task in tasks)):
        return "completed"
    if any(task.status == TaskStatus.FAILED for task in tasks):
        return "failed"
    if _attention_task(tasks) is not None:
        return "blocked"
    if any(task.id == lease.task_id and lease.status == TaskLeaseStatus.ACTIVE for lease in leases for task in tasks):
        return "running"
    if any(task.status in {TaskStatus.RUNNING, TaskStatus.LEASED} for task in tasks):
        return "running"
    return "ready"


def _objective_graph_state(objective: ObjectiveRecord, tasks: list[TaskRecord], leases: list[TaskLease]) -> str:
    state = _objective_state(objective, tasks, leases)
    return "completed" if state == "completed" else "failed" if state == "failed" else "blocked" if state == "blocked" else "running" if state == "running" else "ready"


def _task_graph_state(task: TaskRecord, lease: TaskLease | None, attempt: TaskAttempt | None) -> str:
    if task.status == TaskStatus.SUCCEEDED:
        return "completed"
    if task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED} or (attempt is not None and attempt.failure_code):
        return "failed"
    if task.status in {TaskStatus.BLOCKED, TaskStatus.WAITING_APPROVAL} or task.required_approvals:
        return "blocked"
    if task.status in {TaskStatus.RUNNING, TaskStatus.LEASED} or (lease is not None and lease.status == TaskLeaseStatus.ACTIVE):
        return "running"
    if task.status == TaskStatus.READY:
        return "ready"
    return "waiting"


def _run_graph_state(run: RunRecord) -> str:
    status = str(run.status or "").casefold()
    if status in {"completed", "succeeded", "success"}:
        return "completed"
    if status in {"failed", "error", "cancelled", "canceled"}:
        return "failed"
    if status in {"running", "started", "in_progress"}:
        return "running"
    return "ready"


def _task_order(tasks: list[TaskRecord], dependencies: list[TaskDependency]) -> list[TaskRecord]:
    task_by_id = {task.id: task for task in tasks}
    indegree = {task.id: 0 for task in tasks}
    downstream: dict[str, list[str]] = defaultdict(list)
    for dependency in dependencies:
        if dependency.upstream_task_id in task_by_id and dependency.downstream_task_id in task_by_id:
            downstream[dependency.upstream_task_id].append(dependency.downstream_task_id)
            indegree[dependency.downstream_task_id] += 1
    for task in tasks:
        for upstream_id in task.depends_on:
            if upstream_id in task_by_id:
                downstream[upstream_id].append(task.id)
                indegree[task.id] += 1
    ready = sorted([task_id for task_id, count in indegree.items() if count == 0], key=lambda task_id: task_by_id[task_id].created_at)
    ordered: list[TaskRecord] = []
    while ready:
        task_id = ready.pop(0)
        ordered.append(task_by_id[task_id])
        for next_id in sorted(downstream.get(task_id, []), key=lambda value: task_by_id[value].created_at):
            indegree[next_id] -= 1
            if indegree[next_id] == 0:
                ready.append(next_id)
    if len(ordered) != len(tasks):
        return sorted(tasks, key=lambda task: (task.created_at, task.id))
    return ordered


def _latest_attempts_by_task(attempts: list[TaskAttempt]) -> dict[str, TaskAttempt]:
    result: dict[str, TaskAttempt] = {}
    for attempt in attempts:
        current = result.get(attempt.task_id)
        if current is None or attempt.attempt_number >= current.attempt_number:
            result[attempt.task_id] = attempt
    return result


def _latest_leases_by_task(leases: list[TaskLease]) -> dict[str, TaskLease]:
    result: dict[str, TaskLease] = {}
    for lease in leases:
        current = result.get(lease.task_id)
        if lease.status == TaskLeaseStatus.ACTIVE or current is None or lease.acquired_at >= current.acquired_at:
            result[lease.task_id] = lease
    return result


def _runs_by_task(runs: list[RunRecord]) -> dict[str, list[RunRecord]]:
    result: dict[str, list[RunRecord]] = defaultdict(list)
    for run in runs:
        if run.task_id:
            result[run.task_id].append(run)
    for task_runs in result.values():
        task_runs.sort(key=lambda run: run.created_at)
    return result


def _task_run(
    task: TaskRecord,
    attempt: TaskAttempt | None,
    runs_by_id: dict[str, RunRecord],
    runs_by_task: dict[str, list[RunRecord]],
) -> RunRecord | None:
    if task.run_id and task.run_id in runs_by_id:
        return runs_by_id[task.run_id]
    if attempt is not None and attempt.run_id and attempt.run_id in runs_by_id:
        return runs_by_id[attempt.run_id]
    task_runs = runs_by_task.get(task.id) or []
    return task_runs[-1] if task_runs else None


def _approval_gate_nodes(task: TaskRecord) -> list[GraphNode]:
    nodes = []
    approvals = task.required_approvals or []
    if task.status == TaskStatus.WAITING_APPROVAL and not approvals:
        approvals = ["operator_approval"]
    for approval in approvals:
        approval_id = str(sanitize_for_logging(str(approval)))
        nodes.append(
            GraphNode(
                id=f"approval:{task.id}:{approval_id}",
                kind="approval_gate",
                title=_human_title(approval_id),
                state="blocked",
                entity_id=task.id,
                entity_kind="task_approval",
                lane_id="gate",
                attention=True,
                symbol="◆",
                detail_rows=[
                    f"Task: {task.title}",
                    f"Approval: {approval_id}",
                    "State: required",
                    "Reason: task requires explicit approval before dispatch",
                ],
            )
        )
    return nodes


def _blocker_nodes(task: TaskRecord) -> list[GraphNode]:
    blockers = []
    if task.status == TaskStatus.BLOCKED and not task.required_approvals:
        blockers.append(str(task.approval_state or task.metadata.get("blocked_reason") or "blocked"))
    for blocker in blockers:
        blockers_id = str(sanitize_for_logging(blocker))
        blockers_title = _human_title(blockers_id)
        yield GraphNode(
            id=f"blocker:{task.id}:{blockers_id}",
            kind="blocker",
            title=blockers_title,
            state="blocked",
            entity_id=task.id,
            entity_kind="task_blocker",
            lane_id="gate",
            attention=True,
            symbol="■",
            detail_rows=[f"Task: {task.title}", f"Blocker: {blockers_id}", "Next: inspect progress details"],
        )


def _append_artifact_and_verification_nodes(
    *,
    snapshot: OrchestrationStateSnapshot,
    run: RunRecord,
    task: TaskRecord,
    run_node_id: str,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    active_nodes: list[str],
    attention_nodes: list[str],
) -> None:
    for artifact in snapshot.artifacts_by_run.get(run.id, []):
        artifact_node_id = _artifact_node_id(artifact.id)
        nodes.append(
            GraphNode(
                id=artifact_node_id,
                kind="artifact",
                title=f"{artifact.kind} {display_artifact_path(artifact.path)}",
                state="completed" if artifact.evidence_status == "verified" else "ready",
                entity_id=artifact.id,
                entity_kind="artifact",
                lane_id="evidence",
                symbol="⬡",
                detail_rows=[
                    f"Artifact: {artifact.id}",
                    f"Kind: {artifact.kind}",
                    f"Producer: {artifact.producer or task.agent_id or 'unknown'}",
                    f"Hash: {(artifact.sha256 or '')[:12] or 'unknown'}",
                    f"Redaction: {artifact.redaction_state}",
                    f"Evidence: {artifact.evidence_status}",
                    f"Command: harness artifacts list {run.id} --project {snapshot.project_root} --output json",
                ],
                metadata={
                    "artifact_id": artifact.id,
                    "kind": artifact.kind,
                    "sha256": artifact.sha256,
                    "redaction_state": artifact.redaction_state,
                    "evidence_status": artifact.evidence_status,
                    "path_display": display_artifact_path(artifact.path),
                },
            )
        )
        edges.append(_edge("produces", run_node_id, artifact_node_id))
    for event in snapshot.run_events_by_run.get(run.id, []):
        if _is_verification_event(event.event_type):
            verification_node_id = f"verification:{event.id}"
            state = "failed" if "failed" in str(event.event_type).casefold() or str(event.payload.get("status", "")).casefold() == "failed" else "completed"
            if state == "failed":
                attention_nodes.append(verification_node_id)
            nodes.append(
                GraphNode(
                    id=verification_node_id,
                    kind="verification",
                    title=_human_title(event.event_type),
                    state=state,
                    entity_id=event.id,
                    entity_kind="run_event",
                    lane_id="evidence",
                    attention=state == "failed",
                    symbol=_symbol_for_state(state),
                    detail_rows=[
                        f"Run: {run.id}",
                        f"Verification: {event.event_type}",
                        f"Status: {event.payload.get('status') or state}",
                        f"Command: harness runs --project {snapshot.project_root}",
                    ],
                )
            )
            edges.append(_edge("verifies", run_node_id, verification_node_id))


def _append_dependency_edges(
    dependencies: list[TaskDependency],
    task_by_id: dict[str, TaskRecord],
    edges: list[GraphEdge],
) -> None:
    seen: set[tuple[str, str]] = set()
    for dependency in dependencies:
        if dependency.upstream_task_id in task_by_id and dependency.downstream_task_id in task_by_id:
            pair = (dependency.upstream_task_id, dependency.downstream_task_id)
            if pair in seen:
                continue
            seen.add(pair)
            edges.append(
                _edge(
                    "depends_on",
                    _task_node_id(dependency.upstream_task_id),
                    _task_node_id(dependency.downstream_task_id),
                    title=dependency.dependency_type.value,
                )
            )
    for task in task_by_id.values():
        for upstream_id in task.depends_on:
            if upstream_id in task_by_id:
                pair = (upstream_id, task.id)
                if pair not in seen:
                    seen.add(pair)
                    edges.append(_edge("depends_on", _task_node_id(upstream_id), _task_node_id(task.id)))


def _append_artifact_handoff_edges(
    snapshot: OrchestrationStateSnapshot,
    task_by_id: dict[str, TaskRecord],
    attempts_by_task: dict[str, TaskAttempt],
    runs_by_id: dict[str, RunRecord],
    runs_by_task: dict[str, list[RunRecord]],
    edges: list[GraphEdge],
) -> None:
    for dependency in snapshot.dependencies:
        if dependency.dependency_type != TaskDependencyType.ARTIFACT:
            continue
        if dependency.upstream_task_id not in task_by_id or dependency.downstream_task_id not in task_by_id:
            continue
        upstream = task_by_id[dependency.upstream_task_id]
        run = _task_run(upstream, attempts_by_task.get(upstream.id), runs_by_id, runs_by_task)
        if run is None:
            continue
        artifact = next(
            (
                artifact
                for artifact in snapshot.artifacts_by_run.get(run.id, [])
                if not dependency.required_artifact_kind or artifact.kind == dependency.required_artifact_kind
            ),
            None,
        )
        if artifact is not None:
            edges.append(_edge("consumes", _artifact_node_id(artifact.id), _task_node_id(dependency.downstream_task_id)))


def _lanes_for_graph(nodes: list[GraphNode], tasks: list[TaskRecord]) -> list[AgentLane]:
    lanes = [AgentLane(id="orchestrator", title="Orchestrator", row=0)]
    lane_ids = {"orchestrator"}
    if any(node.kind in {"approval_gate", "blocker"} for node in nodes):
        lanes.append(AgentLane(id="gate", title="Gate", row=len(lanes)))
        lane_ids.add("gate")
    for task in tasks:
        lane_id = _lane_id_for_task(task)
        if lane_id not in lane_ids:
            lanes.append(AgentLane(id=lane_id, title=_lane_title_for_task(task), agent_id=task.agent_id, row=len(lanes)))
            lane_ids.add(lane_id)
    if any(node.kind in {"artifact", "verification"} for node in nodes):
        lanes.append(AgentLane(id="evidence", title="Evidence", row=len(lanes)))
    if len(lanes) == 1:
        lanes.append(AgentLane(id="unassigned", title="Work", row=1))
    return lanes


def _task_detail_rows(project_root: Path, task: TaskRecord, lease: TaskLease | None, attempt: TaskAttempt | None) -> list[str]:
    rows = [
        f"Task: {task.title}",
        f"Agent: {task.agent_id or task.workbench_id or 'unassigned'}",
        f"Adapter: {task.metadata.get('execution_adapter') or 'none'}",
        f"State: {task.status.value}",
        f"Lease: {'active' if lease is not None and lease.status == TaskLeaseStatus.ACTIVE else 'none'}",
        f"Approval: {'required' if task.required_approvals else 'none'}",
    ]
    if task.required_approvals:
        rows.append(f"Reason: {', '.join(task.required_approvals)}")
    if attempt is not None and attempt.failure_code:
        rows.append(f"Failure: {attempt.failure_code}")
    rows.append(f"Command: harness progress --objective {task.objective_id or '<objective_id>'} --project {project_root} --output json")
    return rows


def _timeline_tail(
    snapshot: OrchestrationStateSnapshot,
    objective_id: str,
    runs_by_task: dict[str, list[RunRecord]],
    tasks: list[TaskRecord],
) -> list[GraphEvent]:
    events: list[GraphEvent] = []
    for stored in snapshot.orchestration_events_by_objective.get(objective_id, [])[-6:]:
        events.append(
            GraphEvent(
                seq=stored.seq,
                orchestration_id=objective_id,
                objective_id=objective_id,
                event_type=stored.kind,
                entity_id=stored.task_id or stored.run_id or stored.artifact_id,
                timestamp=stored.created_at.isoformat(),
                summary=stored.kind,
            )
        )
    run_ids = {run.id for task in tasks for run in runs_by_task.get(task.id, [])}
    seq_offset = len(events)
    for run_id in run_ids:
        for event in snapshot.run_events_by_run.get(run_id, [])[-3:]:
            events.append(
                GraphEvent(
                    seq=seq_offset + int(event.seq or 0),
                    orchestration_id=objective_id,
                    objective_id=objective_id,
                    event_type=event.event_type,
                    entity_id=event.task_id or event.run_id,
                    timestamp=event.created_at.isoformat(),
                    summary=event.message or event.event_type,
                )
            )
    events.sort(key=lambda event: (event.timestamp, event.seq))
    return events[-6:]


def _last_event_seq(snapshot: OrchestrationStateSnapshot, objective_id: str) -> int:
    stored = snapshot.orchestration_events_by_objective.get(objective_id, [])
    return max((event.seq for event in stored), default=0)


def _latest_updated_at(objective: ObjectiveRecord, tasks: list[TaskRecord]) -> str:
    values: list[datetime] = [objective.updated_at]
    values.extend(task.updated_at for task in tasks)
    return max(values).isoformat()


def _first_string(values) -> str | None:
    for value in values:
        if value:
            return str(value)
    return None


def _lane_id_for_task(task: TaskRecord) -> str:
    return str(task.agent_id or task.workbench_id or "unassigned")


def _lane_title_for_task(task: TaskRecord) -> str:
    return _human_title(task.agent_id or task.workbench_id or "Work")


def _run_title(task: TaskRecord, run: RunRecord) -> str:
    adapter = task.metadata.get("execution_adapter") or run.backend_name or "adapter"
    return f"{_human_title(adapter)} run"


def _is_verification_event(event_type: str) -> bool:
    value = str(event_type).casefold()
    return value in {
        RunEventType.TEST_STARTED.value,
        RunEventType.TEST_FINISHED.value,
    } or "test" in value or "verification" in value


def _objective_node_id(objective_id: str) -> str:
    return f"objective:{objective_id}"


def _task_node_id(task_id: str) -> str:
    return f"task:{task_id}"


def _run_node_id(run_id: str) -> str:
    return f"run:{run_id}"


def _artifact_node_id(artifact_id: str) -> str:
    return f"artifact:{artifact_id}"


def _edge(kind: str, source_node_id: str, target_node_id: str, *, title: str | None = None) -> GraphEdge:
    return GraphEdge(
        id=f"{kind}:{source_node_id}->{target_node_id}",
        kind=kind,  # type: ignore[arg-type]
        source_node_id=source_node_id,
        target_node_id=target_node_id,
        title=title,
    )


def _symbol_for_state(state: str) -> str:
    return {
        "running": "●",
        "completed": "✓",
        "ready": "○",
        "waiting": "○",
        "blocked": "■",
        "failed": "!",
    }.get(state, "○")


def _human_title(value: object) -> str:
    text = str(sanitize_for_logging(str(value or "unknown"))).replace("_", " ").replace("-", " ").replace("/", " ")
    return " ".join(part.capitalize() for part in text.split()) or "Unknown"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _dedupe_nodes(nodes: list[GraphNode]) -> list[GraphNode]:
    seen: set[str] = set()
    result: list[GraphNode] = []
    for node in nodes:
        if node.id in seen:
            continue
        seen.add(node.id)
        result.append(node)
    return result


def _dedupe_edges(edges: list[GraphEdge]) -> list[GraphEdge]:
    seen: set[str] = set()
    result: list[GraphEdge] = []
    for edge in edges:
        key = f"{edge.kind}:{edge.source_node_id}->{edge.target_node_id}"
        if key in seen:
            continue
        seen.add(key)
        result.append(edge.model_copy(update={"id": key}))
    return result
