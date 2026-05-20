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
    header = "  ".join(f"[{lane.title[:12]}]" for lane in lanes[:5])
    rows = [header]
    lane_order = {lane.id: lane.row for lane in lanes}
    nodes = sorted(graph.nodes, key=lambda node: (lane_order.get(node.lane_id or "", 999), (node.row or 0), node.id))
    for node in nodes[:limit]:
        rows.append(f"{node.symbol or _symbol_for_state(node.state)} {node.title}  ({node.kind})")
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
