from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from rich.markup import escape

from harness.models import (
    CockpitTopBarView,
    GraphNode,
    LiveOrchestrationGraph,
    OrchestrationInstance,
    RightPaneCockpitModel,
)
from harness.orchestration_layout import compact_graph_rows, expanded_graph_rows
from harness.orchestration_projector import project_orchestration_graphs
from harness.orchestration_registry import OrchestrationGraphRegistry
from harness.orchestration_state import load_orchestration_state
from harness.orchestration_validate import display_artifact_path
from harness.tui_primitives import status_label as shared_status_label
from harness.tui_primitives import status_symbol


COCKPIT_SECTION_IDS = (
    "orchestrations",
    "active_work",
    "graph",
    "attention",
    "evidence",
    "context",
    "node_details",
    "shortcuts",
    "commands",
)
COCKPIT_FOOTER = "Ctrl+X O/G/E modes · Tab section · Enter details · ? shortcuts"
_REGISTRIES: dict[Path, OrchestrationGraphRegistry] = {}


def build_right_pane_cockpit_model(
    dashboard: dict[str, Any],
    view_state: dict[str, Any] | None,
    query: str,
    focus_mode: str,
) -> dict[str, Any]:
    state = dict(view_state or {})
    project_root = Path(str(dashboard.get("project_root") or ".")).resolve()
    registry = _registry_for(project_root)
    mode = _cockpit_mode(state.get("right_pane_mode") or state.get("cockpit_mode") or "overview")
    if focus_mode == "palette":
        mode = "overview"
    registry.apply_selection_hints(
        selected_orchestration_id=_optional_str(state.get("selected_orchestration_id")),
        selected_node_id=_optional_str(state.get("selected_node_id") or state.get("selected_graph_node_id")),
        pinned_orchestration_id=_optional_str(state.get("pinned_orchestration_id")),
        show_all_orchestrations=bool(state.get("show_all_orchestrations", registry.show_all_orchestrations)),
    )
    snapshot = load_orchestration_state(project_root)
    instances, graphs = project_orchestration_graphs(
        snapshot,
        selected_node_by_orchestration=registry.selected_node_by_orchestration(),
        previous_positions_by_orchestration=registry.positions_by_orchestration,
    )
    registry.apply_graphs(instances, graphs)
    selected_graph = _selected_graph(graphs, registry.selected_orchestration_id)
    selected_instance = _selected_instance(instances, registry.selected_orchestration_id)
    if selected_graph is not None and registry.selected_node_id:
        selected_graph = _mark_selected_node(selected_graph, registry.selected_node_id)
        graphs = [selected_graph if graph.orchestration_id == selected_graph.orchestration_id else graph for graph in graphs]
    active_work = _active_work_rows(dashboard, state, selected_instance, selected_graph)
    attention = _attention_rows(dashboard, selected_graph, active_work)
    evidence_rows = _evidence_rows(dashboard, selected_graph)
    sections = _sections_for_mode(
        mode=mode,
        dashboard=dashboard,
        state=state,
        query=query,
        focus_mode=focus_mode,
        instances=instances,
        graphs=graphs,
        selected_instance=selected_instance,
        selected_graph=selected_graph,
        active_work=active_work,
        attention=attention,
        evidence_rows=evidence_rows,
        show_all_orchestrations=registry.show_all_orchestrations,
    )
    sections = _apply_filter_and_commands(sections, state, query, focus_mode)
    active_section_id, active_index = _resolve_active_section(sections, state.get("active_section_id"), state.get("active_section_index"))
    collapsed_ids = set(_normalized_collapsed_sections(state.get("collapsed_section_ids")))
    for section in sections:
        section["collapsed"] = section["id"] in collapsed_ids
        section["match_count"] = len(section.get("rows") or [])
    active_signal = _active_signal(dashboard, state)
    summary = _summary(dashboard, instances, active_signal)
    model = RightPaneCockpitModel(
        mode=mode,
        focus_mode=focus_mode if focus_mode in {"dashboard", "palette"} else "dashboard",
        query=query.strip(),
        project=project_root.name or str(project_root),
        branch=str(dashboard.get("branch") or "unknown"),
        model_label=_model_label(dashboard, state),
        live_state=_status_label(active_signal),
        initialized=bool(dashboard.get("initialized")) and snapshot.initialized,
        orchestration_instances=instances,
        selected_orchestration_id=registry.selected_orchestration_id,
        pinned_orchestration_id=registry.pinned_orchestration_id,
        selected_node_id=registry.selected_node_id,
        top_bar=CockpitTopBarView(
            app_label="Harness",
            live=True,
            state=_status_label(active_signal),
            mode=mode,
            queue_ready=int(summary.get("ready", 0) or 0),
            queue_active=int(summary.get("running", 0) or 0),
            queue_blocked=int(summary.get("blocked", 0) or 0),
            project=project_root.name or str(project_root),
            branch=str(dashboard.get("branch") or "unknown"),
            model=_model_label(dashboard, state),
        ),
        graph=selected_graph,
        all_graphs=graphs,
        active_work=active_work,
        attention=attention,
        evidence_rows=evidence_rows,
        shortcuts_visible=bool(state.get("shortcuts_visible")),
        sections=sections,
        active_section_id=active_section_id,
        active_section_index=active_index,
        active_signal=active_signal,
        summary=summary,
        live_activity=dashboard.get("live_activity") or {},
        search={
            "context_matches": sum(len(section.get("rows") or []) for section in sections),
            "command_matches": _command_match_count(state.get("palette") or {}, query),
        },
        navigation_hints=[
            {"key": "1-9 switch", "label": ""},
            {"key": "Ctrl+X O/G/E modes", "label": ""},
            {"key": "Tab next", "label": ""},
            {"key": "Enter details", "label": ""},
            {"key": "? shortcuts", "label": ""},
        ],
        empty_state={
            "title": "No matches",
            "message": "No matches. Try /help, tasks, runs, adapters.",
            "query": query.strip(),
        }
        if not sections
        else None,
    )
    payload = model.model_dump(mode="json")
    payload["cockpit_schema_version"] = payload["schema_version"]
    payload["schema_version"] = "harness.tui_right_panel/v1"
    payload["collapsed_section_ids"] = sorted(collapsed_ids)
    payload["show_all_orchestrations"] = registry.show_all_orchestrations
    return payload


