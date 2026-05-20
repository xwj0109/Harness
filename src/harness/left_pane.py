from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from harness.operator_context import build_session_pane_projection
from harness.orchestration_projector import project_orchestration_graphs
from harness.orchestration_state import load_orchestration_state
from harness.paths import resolve_project_root
from harness.tui_primitives import clean_event_summary, humanize_identifier, short_text, status_label, status_symbol


LeftPaneMode = Literal["sessions", "orchestrations", "agents", "queue"]
NavState = Literal["idle", "active", "ready", "running", "blocked", "failed", "completed"]

LEFT_PANE_SCHEMA_VERSION = "harness.left_pane/v1"
LEFT_PANE_MODES: tuple[LeftPaneMode, ...] = ("sessions", "orchestrations", "agents", "queue")
LEFT_PANE_FOOTER = "↑↓ select · enter open · / search · f filter · n new"


@dataclass(frozen=True)
class LeftPaneHeader:
    app_label: str
    live: bool
    state: str
    project: str
    branch: str
    model: str


@dataclass(frozen=True)
class NavFilters:
    query: str = ""
    session_status: str = "open"
    active_only: bool = False
    blocked_only: bool = False


@dataclass(frozen=True)
class ShortcutSummary:
    primary: str = LEFT_PANE_FOOTER
    secondary: str = "1-4 mode · g active · b blocked"


@dataclass(frozen=True)
class SessionNavItem:
    id: str
    title: str
    state: NavState
    selected: bool
    active: bool
    message_count: int
    orchestration_count: int
    latest: str | None
    attention: str | None
    updated_at: str
    unread_changes: int = 0


@dataclass(frozen=True)
class OrchestrationNavItem:
    id: str
    title: str
    state: Literal["ready", "running", "blocked", "failed", "completed"]
    selected: bool
    active_agent: str | None
    summary: str | None
    attention: str | None
    updated_at: str
    unread_changes: int


@dataclass(frozen=True)
class AgentNavItem:
    id: str
    label: str
    state: Literal["idle", "ready", "running", "blocked", "failed"]
    selected: bool
    current_orchestration_id: str | None
    current_task_id: str | None
    summary: str | None


@dataclass(frozen=True)
class QueueNavItem:
    id: str
    title: str
    state: Literal["ready", "running", "blocked", "failed"]
    selected: bool
    orchestration_id: str | None
    task_id: str | None
    agent: str | None
    reason: str | None


@dataclass(frozen=True)
class QueueNavSummary:
    ready: int
    running: int
    blocked: int
    failed: int
    items: list[QueueNavItem]


@dataclass(frozen=True)
class LeftPaneView:
    schema_version: str
    ok: bool
    header: LeftPaneHeader
    mode: LeftPaneMode
    sessions: list[SessionNavItem]
    orchestrations: list[OrchestrationNavItem]
    agents: list[AgentNavItem]
    queue: QueueNavSummary
    selected_item_id: str | None
    filters: NavFilters
    footer: ShortcutSummary
    policy_boundary: dict[str, Any]


