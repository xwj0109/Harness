from __future__ import annotations

from pathlib import Path

import pytest

from harness.memory.sqlite_store import SQLiteStore
from harness.models import (
    AgentLane,
    EventStreamType,
    GraphEdge,
    GraphNode,
    LiveOrchestrationGraph,
    RunEventType,
    TaskDependencyType,
)
from harness.left_pane import build_left_pane_view, left_pane_list_item_labels, render_left_pane, selected_left_pane_item
from harness.operator_context import build_tui_dashboard
from harness.orchestration_layout import assign_stable_graph_layout, graph_position_map
from harness.orchestration_projector import project_orchestration_graphs
from harness.orchestration_state import load_orchestration_state
from harness.orchestration_validate import display_artifact_path, validate_live_orchestration_graph
from harness.right_pane import _instance_symbol, _status_label
from harness.tui_primitives import status_label, status_symbol
from harness.tui import build_command_palette, build_right_panel_model, render_right_panel, render_right_panel_detail


def _store(project: Path) -> SQLiteStore:
    return SQLiteStore.open_initialized(project)


def _project(project: Path):
    snapshot = load_orchestration_state(project)
    return project_orchestration_graphs(snapshot)


def test_graph_projector_single_objective(tmp_path) -> None:
    store = _store(tmp_path)
    objective = store.create_objective("Coding fix")
    task = store.create_task("Plan the fix", objective_id=objective.id, agent_id="planner")

    instances, graphs = _project(tmp_path)
    graph = graphs[0]

    assert instances[0].title == "Coding fix"
    assert {node.kind for node in graph.nodes} >= {"objective", "task"}
    assert any(edge.kind == "contains" and edge.target_node_id == f"task:{task.id}" for edge in graph.edges)


def test_graph_projector_multi_agent_handoff(tmp_path) -> None:
    store = _store(tmp_path)
    objective = store.create_objective("Multi-agent handoff")
    planner = store.create_task("Produce plan", objective_id=objective.id, agent_id="Planner")
    coder = store.create_task("Apply patch", objective_id=objective.id, agent_id="Coder", depends_on=[planner.id])

    _, graphs = _project(tmp_path)
    graph = graphs[0]

    assert {"Planner", "Coder"}.issubset({lane.title for lane in graph.lanes})
    assert any(
        edge.kind == "depends_on"
        and edge.source_node_id == f"task:{planner.id}"
        and edge.target_node_id == f"task:{coder.id}"
        for edge in graph.edges
    )


def test_graph_projector_approval_gate(tmp_path) -> None:
    store = _store(tmp_path)
    objective = store.create_objective("Approval gate")
    task = store.create_task(
        "Hosted edit",
        objective_id=objective.id,
        agent_id="Coder",
        required_approvals=["hosted_boundary"],
    )

    _, graphs = _project(tmp_path)
    graph = graphs[0]
    gate = next(node for node in graph.nodes if node.kind == "approval_gate")

    assert gate.attention is True
    assert gate.symbol == "◆"
    assert gate.id in graph.attention_node_ids
    assert any(edge.kind == "requires_approval" and edge.target_node_id == f"task:{task.id}" for edge in graph.edges)


def test_graph_projector_artifact_handoff(tmp_path) -> None:
    store = _store(tmp_path)
    objective = store.create_objective("Artifact handoff")
    producer = store.create_task("Produce report", objective_id=objective.id, agent_id="Planner")
    consumer = store.create_task("Review report", objective_id=objective.id, agent_id="Reviewer")
    run = store.create_run("report run", "read_only_repo_summary", status="completed", task_id=producer.id, objective_id=objective.id)
    artifact_path = tmp_path / "report.txt"
    artifact_path.write_text("artifact body must not render", encoding="utf-8")
    artifact = store.register_artifact(run.id, "report", artifact_path, producer="Planner")
    with store.connect() as conn:
        store._create_task_dependency(  # noqa: SLF001 - test fixture for an artifact dependency edge.
            conn,
            upstream_task_id=producer.id,
            downstream_task_id=consumer.id,
            dependency_type=TaskDependencyType.ARTIFACT,
            required_artifact_kind="report",
            created_at=producer.created_at.isoformat(),
        )

    _, graphs = _project(tmp_path)
    graph = graphs[0]

    assert any(node.kind == "artifact" and node.entity_id == artifact.id for node in graph.nodes)
    assert any(edge.kind == "produces" and edge.target_node_id == f"artifact:{artifact.id}" for edge in graph.edges)
    assert any(edge.kind == "consumes" and edge.source_node_id == f"artifact:{artifact.id}" for edge in graph.edges)
    assert "artifact body must not render" not in graph.model_dump_json()