def render_right_pane_cockpit(model: dict[str, Any]) -> str:
    if model.get("empty_state"):
        return escape(str(model["empty_state"]["message"]))
    lines = [
    ]
    active_id = model.get("active_section_id")
    for section in model.get("sections") or []:
        if section.get("id") == "shortcuts" and not model.get("shortcuts_visible"):
            continue
        title = str(section.get("title") or "Section")
        marker = ">" if section.get("id") == active_id else " "
        if section.get("id") == active_id:
            lines.append(f"[bold reverse]{escape(marker + ' ' + title)}[/bold reverse]")
        else:
            lines.append(f"[bold steel_blue1]{escape(marker + ' ' + title.upper())}[/bold steel_blue1]")
        if section.get("collapsed"):
            lines.append("  [dim]- hidden[/dim]")
            lines.append("")
            continue
        for row in section.get("rows") or []:
            lines.append(_render_row(str(row)))
        lines.append("")
    lines.append(f"[dim]{escape(str(model.get('footer') or COCKPIT_FOOTER))}[/dim]")
    return "\n".join(lines).strip()


def render_right_pane_detail(model: dict[str, Any], section_id: str | None = None) -> str:
    if model.get("empty_state"):
        return escape(str(model["empty_state"]["message"]))
    selected = _selected_node_from_model(model)
    section = _section_for_detail(model, section_id)
    title = selected.get("title") if selected else section.get("title") if section else "Cockpit"
    lines = [
        f"[bold deep_sky_blue1]{escape(str(title))} detail[/bold deep_sky_blue1]",
        "[dim]Read-only persisted Harness projection. No command, provider, shell, Docker, adapter, filesystem, or permission action is started; no lease, approval, or adapter dispatch is started.[/dim]",
        "",
        f"[bold]Mode:[/bold] {escape(str(model.get('mode') or 'overview'))}",
        f"[bold]Signal:[/bold] {escape(_status_label(model.get('active_signal') or 'idle'))}",
        f"[bold]Selection:[/bold] {escape(str(model.get('selected_orchestration_id') or 'none'))}",
        "",
    ]
    detail_rows = selected.get("detail_rows") if selected else None
    if detail_rows:
        for row in detail_rows:
            lines.append(_render_row(str(row)))
    elif section:
        for row in section.get("rows") or []:
            lines.append(_render_row(str(row)))
    else:
        lines.append(_render_row("No details available."))
    lines.extend(
        [
            "",
            "[bold]Boundary:[/bold] read-only cockpit",
            "  [dim]-[/dim] process=False fs=False shell=False docker=False adapter=False perm=False approval=False",
        ]
    )
    return "\n".join(lines).strip()


