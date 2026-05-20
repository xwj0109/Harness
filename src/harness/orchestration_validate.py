from __future__ import annotations

from pathlib import Path

from harness.models import GraphEdge, LiveOrchestrationGraph
from harness.security import is_secret_path


FORBIDDEN_DISPLAY_PATH_PARTS = {".harness", ".git", "secrets"}


def is_display_safe_path(path: Path) -> bool:
    if is_secret_path(path):
        return False
    return not set(path.parts).intersection(FORBIDDEN_DISPLAY_PATH_PARTS)


def display_artifact_path(path: Path) -> str:
    if not is_display_safe_path(path):
        return "path redacted"
    return path.name or "artifact"


def validate_live_orchestration_graph(graph: LiveOrchestrationGraph) -> LiveOrchestrationGraph:
    node_ids = {node.id for node in graph.nodes}
    for edge in graph.edges:
        if edge.source_node_id not in node_ids:
            raise ValueError(f"Graph edge {edge.id} references unknown source node {edge.source_node_id}")
        if edge.target_node_id not in node_ids:
            raise ValueError(f"Graph edge {edge.id} references unknown target node {edge.target_node_id}")
    _reject_cycles(graph.edges, node_ids)
    return graph


def _reject_cycles(edges: list[GraphEdge], node_ids: set[str]) -> None:
    outgoing: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for edge in edges:
        outgoing.setdefault(edge.source_node_id, []).append(edge.target_node_id)
    temporary: set[str] = set()
    permanent: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in permanent:
            return
        if node_id in temporary:
            raise ValueError(f"Graph contains a cycle at node {node_id}")
        temporary.add(node_id)
        for target_id in outgoing.get(node_id, []):
            visit(target_id)
        temporary.remove(node_id)
        permanent.add(node_id)

    for node_id in sorted(node_ids):
        visit(node_id)