def test_graph_projector_verification_result(tmp_path) -> None:
    store = _store(tmp_path)
    objective = store.create_objective("Verify")
    task = store.create_task("Run tests", objective_id=objective.id, agent_id="Verifier")
    run = store.create_run("test run", "phase_1a_test", status="completed", task_id=task.id, objective_id=objective.id)
    store.append_run_event(run.id, RunEventType.TEST_FINISHED, {"status": "passed"}, message="tests passed")

    _, graphs = _project(tmp_path)
    graph = graphs[0]

    assert any(node.kind == "verification" and node.state == "completed" for node in graph.nodes)
    assert any(edge.kind == "verifies" for edge in graph.edges)


def test_graph_rejects_cycles() -> None:
    graph = LiveOrchestrationGraph(
        orchestration_id="orch",
        nodes=[
            GraphNode(id="a", kind="task", title="A"),
            GraphNode(id="b", kind="task", title="B"),
        ],
        edges=[
            GraphEdge(id="a-b", source_node_id="a", target_node_id="b", kind="depends_on"),
            GraphEdge(id="b-a", source_node_id="b", target_node_id="a", kind="depends_on"),
        ],
        lanes=[AgentLane(id="work", title="Work")],
    )

    with pytest.raises(ValueError, match="cycle"):
        validate_live_orchestration_graph(graph)


def test_graph_rejects_unknown_edges() -> None:
    graph = LiveOrchestrationGraph(
        orchestration_id="orch",
        nodes=[GraphNode(id="a", kind="task", title="A")],
        edges=[GraphEdge(id="missing", source_node_id="a", target_node_id="missing", kind="depends_on")],
        lanes=[AgentLane(id="work", title="Work")],
    )

    with pytest.raises(ValueError, match="unknown target"):
        validate_live_orchestration_graph(graph)


def test_graph_redacts_secret_like_paths(tmp_path) -> None:
    assert display_artifact_path(tmp_path / ".harness" / "runs" / "secret.txt") == "path redacted"
    assert display_artifact_path(tmp_path / "report.txt") == "report.txt"


def test_graph_does_not_render_artifact_bodies(tmp_path) -> None:
    store = _store(tmp_path)
    objective = store.create_objective("Evidence")
    task = store.create_task("Produce evidence", objective_id=objective.id, agent_id="Verifier")
    run = store.create_run("evidence run", "phase_1a_test", status="completed", task_id=task.id, objective_id=objective.id)
    artifact_path = tmp_path / "evidence.txt"
    artifact_path.write_text("VERY_SECRET_BODY_SHOULD_NOT_RENDER", encoding="utf-8")
    artifact = store.register_artifact(run.id, "evidence", artifact_path, producer="Verifier")

    dashboard = build_tui_dashboard(tmp_path)
    model = build_right_panel_model(
        dashboard,
        {"palette": build_command_palette(), "right_pane_mode": "evidence"},
        "",
        "dashboard",
    )
    rendered = render_right_panel(model)

    assert artifact.id in rendered
    assert "VERY_SECRET_BODY_SHOULD_NOT_RENDER" not in rendered
    assert "evidence.txt" in rendered


def test_right_pane_idle_snapshot(tmp_path) -> None:
    dashboard = build_tui_dashboard(tmp_path)
    model = build_right_panel_model(dashboard, {"palette": build_command_palette()}, "", "dashboard")
    rendered = render_right_panel(model)

    assert model["cockpit_schema_version"] == "harness.right_pane_cockpit/v1"
    assert model["mode"] == "overview"
    assert "ORCHESTRATIONS" in rendered
    assert "No assigned orchestrations." in rendered
    assert "Next: /init" in rendered
    assert "IDs:" not in rendered


def test_right_pane_running_task_snapshot(tmp_path) -> None:
    store = _store(tmp_path)
    objective = store.create_objective("Running objective")
    task = store.create_task(
        "Lease this",
        objective_id=objective.id,
        metadata={"execution_adapter": "repo_planning", "task_type": "repo_planning"},
    )
    lease = store.select_next_task_for_lease("test-owner")["lease"]

    dashboard = build_tui_dashboard(tmp_path)
    rendered = render_right_panel(build_right_panel_model(dashboard, {"palette": build_command_palette()}, "", "dashboard"))

    assert task.id == lease.task_id
    assert "Running objective" in rendered
    assert "State: running" in rendered
    assert task.id not in rendered
    assert lease.id not in rendered