def render_right_pane_status(model: dict[str, Any]) -> str:
    focus_mode = str(model.get("focus_mode") or "dashboard")
    if focus_mode == "palette":
        top_bar = dict(model.get("top_bar") or {})
        matches = (model.get("search") or {}).get("command_matches", 0)
        return _top_bar_line_one({**top_bar, "mode": "palette"}, suffix=f"{matches} commands")
    return _top_bar_line_one(model.get("top_bar") or {})


def render_right_pane_top_context(model: dict[str, Any]) -> str:
    return _top_bar_line_two(model.get("top_bar") or {})


def reset_right_pane_registries() -> None:
    _REGISTRIES.clear()


def _registry_for(project_root: Path) -> OrchestrationGraphRegistry:
    resolved = project_root.resolve()
    registry = _REGISTRIES.get(resolved)
    if registry is None:
        registry = OrchestrationGraphRegistry()
        _REGISTRIES[resolved] = registry
    return registry


def _sections_for_mode(
    *,
    mode: str,
    dashboard: dict[str, Any],
    state: dict[str, Any],
    query: str,
    focus_mode: str,
    instances: list[OrchestrationInstance],
    graphs: list[LiveOrchestrationGraph],
    selected_instance: OrchestrationInstance | None,
    selected_graph: LiveOrchestrationGraph | None,
    active_work: dict[str, Any],
    attention: list[str],
    evidence_rows: list[str],
    show_all_orchestrations: bool,
) -> list[dict[str, Any]]:
    if mode == "graph":
        graph_rows = _all_graph_rows(instances, graphs) if show_all_orchestrations else _graph_rows(selected_graph, expanded=True)
        return [
            {"id": "orchestrations", "title": "Orchestrations", "rows": _orchestration_rows(instances, selected_instance)},
            {"id": "graph", "title": "Graph", "rows": graph_rows},
            {"id": "node_details", "title": "Node Details", "rows": _selected_node_rows(selected_graph)},
            {"id": "attention", "title": "Attention", "rows": attention},
            {"id": "shortcuts", "title": "Shortcuts", "rows": _shortcut_rows()},
        ]
    if mode == "evidence":
        return [
            {"id": "evidence", "title": "Evidence", "rows": evidence_rows},
            {"id": "node_details", "title": "Node Details", "rows": _selected_node_rows(selected_graph)},
            {"id": "graph", "title": "Graph", "rows": _graph_rows(selected_graph, expanded=False)},
            {"id": "shortcuts", "title": "Shortcuts", "rows": _shortcut_rows()},
        ]
    return [
        {"id": "orchestrations", "title": "Orchestrations", "rows": _orchestration_rows(instances, selected_instance)},
        {"id": "active_work", "title": "Active Work", "rows": list(active_work.get("rows") or [])},
        {"id": "graph", "title": "Graph", "rows": _graph_rows(selected_graph, expanded=False)},
        {"id": "attention", "title": "Attention", "rows": attention},
        {"id": "evidence", "title": "Evidence", "rows": evidence_rows},
        {"id": "context", "title": "Context", "rows": _context_rows(dashboard, state, focus_mode, query)},
        {"id": "shortcuts", "title": "Shortcuts", "rows": _shortcut_rows()},
    ]


def _orchestration_rows(instances: list[OrchestrationInstance], selected: OrchestrationInstance | None) -> list[str]:
    if not instances:
        return ["No assigned orchestrations.", "Next: create or select an objective"]
    rows = []
    for index, instance in enumerate(instances[:9], start=1):
        selected_marker = ">" if selected is not None and instance.orchestration_id == selected.orchestration_id else " "
        rows.append(f"{index} {selected_marker} {_instance_symbol(instance.state)} {instance.title}  {instance.state}")
    if len(instances) > 9:
        rows.append(f"... {len(instances) - 9} more orchestrations")
    return rows


