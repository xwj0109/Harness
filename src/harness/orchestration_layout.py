from __future__ import annotations

from harness.models import AgentLane, GraphNode, LiveOrchestrationGraph


GraphPositionMap = dict[str, tuple[str | None, int | None]]


def assign_stable_graph_layout(
    graph: LiveOrchestrationGraph,
    previous_positions: GraphPositionMap | None = None,
) -> LiveOrchestrationGraph:
    previous_positions = previous_positions or {}
    lanes = _stable_lanes(graph.lanes)
    lane_ids = [lane.id for lane in lanes]
    used_rows: dict[str | None, set[int]] = {lane_id: set() for lane_id in lane_ids}
    used_rows.setdefault(None, set())
    laid_out: list[GraphNode] = []
    for node in graph.nodes:
        previous_lane, previous_row = previous_positions.get(node.id, (None, None))
        lane_id = node.lane_id or previous_lane or _lane_for_node(node, lane_ids)
        if lane_id not in used_rows:
            lane_id = lane_ids[-1] if lane_ids else None
        row = previous_row if previous_row is not None else _next_row(used_rows.setdefault(lane_id, set()))
        if row in used_rows.setdefault(lane_id, set()):
            row = _next_row(used_rows[lane_id])
        used_rows[lane_id].add(row)
        laid_out.append(node.model_copy(update={"lane_id": lane_id, "row": row}))
    return graph.model_copy(update={"lanes": lanes, "nodes": laid_out})


def graph_position_map(graph: LiveOrchestrationGraph) -> GraphPositionMap:
    return {node.id: (node.lane_id, node.row) for node in graph.nodes}


def compact_graph_rows(graph: LiveOrchestrationGraph, *, limit: int = 7) -> list[str]:
    lanes_by_id = {lane.id: lane for lane in graph.lanes}
    lane_order = {lane.id: lane.row for lane in graph.lanes}
    nodes = sorted(graph.nodes, key=lambda node: (lane_order.get(node.lane_id or "", 999), (node.row or 0), node.id))
    rows = []
    for node in nodes[:limit]:
        lane = lanes_by_id.get(node.lane_id or "")
        lane_title = (lane.title if lane is not None else "Work")[:14].ljust(14)
        rows.append(f"{lane_title} {node.symbol or _symbol_for_state(node.state)} {node.title}")
    if len(nodes) > limit:
        rows.append(f"... {len(nodes) - limit} more nodes")
    return rows or ["No graph nodes yet."]


def expanded_graph_rows(graph: LiveOrchestrationGraph, *, limit: int = 12) -> list[str]:
    lanes = graph.lanes or [AgentLane(id="work", title="Work", row=0)]
    visible_lanes = lanes[:4]
    lane_width = 18
    header = "  ".join(_fit(lane.title.upper(), lane_width) for lane in visible_lanes)
    divider = "  ".join("-" * lane_width for _ in visible_lanes)
    rows = [header, divider]
    lane_index = {lane.id: index for index, lane in enumerate(visible_lanes)}
    lane_order = {lane.id: lane.row for lane in lanes}
    nodes = sorted(graph.nodes, key=lambda node: (lane_order.get(node.lane_id or "", 999), (node.row or 0), node.id))
    visible_nodes = nodes[:limit]
    row_count = max((node.row or 0 for node in visible_nodes), default=-1) + 1
    for row_index in range(row_count):
        cells = [" " * lane_width for _ in visible_lanes]
        for node in visible_nodes:
            if (node.row or 0) != row_index:
                continue
            lane = node.lane_id or visible_lanes[-1].id
            column = lane_index.get(lane)
            if column is None:
                continue
            cells[column] = _node_cell(node, lane_width, selected=node.id == graph.selected_node_id)
        if any(cell.strip() for cell in cells):
            rows.append("  ".join(cells))
    edge_rows = _edge_rows(graph, visible_nodes, limit=max(3, limit // 2))
    if edge_rows:
        rows.append("Flow:")
        rows.extend(edge_rows)
    if len(nodes) > limit:
        rows.append(f"... {len(nodes) - limit} more nodes")
    return rows


def _stable_lanes(lanes: list[AgentLane]) -> list[AgentLane]:
    if not lanes:
        return [AgentLane(id="orchestrator", title="Orchestrator", row=0), AgentLane(id="unassigned", title="Work", row=1)]
    return [lane.model_copy(update={"row": index}) for index, lane in enumerate(lanes)]


def _lane_for_node(node: GraphNode, lane_ids: list[str]) -> str | None:
    if node.kind == "objective" and "orchestrator" in lane_ids:
        return "orchestrator"
    if node.kind in {"approval_gate", "blocker"} and "gate" in lane_ids:
        return "gate"
    if node.kind == "artifact" and "evidence" in lane_ids:
        return "evidence"
    return lane_ids[-1] if lane_ids else None


def _next_row(used: set[int]) -> int:
    row = 0
    while row in used:
        row += 1
    return row


def _symbol_for_state(state: str) -> str:
    return {
        "running": "●",
        "completed": "✓",
        "ready": "○",
        "waiting": "○",
        "blocked": "■",
        "failed": "!",
    }.get(state, "○")


def _node_cell(node: GraphNode, width: int, *, selected: bool) -> str:
    selected_marker = ">" if selected else " "
    attention = "!" if node.attention else " "
    symbol = node.symbol or _symbol_for_state(node.state)
    title = _fit(node.title, width - 6)
    return _fit(f"{selected_marker}{symbol}{attention} {title}", width)


def _edge_rows(graph: LiveOrchestrationGraph, nodes: list[GraphNode], *, limit: int) -> list[str]:
    visible_ids = {node.id for node in nodes}
    title_by_id = {node.id: node.title for node in graph.nodes}
    rows: list[str] = []
    priority = {
        "requires_approval": 0,
        "blocked_by": 1,
        "depends_on": 2,
        "dispatches": 3,
        "produces": 4,
        "consumes": 5,
        "verifies": 6,
        "contains": 7,
    }
    edges = sorted(graph.edges, key=lambda edge: (priority.get(edge.kind, 99), edge.id))
    for edge in edges:
        if edge.source_node_id not in visible_ids or edge.target_node_id not in visible_ids:
            continue
        source = _fit(title_by_id.get(edge.source_node_id, edge.source_node_id), 16)
        target = _fit(title_by_id.get(edge.target_node_id, edge.target_node_id), 16)
        rows.append(f"  {source} -> {target}  {edge.kind}")
        if len(rows) >= limit:
            break
    return rows


def _fit(value: str, width: int) -> str:
    text = " ".join(str(value).split())
    if len(text) > width:
        return text[: max(0, width - 1)] + "…"
    return text.ljust(width)