def build_left_pane_view(
    dashboard: dict[str, Any],
    view_state: dict[str, Any] | None = None,
    query: str = "",
    *,
    right_pane_model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state = dict(view_state or {})
    project_root = resolve_project_root(Path(str(dashboard.get("project_root") or ".")))
    mode = _left_pane_mode(state.get("left_pane_mode") or "sessions")
    normalized_query = query.strip()
    session_filter = str(state.get("session_filter") or "open")
    selected_session_id = _optional_str(state.get("selected_session_id") or state.get("active_session_id"))
    active_session_id = _optional_str(state.get("active_session_id") or (dashboard.get("active_session") or {}).get("id"))
    snapshot = load_orchestration_state(project_root)
    instances, graphs = _orchestration_projection(snapshot, right_pane_model)
    selected_orchestration_id = _optional_str(
        state.get("selected_orchestration_id") or (right_pane_model or {}).get("selected_orchestration_id")
    )
    selected_item_id = _optional_str(state.get("left_selected_item_id"))

    sessions = _session_items(
        project_root=project_root,
        session_filter=session_filter,
        query=normalized_query if mode == "sessions" else "",
        selected_session_id=selected_session_id,
        active_session_id=active_session_id,
        snapshot=snapshot,
        graphs=graphs,
        dashboard=dashboard,
    )
    orchestrations = _orchestration_items(instances, graphs, selected_orchestration_id)
    agents = _agent_items(instances, graphs)
    queue = _queue_summary(graphs)

    if mode == "orchestrations":
        orchestrations = _filter_dataclass_items(orchestrations, normalized_query)
    elif mode == "agents":
        agents = _filter_dataclass_items(agents, normalized_query)
    elif mode == "queue":
        queue = replace(queue, items=_filter_dataclass_items(queue.items, normalized_query))

    candidate_ids = _candidate_item_ids(mode, sessions, orchestrations, agents, queue.items)
    selected_item_id = _resolve_selected_item_id(
        mode=mode,
        requested=selected_item_id,
        candidate_ids=candidate_ids,
        selected_session_id=selected_session_id,
        selected_orchestration_id=selected_orchestration_id,
    )
    sessions = [replace(item, selected=f"session:{item.id}" == selected_item_id) for item in sessions]
    orchestrations = [replace(item, selected=f"orchestration:{item.id}" == selected_item_id) for item in orchestrations]
    agents = [replace(item, selected=f"agent:{item.id}" == selected_item_id) for item in agents]
    queue = replace(queue, items=[replace(item, selected=item.id == selected_item_id) for item in queue.items])

    view = LeftPaneView(
        schema_version=LEFT_PANE_SCHEMA_VERSION,
        ok=bool(dashboard.get("ok", True)),
        header=LeftPaneHeader(
            app_label="Harness",
            live=True,
            state=status_label((dashboard.get("live_activity") or {}).get("active_signal") or "idle"),
            project=project_root.name or str(project_root),
            branch=str(dashboard.get("branch") or "unknown"),
            model=_model_label(dashboard, state),
        ),
        mode=mode,
        sessions=sessions,
        orchestrations=orchestrations,
        agents=agents,
        queue=queue,
        selected_item_id=selected_item_id,
        filters=NavFilters(query=normalized_query, session_status=session_filter),
        footer=ShortcutSummary(),
        policy_boundary={
            "kind": "left_pane_navigation_projection",
            "source": "persisted_harness_state",
            "process_started": False,
            "filesystem_modified": False,
            "active_repo_modified": False,
            "provider_execution_started": False,
            "model_execution_started": False,
            "shell_started": False,
            "docker_started": False,
            "adapter_dispatched": False,
            "permission_granting": False,
            "authority_granting": False,
            "artifact_contents_included": False,
        },
    )
    return asdict(view)


def render_left_pane(view: dict[str, Any], *, width: int = 34, focused: bool = False) -> str:
    return "\n".join(
        [
            render_left_pane_header(view, width=width, focused=focused),
            "",
            *left_pane_list_item_labels(view, width=width),
            "",
            render_left_pane_detail(view, width=width),
            render_left_pane_footer(view, width=width),
        ]
    ).strip()


def render_left_pane_header(view: dict[str, Any], *, width: int = 34, focused: bool = False) -> str:
    header = view.get("header") or {}
    live = "live ●" if header.get("live", True) else "idle ○"
    focus = "focused" if focused else str(header.get("state") or "idle")
    mode = str(view.get("mode") or "sessions")
    mode_rows = [
        _mode_chip("1", "Sessions", mode == "sessions") + " " + _mode_chip("2", "Orchestrations", mode == "orchestrations"),
        _mode_chip("3", "Agents", mode == "agents") + " " + _mode_chip("4", "Queue", mode == "queue"),
    ]
    lines = [
        _two_col("HARNESS", f"{live} {focus}", width=width),
        _clip(f"{header.get('project') or 'project'} · {header.get('branch') or 'unknown'} · {header.get('model') or 'default'}", width),
        "",
        "SCOPE",
        *[_clip(row, width) for row in mode_rows],
    ]
    return "\n".join(lines)


def left_pane_list_item_labels(view: dict[str, Any], *, width: int = 34) -> list[str]:
    mode = str(view.get("mode") or "sessions")
    if mode == "orchestrations":
        return [_orchestration_label(item, width=width) for item in view.get("orchestrations") or []] or ["No orchestrations", "  create or select an objective"]
    if mode == "agents":
        return [_agent_label(item, width=width) for item in view.get("agents") or []] or ["No active agents", "  agents appear when work is assigned"]
    if mode == "queue":
        queue = view.get("queue") or {}
        rows = [
            f"Ready   {queue.get('ready', 0)}",
            f"Running {queue.get('running', 0)}",
            f"Blocked {queue.get('blocked', 0)}",
        ]
        labels = [_queue_label(item, width=width) for item in queue.get("items") or []]
        return ["\n".join(rows), *labels] if labels else ["\n".join(rows), "No queued work"]
    return [_session_label(item, width=width) for item in view.get("sessions") or []] or ["No sessions", "  start by typing a prompt"]


def render_left_pane_detail(view: dict[str, Any], *, width: int = 34) -> str:
    selected = selected_left_pane_item(view)
    if selected is None:
        return _clip("Attention: none", width)
    title = str(selected.get("title") or selected.get("label") or "Selected")
    attention = selected.get("attention") or selected.get("reason")
    if not attention and selected.get("kind") == "session":
        attention = "no open work"
    elif not attention:
        attention = "inspectable"
    lines = [
        f"Selected: {title}",
        _clip(f"Attention: {attention}", width),
    ]
    return "\n".join(lines)


def render_left_pane_footer(view: dict[str, Any], *, width: int = 34) -> str:
    filters = view.get("filters") or {}
    footer = view.get("footer") or {}
    query = str(filters.get("query") or "")
    if query:
        return _clip(f"Search: {query} · enter/esc finish", width)
    primary = str(footer.get("primary") or LEFT_PANE_FOOTER)
    secondary = str(footer.get("secondary") or "")
    return "\n".join(_clip(line, width) for line in [primary, secondary] if line)


def left_pane_visible_items(view: dict[str, Any]) -> list[dict[str, Any]]:
    mode = str(view.get("mode") or "sessions")
    if mode == "orchestrations":
        return [
            {"nav_id": f"orchestration:{item['id']}", "kind": "orchestration", **item}
            for item in view.get("orchestrations") or []
        ]
    if mode == "agents":
        return [
            {"nav_id": f"agent:{item['id']}", "kind": "agent", **item}
            for item in view.get("agents") or []
        ]
    if mode == "queue":
        return [
            {"nav_id": item["id"], "kind": "queue_task", **item}
            for item in (view.get("queue") or {}).get("items") or []
        ]
    return [
        {"nav_id": f"session:{item['id']}", "kind": "session", **item}
        for item in view.get("sessions") or []
    ]


def selected_left_pane_item(view: dict[str, Any]) -> dict[str, Any] | None:
    selected_id = view.get("selected_item_id")
    for item in left_pane_visible_items(view):
        if item.get("nav_id") == selected_id:
            return item
    items = left_pane_visible_items(view)
    return items[0] if items else None


def _orchestration_projection(snapshot, right_pane_model: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if right_pane_model is not None:
        return list(right_pane_model.get("orchestration_instances") or []), list(right_pane_model.get("all_graphs") or [])
    instances, graphs = project_orchestration_graphs(snapshot)
    return [item.model_dump(mode="json") for item in instances], [item.model_dump(mode="json") for item in graphs]


def _session_items(
    *,
    project_root: Path,
    session_filter: str,
    query: str,
    selected_session_id: str | None,
    active_session_id: str | None,
    snapshot,
    graphs: list[dict[str, Any]],
    dashboard: dict[str, Any],
) -> list[SessionNavItem]:
    projection = build_session_pane_projection(
        project_root,
        selected_session_id=selected_session_id,
        status_filter=session_filter,
        query=query,
    )
    selected_id = projection.get("selected_session_id") or selected_session_id
    orchestration_ids_by_session = _orchestration_ids_by_session(snapshot)
    attention_by_orchestration = {graph.get("orchestration_id"): _graph_attention(graph) for graph in graphs}
    active_session = dashboard.get("active_session") or {}
    items = []
    for session in projection.get("sessions") or []:
        session_id = str(session.get("id") or "")
        orchestration_ids = orchestration_ids_by_session.get(session_id, set())
        attention = _session_attention(
            session,
            active_session=active_session if active_session.get("id") == session_id else {},
            orchestration_ids=orchestration_ids,
            attention_by_orchestration=attention_by_orchestration,
        )
        latest = clean_event_summary(session.get("latest_event")) if session.get("latest_event") else None
        state = _session_nav_state(session.get("status"), session_id == active_session_id)
        items.append(
            SessionNavItem(
                id=session_id,
                title=str(session.get("display_title") or session.get("title") or "Untitled session"),
                state=state,
                selected=session_id == selected_id,
                active=session_id == active_session_id,
                message_count=int(session.get("message_count") or 0),
                orchestration_count=len(orchestration_ids),
                latest=latest,
                attention=attention,
                updated_at=str(session.get("updated_at") or ""),
                unread_changes=1 if latest else 0,
            )
        )
    return items


def _orchestration_items(
    instances: list[dict[str, Any]],
    graphs: list[dict[str, Any]],
    selected_orchestration_id: str | None,
) -> list[OrchestrationNavItem]:
    graph_by_id = {graph.get("orchestration_id"): graph for graph in graphs}
    items = []
    for instance in instances:
        graph = graph_by_id.get(instance.get("orchestration_id"))
        active_agent, summary = _active_agent_summary(instance, graph)
        attention = _graph_attention(graph)
        items.append(
            OrchestrationNavItem(
                id=str(instance.get("orchestration_id") or ""),
                title=str(instance.get("title") or "Untitled orchestration"),
                state=_orchestration_nav_state(instance.get("state")),
                selected=instance.get("orchestration_id") == selected_orchestration_id,
                active_agent=active_agent,
                summary=summary,
                attention=attention,
                updated_at=str(instance.get("updated_at") or ""),
                unread_changes=1 if int(instance.get("last_event_seq") or 0) > 0 else 0,
            )
        )
    return items


def _agent_items(instances: list[dict[str, Any]], graphs: list[dict[str, Any]]) -> list[AgentNavItem]:
    instance_by_id = {instance.get("orchestration_id"): instance for instance in instances}
    ranked: dict[str, AgentNavItem] = {}
    for graph in graphs:
        orchestration_id = str(graph.get("orchestration_id") or "")
        instance = instance_by_id.get(orchestration_id) or {}
        lanes = graph.get("lanes") or []
        nodes = graph.get("nodes") or []
        for lane in lanes:
            lane_id = str(lane.get("id") or "")
            if lane_id in {"gate", "evidence"}:
                continue
            label = str(lane.get("title") or lane.get("agent_id") or lane_id or "Agent")
            agent_id = str(lane.get("agent_id") or label)
            lane_nodes = [node for node in nodes if node.get("lane_id") == lane_id and node.get("kind") in {"task", "adapter_run", "objective"}]
            state = _agent_state(lane_nodes)
            current = _current_agent_node(lane_nodes)
            task_id = str(current.get("entity_id") or "") if current and current.get("kind") == "task" else None
            summary = None
            if current and current.get("kind") == "task":
                summary = f"task: {short_text(current.get('title'), limit=40)}"
            elif instance.get("title") and state != "idle":
                summary = f"owns: {short_text(instance.get('title'), limit=40)}"
            item = AgentNavItem(
                id=agent_id,
                label=humanize_identifier(label).title(),
                state=state,
                selected=False,
                current_orchestration_id=orchestration_id if state != "idle" else None,
                current_task_id=task_id,
                summary=summary,
            )
            previous = ranked.get(agent_id)
            if previous is None or _agent_rank(item.state) < _agent_rank(previous.state):
                ranked[agent_id] = item
    return sorted(ranked.values(), key=lambda item: (_agent_rank(item.state), item.label.casefold()))


def _queue_summary(graphs: list[dict[str, Any]]) -> QueueNavSummary:
    items: list[QueueNavItem] = []
    for graph in graphs:
        orchestration_id = str(graph.get("orchestration_id") or "")
        for node in graph.get("nodes") or []:
            if node.get("kind") != "task":
                continue
            state = str(node.get("state") or "ready")
            if state not in {"ready", "running", "blocked", "failed"}:
                continue
            metadata = node.get("metadata") or {}
            items.append(
                QueueNavItem(
                    id=str(node.get("id") or ""),
                    title=str(node.get("title") or "Task"),
                    state=state,  # type: ignore[arg-type]
                    selected=False,
                    orchestration_id=orchestration_id,
                    task_id=str(node.get("entity_id") or "") or None,
                    agent=metadata.get("agent_id") or metadata.get("workbench_id"),
                    reason=_task_reason(graph, node),
                )
            )
    items.sort(key=lambda item: (_queue_rank(item.state), item.title.casefold()))
    return QueueNavSummary(
        ready=sum(1 for item in items if item.state == "ready"),
        running=sum(1 for item in items if item.state == "running"),
        blocked=sum(1 for item in items if item.state == "blocked"),
        failed=sum(1 for item in items if item.state == "failed"),
        items=items,
    )


def _session_label(item: dict[str, Any], *, width: int) -> str:
    marker = "›" if item.get("selected") else " "
    title_line = _two_col(f"{marker} {short_text(item.get('title'), limit=max(8, width - 12))}", status_label(item.get("state")), width=width)
    bits = [
        f"{int(item.get('message_count') or 0)} msgs",
        f"{int(item.get('orchestration_count') or 0)} orch",
    ]
    if int(item.get("unread_changes") or 0):
        bits.append("changed")
    lines = [title_line, _clip("  " + " · ".join(bits), width)]
    if item.get("latest"):
        lines.append(_clip(f"  latest: {item['latest']}", width))
    if item.get("attention"):
        lines.append(_clip(f"  ◆ {item['attention']}", width))
    return "\n".join(lines)


def _orchestration_label(item: dict[str, Any], *, width: int) -> str:
    marker = "›" if item.get("selected") else " "
    symbol = status_symbol(item.get("state"))
    title_line = _two_col(f"{marker} {symbol} {short_text(item.get('title'), limit=max(8, width - 14))}", status_label(item.get("state")), width=width)
    lines = [title_line]
    if item.get("summary"):
        lines.append(_clip(f"  {item['summary']}", width))
    elif item.get("active_agent"):
        lines.append(_clip(f"  {item['active_agent']} assigned", width))
    if item.get("attention"):
        lines.append(_clip(f"  ◆ {item['attention']}", width))
    elif int(item.get("unread_changes") or 0):
        lines.append(_clip("  changed", width))
    return "\n".join(lines)


def _agent_label(item: dict[str, Any], *, width: int) -> str:
    marker = "›" if item.get("selected") else " "
    symbol = status_symbol(item.get("state"))
    title_line = _two_col(f"{marker} {item.get('label') or 'Agent'}", f"{symbol} {status_label(item.get('state'))}", width=width)
    lines = [title_line]
    if item.get("summary"):
        lines.append(_clip(f"  {item['summary']}", width))
    return "\n".join(lines)


def _queue_label(item: dict[str, Any], *, width: int) -> str:
    marker = "›" if item.get("selected") else " "
    symbol = status_symbol(item.get("state"))
    title_line = _two_col(f"{marker} {symbol} {short_text(item.get('title'), limit=max(8, width - 13))}", status_label(item.get("state")), width=width)
    lines = [title_line]
    if item.get("reason"):
        lines.append(_clip(f"  reason: {item['reason']}", width))
    elif item.get("agent"):
        lines.append(_clip(f"  agent: {item['agent']}", width))
    return "\n".join(lines)


def _mode_chip(number: str, label: str, selected: bool) -> str:
    return f"[{number} {label}]" if selected else f" {number} {label} "


def _two_col(left: object, right: object, *, width: int) -> str:
    left_text = str(left or "")
    right_text = str(right or "")
    if len(left_text) + len(right_text) + 1 >= width:
        return _clip(f"{left_text} {right_text}", width)
    return f"{left_text}{' ' * (width - len(left_text) - len(right_text))}{right_text}"


def _clip(value: object, width: int) -> str:
    text = str(value or "")
    if len(text) <= width:
        return text
    return text[: max(1, width - 1)].rstrip() + "…"


def _candidate_item_ids(
    mode: LeftPaneMode,
    sessions: list[SessionNavItem],
    orchestrations: list[OrchestrationNavItem],
    agents: list[AgentNavItem],
    queue_items: list[QueueNavItem],
) -> list[str]:
    if mode == "orchestrations":
        return [f"orchestration:{item.id}" for item in orchestrations]
    if mode == "agents":
        return [f"agent:{item.id}" for item in agents]
    if mode == "queue":
        return [item.id for item in queue_items]
    return [f"session:{item.id}" for item in sessions]


def _resolve_selected_item_id(
    *,
    mode: LeftPaneMode,
    requested: str | None,
    candidate_ids: list[str],
    selected_session_id: str | None,
    selected_orchestration_id: str | None,
) -> str | None:
    if requested in candidate_ids:
        return requested
    preferred = None
    if mode == "sessions" and selected_session_id:
        preferred = f"session:{selected_session_id}"
    elif mode == "orchestrations" and selected_orchestration_id:
        preferred = f"orchestration:{selected_orchestration_id}"
    if preferred in candidate_ids:
        return preferred
    return candidate_ids[0] if candidate_ids else None


def _left_pane_mode(value: object) -> LeftPaneMode:
    mode = str(value or "sessions").strip().casefold()
    return mode if mode in LEFT_PANE_MODES else "sessions"  # type: ignore[return-value]


def _filter_dataclass_items(items: list[Any], query: str) -> list[Any]:
    normalized = query.strip().casefold()
    if not normalized:
        return items
    return [item for item in items if normalized in " ".join(str(value or "") for value in asdict(item).values()).casefold()]


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _model_label(dashboard: dict[str, Any], state: dict[str, Any]) -> str:
    chat_cfg = dashboard.get("chat") or {}
    active_session = dashboard.get("active_session") or {}
    active_model = (dashboard.get("model_catalog") or {}).get("active_model") or {}
    return str(
        active_model.get("raw_model_ref")
        or active_session.get("raw_model_ref")
        or active_model.get("model_profile_id")
        or chat_cfg.get("default_model_profile")
        or state.get("model_profile")
        or "default"
    )


def _orchestration_ids_by_session(snapshot) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    task_by_id = {task.id: task for task in snapshot.tasks}
    objective_session = {objective.id: objective.session_id for objective in snapshot.objectives if objective.session_id}
    for objective_id, session_id in objective_session.items():
        if session_id:
            result[str(session_id)].add(str(objective_id))
    for task in snapshot.tasks:
        session_id = task.session_id or objective_session.get(task.objective_id or "")
        if session_id and task.objective_id:
            result[str(session_id)].add(str(task.objective_id))
    for session_id, active_task in [
        (task.session_id, task)
        for task in task_by_id.values()
        if task.session_id and task.objective_id
    ]:
        result[str(session_id)].add(str(active_task.objective_id))
    return result


def _session_attention(
    session: dict[str, Any],
    *,
    active_session: dict[str, Any],
    orchestration_ids: set[str],
    attention_by_orchestration: dict[Any, str | None],
) -> str | None:
    if session.get("status") == "waiting_approval":
        return "approval required"
    operator = active_session.get("operator") or {}
    if operator.get("waiting_approval_id"):
        return "approval required"
    for orchestration_id in orchestration_ids:
        attention = attention_by_orchestration.get(orchestration_id)
        if attention:
            return attention
    if session.get("active_task_id") or session.get("active_run_id"):
        return "active work"
    return None


def _session_nav_state(status: object, active: bool) -> NavState:
    raw = str(status or "idle")
    if raw == "waiting_approval":
        return "blocked"
    if raw in {"failed"}:
        return "failed"
    if raw in {"completed"}:
        return "completed"
    if raw in {"running"}:
        return "running"
    if active:
        return "active"
    if raw in {"active"}:
        return "active"
    return "idle"


def _orchestration_nav_state(state: object) -> Literal["ready", "running", "blocked", "failed", "completed"]:
    raw = str(state or "ready")
    if raw in {"running", "blocked", "failed", "completed"}:
        return raw  # type: ignore[return-value]
    return "ready"


def _active_agent_summary(instance: dict[str, Any], graph: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if not graph:
        agents = instance.get("assigned_agents") or []
        return (str(agents[0]), None) if agents else (None, None)
    selected_nodes = graph.get("active_node_ids") or graph.get("attention_node_ids") or []
    node_by_id = {node.get("id"): node for node in graph.get("nodes") or []}
    for node_id in selected_nodes:
        node = node_by_id.get(node_id)
        if not node or node.get("kind") != "task":
            continue
        metadata = node.get("metadata") or {}
        agent = metadata.get("agent_id") or metadata.get("workbench_id")
        if agent:
            action = "leased" if node.get("state") == "running" else status_label(node.get("state"))
            return str(agent), f"{agent}: {short_text(node.get('title'), limit=34)} ({action})"
    agents = instance.get("assigned_agents") or []
    return (str(agents[0]), None) if agents else (None, None)


def _graph_attention(graph: dict[str, Any] | None) -> str | None:
    if not graph:
        return None
    node_by_id = {node.get("id"): node for node in graph.get("nodes") or []}
    attention_nodes = [node_by_id[node_id] for node_id in graph.get("attention_node_ids") or [] if node_id in node_by_id]
    attention_nodes.sort(key=lambda node: {"approval_gate": 0, "blocker": 1, "adapter_run": 2, "verification": 3, "task": 4}.get(node.get("kind"), 5))
    for node in attention_nodes:
        if node.get("kind") == "approval_gate":
            return "approval required"
        if node.get("kind") == "blocker":
            return f"blocked: {humanize_identifier(node.get('title'))}"
        if node.get("kind") == "task":
            reason = _task_reason(graph, node)
            if reason:
                return reason
        if node.get("state") == "failed":
            return f"failed: {short_text(node.get('title'), limit=48)}"
        return f"attention: {short_text(node.get('title'), limit=48)}"
    if any(node.get("kind") == "artifact" for node in graph.get("nodes") or []):
        return "evidence available"
    return None


def _agent_state(nodes: list[dict[str, Any]]) -> Literal["idle", "ready", "running", "blocked", "failed"]:
    states = {str(node.get("state") or "idle") for node in nodes}
    if "failed" in states:
        return "failed"
    if "blocked" in states:
        return "blocked"
    if "running" in states:
        return "running"
    if "ready" in states:
        return "ready"
    return "idle"


def _current_agent_node(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    ranked = sorted(nodes, key=lambda node: _agent_rank(str(node.get("state") or "idle")))
    return ranked[0] if ranked else None


def _agent_rank(state: str) -> int:
    return {"failed": 0, "blocked": 1, "running": 2, "ready": 3, "idle": 4}.get(state, 5)


def _queue_rank(state: str) -> int:
    return {"blocked": 0, "failed": 1, "running": 2, "ready": 3}.get(state, 4)


def _task_reason(graph: dict[str, Any], task_node: dict[str, Any]) -> str | None:
    node_by_id = {node.get("id"): node for node in graph.get("nodes") or []}
    task_id = task_node.get("id")
    for edge in graph.get("edges") or []:
        if edge.get("target_node_id") != task_id:
            continue
        source = node_by_id.get(edge.get("source_node_id"))
        if not source:
            continue
        if source.get("kind") == "approval_gate":
            return "approval required"
        if source.get("kind") == "blocker":
            return f"blocked: {humanize_identifier(source.get('title'))}"
    if task_node.get("state") == "blocked":
        return "blocked"
    if task_node.get("state") == "running":
        metadata = task_node.get("metadata") or {}
        agent = metadata.get("agent_id") or metadata.get("workbench_id")
        return f"agent: {agent}" if agent else "leased"
    return None