def _active_work_rows(
    dashboard: dict[str, Any],
    state: dict[str, Any],
    selected_instance: OrchestrationInstance | None,
    selected_graph: LiveOrchestrationGraph | None,
) -> dict[str, Any]:
    rows: list[str] = []
    signal = _active_signal(dashboard, state)
    contract = state.get("pending_action_contract")
    if contract:
        rows.extend(
            [
                "State: needs confirmation",
                f"Pending: {_short(contract.get('summary'))}",
                f"Tool: {_humanize(contract.get('tool'))}",
                f"Risk: {_humanize(contract.get('risk'))}",
                "Next: choose confirm or decline from the decision menu",
            ]
        )
        return {"rows": rows, "next_action": "confirm or decline from the decision menu"}
    pending_permissions = (dashboard.get("live_activity") or {}).get("pending_permissions") or []
    if pending_permissions:
        permission = pending_permissions[-1]
        target = _short(permission.get("target"), limit=64)
        rows.extend(
            [
                "State: approval needed",
                f"Permission: {_humanize(permission.get('tool_id'))} {_humanize(permission.get('action'))}",
                f"Target: {target}",
                f"Risk: {_humanize(permission.get('risk') or 'unknown')}",
                "Next: choose allow or deny from the permission menu",
            ]
        )
        return {"rows": rows, "next_action": "allow or deny from the permission menu"}
    task_node = _selected_or_active_task_node(selected_graph)
    instance_title = selected_instance.title if selected_instance is not None else _fallback_objective_title(dashboard)
    rows.append(f"Orchestration: {instance_title}")
    blocker, blocked_task_id = _dashboard_blocker(dashboard)
    if signal == "blocked" and blocker:
        rows.extend(
            [
                "State: blocked",
                f"Blocker: {blocker}",
                f"Work: {_task_title(dashboard, blocked_task_id) if blocked_task_id else 'selected work'}",
                "Next: inspect progress details",
            ]
        )
    elif task_node is not None:
        rows.extend(_task_node_active_rows(task_node))
    elif dashboard.get("active_leases"):
        lease = dashboard["active_leases"][0]
        rows.extend(["Task: " + _task_title(dashboard, lease.get("task_id")), "State: running", "Next: inspect details"])
    elif _ready_task(dashboard) is not None:
        task = _ready_task(dashboard)
        next_line = "Next: lease the next task"
        then_line = None
        if task.get("execution_adapter") == "repo_planning":
            next_line = "Next: Lease the next planning task"
            then_line = "Then: Dispatch the lease after review"
        rows.extend(
            [
                f"Task: {task.get('title') or 'Ready task'}",
                f"Adapter: {task.get('execution_adapter') or 'none'}",
                "State: ready",
                next_line,
            ]
        )
        if then_line:
            rows.append(then_line)
    elif not dashboard.get("initialized"):
        rows.extend(["Task: none", "State: needs setup", "Next: /init"])
    else:
        rows.extend(["Task: none", "State: idle", "Next: ask Harness or inspect progress"])
    activation = state.get("latest_palette_activation") or {}
    if activation:
        rows.append(f"Last UI: {_humanize(activation.get('entry_id') or 'updated')}")
    latest_response = state.get("latest_response") or {}
    if latest_response.get("kind") == "self_managed_local_action":
        report = latest_response.get("report_path") or (latest_response.get("extra") or {}).get("report_path")
        rows.append("Latest action: succeeded" if latest_response.get("ok") else "Latest action: failed")
        rows.extend(_managed_action_target_rows(dashboard, latest_response))
        decision = latest_response.get("decision") or {}
        sandbox = decision.get("sandbox_assessment") or {}
        if decision or sandbox:
            rows.append(
                "Policy: "
                f"{_humanize(decision.get('status') or 'unknown')} / "
                f"sandbox {_humanize(sandbox.get('status') or 'not recorded')}"
            )
        if report:
            rows.append(f"Report: {Path(str(report)).name}")
    next_action = next((row.removeprefix("Next: ") for row in rows if row.startswith("Next: ")), None)
    return {"rows": rows, "next_action": next_action}


def _task_node_active_rows(node: GraphNode | dict[str, Any]) -> list[str]:
    data = node.model_dump(mode="json") if isinstance(node, GraphNode) else dict(node)
    rows = [
        f"Task: {data.get('title') or 'selected task'}",
        f"State: {data.get('state') or 'unknown'}",
    ]
    metadata = data.get("metadata") or {}
    if metadata.get("agent_id") or metadata.get("workbench_id"):
        rows.append(f"Agent: {metadata.get('agent_id') or metadata.get('workbench_id')}")
    if metadata.get("execution_adapter"):
        rows.append(f"Adapter: {metadata['execution_adapter']}")
    rows.append("Next: inspect progress details" if data.get("attention") else "Next: inspect node details")
    return rows


def _attention_rows(dashboard: dict[str, Any], graph: LiveOrchestrationGraph | None, active_work: dict[str, Any]) -> list[str]:
    pending_permissions = (dashboard.get("live_activity") or {}).get("pending_permissions") or []
    if pending_permissions:
        permission = pending_permissions[-1]
        return [f"Approval required: {_humanize(permission.get('tool_id'))} {_humanize(permission.get('action'))}"]
    if graph is not None:
        node_by_id = {node.id: node for node in graph.nodes}
        attention_nodes = [node_by_id[node_id] for node_id in graph.attention_node_ids if node_id in node_by_id]
        attention_nodes.sort(key=lambda node: {"approval_gate": 0, "blocker": 1, "adapter_run": 2, "verification": 3, "task": 4}.get(node.kind, 5))
        for node in attention_nodes:
            if node.kind == "approval_gate":
                return [f"Approval gate: {node.title}"]
            if node.kind == "blocker":
                return [f"Blocked: {node.title}"]
            if node.state == "failed":
                return [f"Failed: {node.title}"]
            return [f"Attention: {node.title}"]
    next_action = active_work.get("next_action")
    if next_action:
        return [f"Next: {next_action}"]
    return ["No blockers requiring attention."]