def test_right_pane_blocked_approval_snapshot(tmp_path) -> None:
    store = _store(tmp_path)
    objective = store.create_objective("Approval objective")
    store.create_task("Hosted edit", objective_id=objective.id, required_approvals=["hosted_boundary"])

    dashboard = build_tui_dashboard(tmp_path)
    rendered = render_right_panel(build_right_panel_model(dashboard, {"palette": build_command_palette()}, "", "dashboard"))

    assert "Approval gate: Hosted Boundary" in rendered
    assert "◆ Hosted Boundary" in rendered


def test_right_pane_failed_run_snapshot(tmp_path) -> None:
    store = _store(tmp_path)
    objective = store.create_objective("Failed objective")
    task = store.create_task(
        "Run failing tests",
        objective_id=objective.id,
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    store.create_run("failed", "phase_1a_test", status="failed", task_id=task.id, objective_id=objective.id)

    dashboard = build_tui_dashboard(tmp_path)
    rendered = render_right_panel(build_right_panel_model(dashboard, {"palette": build_command_palette()}, "", "dashboard"))

    assert "Failed objective" in rendered
    assert "Failed:" in rendered or "failed" in rendered
    assert "Recent run:" in rendered or "Run:" in rendered


def test_all_orchestrations_view(tmp_path) -> None:
    store = _store(tmp_path)
    first = store.create_objective("Coding fix")
    second = store.create_objective("Repo summary")
    store.create_task("Plan fix", objective_id=first.id, agent_id="Planner")
    store.create_task("Summarize repo", objective_id=second.id, agent_id="Summary")

    dashboard = build_tui_dashboard(tmp_path)
    model = build_right_panel_model(
        dashboard,
        {"palette": build_command_palette(), "right_pane_mode": "graph", "show_all_orchestrations": True},
        "",
        "dashboard",
    )
    rendered = render_right_panel(model)

    assert "Coding fix" in rendered
    assert "Repo summary" in rendered
    assert model["show_all_orchestrations"] is True


def test_narrow_terminal_compact_mode(tmp_path) -> None:
    store = _store(tmp_path)
    objective = store.create_objective("Compact")
    for index in range(10):
        store.create_task(f"Task {index}", objective_id=objective.id, agent_id=f"Agent {index}")

    _, graphs = _project(tmp_path)
    rows = render_right_panel(
        build_right_panel_model(
            build_tui_dashboard(tmp_path),
            {"palette": build_command_palette(), "right_pane_mode": "overview"},
            "",
            "dashboard",
        )
    )

    assert len(graphs[0].nodes) > 7
    assert "... " in rows
    assert "GRAPH" in rows


def test_graph_mode_renders_lane_flowchart(tmp_path) -> None:
    store = _store(tmp_path)
    objective = store.create_objective("Flowchart")
    planner = store.create_task("Plan fix", objective_id=objective.id, agent_id="Planner")
    store.create_task("Apply patch", objective_id=objective.id, agent_id="Coder", depends_on=[planner.id])

    rendered = render_right_panel(
        build_right_panel_model(
            build_tui_dashboard(tmp_path),
            {"palette": build_command_palette(), "right_pane_mode": "graph"},
            "",
            "dashboard",
        )
    )

    assert "PLANNER" in rendered
    assert "CODER" in rendered
    assert "Plan fix" in rendered
    assert "Apply patch" in rendered
    assert "Flow:" in rendered
    assert "Plan fix" in rendered and "->" in rendered and "Apply patch" in rendered
    assert "depends_on" in rendered


def test_graph_layout_stable_after_event_update(tmp_path) -> None:
    store = _store(tmp_path)
    objective = store.create_objective("Stable layout")
    task = store.create_task("First task", objective_id=objective.id, agent_id="Planner")
    _, graphs = _project(tmp_path)
    first_graph = graphs[0]
    first_positions = graph_position_map(first_graph)
    store.append_store_event(EventStreamType.ORCHESTRATION, objective.id, "task.ready", {"task_id": task.id}, task_id=task.id)

    snapshot = load_orchestration_state(tmp_path)
    _, updated_graphs = project_orchestration_graphs(
        snapshot,
        previous_positions_by_orchestration={objective.id: first_positions},
    )
    updated_positions = graph_position_map(assign_stable_graph_layout(updated_graphs[0], first_positions))

    for node_id, position in first_positions.items():
        assert updated_positions[node_id] == position


def test_right_pane_navigation_is_ui_only(tmp_path) -> None:
    dashboard = build_tui_dashboard(tmp_path)
    model = build_right_panel_model(dashboard, {"palette": build_command_palette(), "right_pane_mode": "graph"}, "", "dashboard")

    assert model["active_signal"] == "setup_needed"
    assert not (tmp_path / ".harness").exists()
    assert model["summary"]["initialized"] is False


def test_right_pane_enter_opens_details_without_dispatch(tmp_path) -> None:
    store = _store(tmp_path)
    objective = store.create_objective("Detail objective")
    store.create_task(
        "Detail task",
        objective_id=objective.id,
        metadata={"execution_adapter": "repo_planning", "task_type": "repo_planning"},
    )
    dashboard = build_tui_dashboard(tmp_path)
    model = build_right_panel_model(dashboard, {"palette": build_command_palette()}, "", "dashboard")
    detail = render_right_panel_detail(model)

    assert "Read-only persisted Harness projection" in detail
    assert "process=False" in detail
    assert "adapter=False" in detail
    assert len(store.list_runs()) == 0


def _left_model(tmp_path: Path, *, mode: str = "sessions", state: dict | None = None, query: str = "") -> dict:
    dashboard = build_tui_dashboard(tmp_path, selected_session_id=(state or {}).get("selected_session_id"))
    right = build_right_panel_model(dashboard, {"palette": build_command_palette()}, "", "dashboard")
    return build_left_pane_view(
        dashboard,
        {"left_pane_mode": mode, **(state or {})},
        query,
        right_pane_model=right,
    )


def test_left_pane_sessions_snapshot(tmp_path) -> None:
    store = _store(tmp_path)
    session = store.create_session(title="Ciao")
    objective = store.create_objective("Coding fix", session_id=session.id)
    store.create_task("Hosted edit", objective_id=objective.id, agent_id="Coder", required_approvals=["hosted_boundary"])

    model = _left_model(
        tmp_path,
        mode="sessions",
        state={"selected_session_id": session.id, "active_session_id": session.id},
    )
    rendered = render_left_pane(model)

    assert model["schema_version"] == "harness.left_pane/v1"
    assert "SCOPE" in rendered
    assert "Ciao" in rendered
    assert "1 orch" in rendered
    assert "approval required" in rendered
    assert session.id not in rendered


def test_left_pane_orchestrations_snapshot(tmp_path) -> None:
    store = _store(tmp_path)
    running = store.create_objective("Coding fix")
    store.create_task(
        "Plan fix",
        objective_id=running.id,
        agent_id="Planner",
        metadata={"execution_adapter": "repo_planning", "task_type": "repo_planning"},
    )
    store.select_next_task_for_lease("Planner")
    ready = store.create_objective("Repo summary")
    store.create_task("Summarize repo", objective_id=ready.id, agent_id="Summary")

    model = _left_model(tmp_path, mode="orchestrations", state={"selected_orchestration_id": running.id})
    rendered = render_left_pane(model)

    assert "Coding fix" in rendered
    assert "Repo summary" in rendered
    assert "running" in rendered
    assert "Planner" in rendered


def test_left_pane_agents_snapshot(tmp_path) -> None:
    store = _store(tmp_path)
    objective = store.create_objective("Agent work")
    store.create_task("Plan the coding fix", objective_id=objective.id, agent_id="Planner")
    store.create_task("Prepare isolated Codex edit", objective_id=objective.id, agent_id="Coder", required_approvals=["hosted_boundary"])

    model = _left_model(tmp_path, mode="agents")
    rendered = render_left_pane(model)

    assert "Planner" in rendered
    assert "Coder" in rendered
    assert "blocked" in rendered
    assert "Prepare isolated Codex" in rendered


def test_left_pane_queue_snapshot(tmp_path) -> None:
    store = _store(tmp_path)
    objective = store.create_objective("Queue work")
    store.create_task("Running task", objective_id=objective.id, agent_id="Planner", priority=10)
    store.select_next_task_for_lease("Planner")
    store.create_task("Ready task", objective_id=objective.id, agent_id="Reviewer")
    store.create_task("Blocked task", objective_id=objective.id, agent_id="Coder", required_approvals=["hosted_boundary"])

    model = _left_model(tmp_path, mode="queue")
    rendered = render_left_pane(model)

    assert "Ready   1" in rendered
    assert "Running 1" in rendered
    assert "Blocked 1" in rendered
    assert "Blocked task" in rendered
    assert "approval required" in rendered


def test_left_pane_selection_updates_right_scope(tmp_path) -> None:
    store = _store(tmp_path)
    first = store.create_objective("First")
    second = store.create_objective("Second")
    store.create_task("Inspect second", objective_id=second.id, agent_id="Planner")

    left = _left_model(
        tmp_path,
        mode="orchestrations",
        state={"left_selected_item_id": f"orchestration:{second.id}"},
    )
    selected = selected_left_pane_item(left)
    dashboard = build_tui_dashboard(tmp_path)
    right = build_right_panel_model(
        dashboard,
        {"palette": build_command_palette(), "selected_orchestration_id": selected["id"]},
        "",
        "dashboard",
    )

    assert selected["kind"] == "orchestration"
    assert selected["id"] == second.id
    assert selected["id"] != first.id
    assert right["selected_orchestration_id"] == second.id


def test_left_pane_blocked_item_surfaces_attention(tmp_path) -> None:
    store = _store(tmp_path)
    objective = store.create_objective("Approval objective")
    store.create_task("Prepare isolated Codex edit", objective_id=objective.id, agent_id="Coder", required_approvals=["hosted_boundary"])

    model = _left_model(tmp_path, mode="queue")
    rendered = render_left_pane(model)

    assert "Prepare isolated Codex edit" in rendered
    assert "reason: approval required" in rendered


def test_left_pane_live_update_preserves_selection(tmp_path) -> None:
    store = _store(tmp_path)
    first = store.create_objective("First")
    second = store.create_objective("Second")
    task = store.create_task("First task", objective_id=first.id, agent_id="Planner")
    store.create_task("Second task", objective_id=second.id, agent_id="Reviewer")

    selected_id = f"orchestration:{second.id}"
    before = _left_model(tmp_path, mode="orchestrations", state={"left_selected_item_id": selected_id})
    store.update_task_status(task.id, "running")
    after = _left_model(tmp_path, mode="orchestrations", state={"left_selected_item_id": before["selected_item_id"]})

    assert before["selected_item_id"] == selected_id
    assert after["selected_item_id"] == selected_id


def test_left_pane_search_filters_items(tmp_path) -> None:
    store = _store(tmp_path)
    store.create_objective("Security check")
    store.create_objective("Repo summary")

    model = _left_model(tmp_path, mode="orchestrations", query="security")
    rendered = render_left_pane(model)

    assert "Security check" in rendered
    assert "Repo summary" not in rendered


def test_left_pane_narrow_width_compact_mode(tmp_path) -> None:
    store = _store(tmp_path)
    objective = store.create_objective("Very long orchestration title that should fit")
    store.create_task("Very long task title that should fit", objective_id=objective.id, agent_id="Planner")

    model = _left_model(tmp_path, mode="orchestrations")
    labels = left_pane_list_item_labels(model, width=24)

    assert labels
    for label in labels:
        for line in label.splitlines():
            assert len(line) <= 24


def test_left_pane_does_not_render_secret_paths(tmp_path) -> None:
    store = _store(tmp_path)
    objective = store.create_objective("Evidence")
    task = store.create_task("Produce evidence", objective_id=objective.id, agent_id="Verifier")
    run = store.create_run("evidence run", "phase_1a_test", status="completed", task_id=task.id, objective_id=objective.id)
    secret_path = tmp_path / ".harness" / "runs" / "secret.txt"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text("VERY_SECRET_BODY_SHOULD_NOT_RENDER", encoding="utf-8")
    store.register_artifact(run.id, "evidence", secret_path, producer="Verifier")

    rendered = render_left_pane(_left_model(tmp_path, mode="orchestrations"))

    assert "evidence available" in rendered
    assert ".harness" not in rendered
    assert "secret.txt" not in rendered
    assert "VERY_SECRET_BODY_SHOULD_NOT_RENDER" not in rendered


def test_left_and_right_share_status_labels() -> None:
    assert status_symbol("blocked") == _instance_symbol("blocked")
    assert status_symbol("running") == _instance_symbol("running")
    assert status_label("approval_required") == _status_label("approval_required")
    assert status_label("waiting_approval") == _status_label("waiting_approval")