def _evidence_rows(dashboard: dict[str, Any], graph: LiveOrchestrationGraph | None) -> list[str]:
    rows: list[str] = []
    if graph is not None:
        for node in graph.nodes:
            if node.kind == "adapter_run":
                adapter = (node.metadata or {}).get("execution_adapter") or "unknown"
                rows.append(f"Run: {node.title} | adapter={adapter} | state={node.state}")
            elif node.kind == "artifact":
                metadata = node.metadata or {}
                sha = str(metadata.get("sha256") or "")[:12] or "unknown"
                rows.append(
                    "Artifact: "
                    f"{metadata.get('artifact_id') or node.entity_id or 'artifact'} | "
                    f"{metadata.get('kind') or node.title} | "
                    f"{metadata.get('path_display') or 'path redacted'} | "
                    f"hash={sha} | redaction={metadata.get('redaction_state') or 'unknown'} | "
                    f"evidence={metadata.get('evidence_status') or 'unknown'}"
                )
            elif node.kind == "verification":
                rows.append(f"Verification: {node.title} | state={node.state}")
    live_activity = dashboard.get("live_activity") or {}
    for artifact in live_activity.get("recent_artifacts") or []:
        path = Path(str(artifact.get("path") or "artifact"))
        rows.append(
            "Recent artifact: "
            f"{_humanize(artifact.get('kind'))} | "
            f"{display_artifact_path(path)} | "
            f"{_humanize(artifact.get('evidence_status') or 'unknown')}"
        )
    for run in dashboard.get("recent_runs") or []:
        rows.append(f"Recent run: {_humanize(run.get('task_type') or 'unknown')} | {run.get('status') or 'unknown'}")
    return rows[:8] or ["No run, artifact, or verification metadata yet."]


def _context_rows(dashboard: dict[str, Any], state: dict[str, Any], focus_mode: str, query: str) -> list[str]:
    active_session = dashboard.get("active_session") or {}
    model_catalog = dashboard.get("model_catalog") or {}
    active_model = model_catalog.get("active_model") or {}
    session_tools = dashboard.get("session_tools") or {}
    planning_mode = session_tools.get("planning_mode") or {}
    rows = [
        f"Project: {Path(str(dashboard.get('project_root') or '.')).name}",
        f"Branch: {dashboard.get('branch') or 'unknown'}",
        f"Model: {_model_label(dashboard, state)}",
        f"Model source: {_humanize(active_model.get('selection_source') or 'unresolved')}",
        f"Context window: {_context_window_label(active_session, active_model)}",
        f"Assistant: {_humanize(state.get('active_orchestrator') or 'default')}",
        f"Mode: {_status_label(state.get('chat_mode') or 'normal')}",
        f"Plan mode: {'active' if planning_mode.get('active') else 'inactive'}",
        f"Research: search={'ready' if session_tools.get('web_search_enabled') else 'blocked'} fetch={'ready' if session_tools.get('web_fetch_enabled') else 'blocked'}",
        f"Active: {_session_title(active_session) if active_session else 'none'}",
        f"Session: {_session_title(active_session) if active_session else 'none'}",
        f"Queue: {_queue_summary(dashboard)}",
        "Surface: read-only cockpit",
        "Fallback: explicit failure only",
    ]
    if active_model.get("blocked_reasons"):
        rows.append("Blocked: " + ", ".join(_humanize(reason) for reason in active_model["blocked_reasons"]))
    timeline = active_session.get("timeline") or []
    if timeline:
        rows.append(f"Latest: {_short(timeline[-1])}")
    transcript = active_session.get("transcript") or []
    if transcript:
        rows.append(f"Last message: {_short(transcript[-1])}")
    latest_response = state.get("latest_response") or {}
    tool_results = latest_response.get("tool_results") or []
    if tool_results:
        rows.append("Tools: " + ", ".join(str(item.get("tool")) for item in tool_results[:4]))
    manifest = latest_response.get("context_manifest") or {}
    blocks = manifest.get("blocks") or []
    if blocks:
        rows.append("Context: " + ", ".join(str(block.get("kind")) for block in blocks[:4]))
    if focus_mode == "palette" and query.strip():
        rows.append("Palette: command previews only")
    return rows


def _managed_action_target_rows(dashboard: dict[str, Any], latest_response: dict[str, Any]) -> list[str]:
    rows = []
    project_root = Path(str(dashboard.get("project_root") or ".")).resolve()
    created = _response_path_list(latest_response.get("created_paths"), project_root)
    changed = _response_path_list(latest_response.get("changed_paths"), project_root)
    if created:
        rows.append("Created: " + ", ".join(created))
    if changed:
        rows.append("Changed: " + ", ".join(changed))
    return rows


def _response_path_list(value: object, project_root: Path) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:3]:
        text = str(item or "").strip()
        if not text:
            continue
        path = Path(text)
        if path.is_absolute():
            try:
                result.append(path.resolve().relative_to(project_root).as_posix())
            except ValueError:
                result.append(path.name)
        else:
            result.append(path.as_posix())
    if len(value) > 3:
        result.append(f"+{len(value) - 3} more")
    return result


def _context_window_label(active_session: dict[str, Any], active_model: dict[str, Any]) -> str:
    composer_context = active_session.get("composer_context") or {}
    estimated_tokens = _safe_int(composer_context.get("total_estimated_tokens"))
    context_limit = _safe_int(active_model.get("context_limit"))
    if context_limit <= 0:
        return "unknown"
    percent = (estimated_tokens / context_limit) * 100 if context_limit else 0
    if estimated_tokens > 0 and percent < 1:
        percent_label = "<1%"
    else:
        percent_label = f"{percent:.0f}%"
    return f"{percent_label} used"


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _graph_rows(graph: LiveOrchestrationGraph | None, *, expanded: bool) -> list[str]:
    if graph is None:
        return ["No graph yet.", "Graph is projected from objectives, tasks, runs, and artifacts."]
    return expanded_graph_rows(graph) if expanded else compact_graph_rows(graph)


def _all_graph_rows(instances: list[OrchestrationInstance], graphs: list[LiveOrchestrationGraph]) -> list[str]:
    if not graphs:
        return ["No assigned orchestrations."]
    graph_by_id = {graph.orchestration_id: graph for graph in graphs}
    rows: list[str] = []
    for instance in instances[:6]:
        rows.append(f"{_instance_symbol(instance.state)} {instance.title}")
        graph = graph_by_id.get(instance.orchestration_id)
        if graph is not None:
            for row in compact_graph_rows(graph, limit=3):
                rows.append(f"  {row}")
    return rows


def _selected_node_rows(graph: LiveOrchestrationGraph | None) -> list[str]:
    if graph is None:
        return ["No selected node."]
    node = _selected_node(graph)
    if node is None:
        return ["No selected node."]
    return node.detail_rows or [f"{node.kind}: {node.title}", f"State: {node.state}"]


def _shortcut_rows() -> list[str]:
    return [
        "1-9: switch orchestration",
        "Ctrl+X O/G/E: overview, graph, evidence",
        "G in graph: toggle selected/all orchestrations",
        "Tab/Shift+Tab: switch section",
        "F: focus attention node",
        "Space: pin selected orchestration",
        "Enter: open read-only details",
    ]


def _apply_filter_and_commands(
    sections: list[dict[str, Any]],
    state: dict[str, Any],
    query: str,
    focus_mode: str,
) -> list[dict[str, Any]]:
    normalized = query.strip().casefold()
    palette = state.get("palette") or {}
    command_rows = _command_rows(palette, query, focus_mode=focus_mode)
    if focus_mode == "palette" or normalized:
        sections = [*sections, {"id": "commands", "title": "Commands", "rows": command_rows or ["No matching commands."]}]
    if normalized and focus_mode != "palette":
        filtered: list[dict[str, Any]] = []
        for section in sections:
            title = str(section.get("title") or "")
            rows = [row for row in section.get("rows") or [] if normalized in str(row).casefold()]
            if normalized in title.casefold() and not rows:
                rows = list(section.get("rows") or [])
            if rows or normalized in title.casefold():
                filtered.append({**section, "rows": rows})
        return filtered
    return sections


def _command_rows(palette: dict[str, Any], query: str, *, focus_mode: str) -> list[str]:
    value = query.strip().casefold()
    if focus_mode != "palette" and not value:
        return []
    rows = []
    for entry in palette.get("entries") or []:
        haystack = " ".join(str(entry.get(key) or "") for key in ("title", "command", "description", "id")).casefold()
        if value and value not in haystack:
            continue
        rows.append(f"{entry.get('title') or entry.get('id')} | {entry.get('command') or entry.get('id')}")
        if len(rows) >= 8:
            break
    return rows


def _command_match_count(palette: dict[str, Any], query: str) -> int:
    value = query.strip().casefold()
    if not value:
        return 0
    return sum(
        1
        for entry in palette.get("entries") or []
        if value in " ".join(str(entry.get(key) or "") for key in ("title", "command", "description", "id")).casefold()
    )


def _resolve_active_section(
    sections: list[dict[str, Any]],
    requested: object,
    requested_index: object,
) -> tuple[str | None, int]:
    if not sections:
        return None, 0
    section_ids = [str(section.get("id")) for section in sections]
    requested_id = _section_alias(requested)
    if requested_id in section_ids:
        return requested_id, section_ids.index(requested_id)
    try:
        index = int(requested_index or 0)
    except (TypeError, ValueError):
        index = 0
    index = index % len(sections)
    return section_ids[index], index


def _normalized_collapsed_sections(value: object) -> list[str]:
    if not value:
        return []
    result = []
    for item in value:
        section_id = _section_alias(item)
        if section_id in COCKPIT_SECTION_IDS and section_id not in result:
            result.append(section_id)
    return result


def _section_alias(value: object) -> str:
    raw = str(value or "").strip()
    aliases = {
        "action": "active_work",
        "now": "context",
        "sessions": "context",
        "progress": "active_work",
        "queue": "orchestrations",
        "recent": "evidence",
        "adapters": "context",
        "project": "context",
        "assistant": "context",
        "next": "attention",
        "queue_daemon": "orchestrations",
        "runtime_evidence": "evidence",
        "agents_specs": "context",
        "planning_research": "context",
        "settings": "context",
        "safety": "attention",
    }
    return aliases.get(raw, raw if raw in COCKPIT_SECTION_IDS else "active_work")


def _mark_selected_node(graph: LiveOrchestrationGraph, selected_node_id: str) -> LiveOrchestrationGraph:
    if selected_node_id not in {node.id for node in graph.nodes}:
        return graph
    return graph.model_copy(update={"selected_node_id": selected_node_id})


def _selected_graph(graphs: list[LiveOrchestrationGraph], selected_id: str | None) -> LiveOrchestrationGraph | None:
    if selected_id:
        graph = next((item for item in graphs if item.orchestration_id == selected_id), None)
        if graph is not None:
            return graph
    return graphs[0] if graphs else None


def _selected_instance(instances: list[OrchestrationInstance], selected_id: str | None) -> OrchestrationInstance | None:
    if selected_id:
        instance = next((item for item in instances if item.orchestration_id == selected_id), None)
        if instance is not None:
            return instance
    return instances[0] if instances else None


def _selected_or_active_task_node(graph: LiveOrchestrationGraph | None) -> GraphNode | None:
    if graph is None:
        return None
    node = _selected_node(graph)
    if node is not None and node.kind == "task":
        return node
    node_by_id = {item.id: item for item in graph.nodes}
    for node_id in graph.active_node_ids + graph.attention_node_ids:
        node = node_by_id.get(node_id)
        if node is not None and node.kind == "task":
            return node
    return next((item for item in graph.nodes if item.kind == "task"), None)


def _selected_node(graph: LiveOrchestrationGraph) -> GraphNode | None:
    if graph.selected_node_id:
        node = next((item for item in graph.nodes if item.id == graph.selected_node_id), None)
        if node is not None:
            return node
    return graph.nodes[0] if graph.nodes else None


def _selected_node_from_model(model: dict[str, Any]) -> dict[str, Any] | None:
    graph = model.get("graph") or {}
    nodes = graph.get("nodes") or []
    selected = model.get("selected_node_id") or graph.get("selected_node_id")
    if selected:
        node = next((item for item in nodes if item.get("id") == selected), None)
        if node is not None:
            return node
    return nodes[0] if nodes else None


def _section_for_detail(model: dict[str, Any], section_id: str | None) -> dict[str, Any] | None:
    active = _section_alias(section_id or model.get("active_section_id"))
    return next((item for item in model.get("sections") or [] if item.get("id") == active), None)


def _active_signal(dashboard: dict[str, Any], state: dict[str, Any]) -> str:
    if state.get("request_in_flight"):
        return "responding"
    return str((dashboard.get("live_activity") or {}).get("active_signal") or "idle")


def _summary(dashboard: dict[str, Any], instances: list[OrchestrationInstance], signal: str) -> dict[str, Any]:
    live_counts = (dashboard.get("live_activity") or {}).get("counts") or {}
    return {
        "initialized": bool(dashboard.get("initialized")),
        "orchestrations": len(instances),
        "tasks_total": (dashboard.get("summary") or {}).get("tasks_total", 0),
        "active_leases": (dashboard.get("summary") or {}).get("active_leases", 0),
        "recent_runs": (dashboard.get("summary") or {}).get("recent_runs", 0),
        "active_signal": signal,
        "ready": live_counts.get("ready", 0),
        "running": live_counts.get("running", 0),
        "blocked": live_counts.get("blocked", 0),
        "waiting_approval": live_counts.get("waiting_approval", 0),
    }


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


def _header(model: dict[str, Any]) -> str:
    return _top_bar_line_one(model.get("top_bar") or {})


def _tabs(mode: str) -> str:
    labels = [("overview", "1 Overview"), ("graph", "2 Graph"), ("evidence", "3 Evidence")]
    return "  ".join(f"[bold][{label}][/bold]" if key == mode else f"[dim]{label}[/dim]" for key, label in labels)


def _top_bar_line_one(top_bar: dict[str, Any], *, suffix: str | None = None) -> str:
    app_label = str(top_bar.get("app_label") or "Harness")
    state = str(top_bar.get("state") or "idle")
    ready = int(top_bar.get("queue_ready") or 0)
    active = int(top_bar.get("queue_active") or 0)
    blocked = int(top_bar.get("queue_blocked") or 0)
    live_label = "live ●" if top_bar.get("live", True) else "idle ○"
    queue = f"Q {ready}R/{active}A/{blocked}B"
    tail = f"   {suffix}" if suffix else ""
    return (
        f"[bold deep_sky_blue1]{escape(app_label)}[/bold deep_sky_blue1]  "
        f"[dim]{escape(live_label)}[/dim] {escape(state)}   "
        f"{escape(queue)}{escape(tail)}"
    )


def _top_bar_line_two(top_bar: dict[str, Any]) -> str:
    project = str(top_bar.get("project") or "project")
    branch = str(top_bar.get("branch") or "unknown")
    model = str(top_bar.get("model") or "default")
    mode = str(top_bar.get("mode") or "overview")
    return f"{escape(project)} · {escape(branch)} · {escape(model)}  {_tabs(mode)}"


def _render_row(row: str) -> str:
    label, sep, value = row.partition(":")
    if sep and 1 <= len(label) <= 28 and re.match(r"^[A-Za-z0-9 _./-]+$", label):
        return f"  [dim]-[/dim] [bold]{escape(label)}: {escape(value.strip())}[/bold]"
    return f"  [dim]-[/dim] {escape(row)}"


def _instance_symbol(state: str) -> str:
    return status_symbol(state)


def _status_label(value: object) -> str:
    return shared_status_label(value)


def _humanize(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    text = re.sub(r"[_./-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or "unknown"


def _short(value: object, *, limit: int = 88) -> str:
    text = str(value or "").splitlines()[0].strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _task_title(dashboard: dict[str, Any], task_id: object) -> str:
    for task in dashboard.get("tasks") or []:
        if task.get("id") == task_id:
            return str(task.get("title") or "selected task")
    return "selected task"


def _ready_task(dashboard: dict[str, Any]) -> dict[str, Any] | None:
    return next((task for task in dashboard.get("tasks") or [] if task.get("status") == "ready"), None)


def _dashboard_blocker(dashboard: dict[str, Any]) -> tuple[str | None, str | None]:
    progress = dashboard.get("progress") or {}
    for task in progress.get("tasks") or []:
        blocked = task.get("blocked_state_explanations") or []
        if blocked:
            return str(blocked[0].get("code") or "blocked"), str(task.get("task_id") or "")
        reasons = task.get("blocked_reasons") or []
        if reasons:
            return str(reasons[0]), str(task.get("task_id") or "")
    reasons = progress.get("blocked_reasons") or []
    if reasons:
        return str(reasons[0]), None
    return None, None


def _fallback_objective_title(dashboard: dict[str, Any]) -> str:
    progress = dashboard.get("progress") or {}
    return str(progress.get("objective_title") or "Project queue")


def _queue_summary(dashboard: dict[str, Any]) -> str:
    counts = dashboard.get("task_status_counts") or {}
    ready = counts.get("ready", 0)
    running = counts.get("leased", 0) + counts.get("running", 0)
    blocked = counts.get("blocked", 0) + counts.get("waiting_approval", 0)
    return f"{ready} ready / {running} running / {blocked} blocked"


def _session_title(session: dict[str, Any]) -> str:
    return str(session.get("display_title") or session.get("title") or session.get("intent") or "Untitled session")


def _cockpit_mode(value: object) -> str:
    mode = str(value or "overview").casefold()
    return mode if mode in {"overview", "graph", "evidence"} else "overview"


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
