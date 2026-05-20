from __future__ import annotations

from pydantic import BaseModel, Field

from harness.models import LiveOrchestrationGraph, OrchestrationInstance
from harness.orchestration_layout import GraphPositionMap, graph_position_map


class OrchestrationGraphRegistry(BaseModel):
    schema_version: str = "harness.orchestration_graph_registry/v1"
    selected_orchestration_id: str | None = None
    selected_node_id: str | None = None
    pinned_orchestration_id: str | None = None
    show_all_orchestrations: bool = False
    last_seen_seq_by_orchestration: dict[str, int] = Field(default_factory=dict)
    positions_by_orchestration: dict[str, GraphPositionMap] = Field(default_factory=dict)

    def apply_selection_hints(
        self,
        *,
        selected_orchestration_id: str | None = None,
        selected_node_id: str | None = None,
        pinned_orchestration_id: str | None = None,
        show_all_orchestrations: bool | None = None,
    ) -> None:
        if selected_orchestration_id:
            self.selected_orchestration_id = selected_orchestration_id
        if selected_node_id:
            self.selected_node_id = selected_node_id
        if pinned_orchestration_id is not None:
            self.pinned_orchestration_id = pinned_orchestration_id or None
        if show_all_orchestrations is not None:
            self.show_all_orchestrations = bool(show_all_orchestrations)

    def selected_node_by_orchestration(self) -> dict[str, str | None]:
        if self.selected_orchestration_id and self.selected_node_id:
            return {self.selected_orchestration_id: self.selected_node_id}
        return {}

    def apply_graphs(
        self,
        instances: list[OrchestrationInstance],
        graphs: list[LiveOrchestrationGraph],
    ) -> None:
        instance_ids = [instance.orchestration_id for instance in instances]
        graph_by_id = {graph.orchestration_id: graph for graph in graphs}
        if self.pinned_orchestration_id in instance_ids:
            selected = self.pinned_orchestration_id
        elif self.selected_orchestration_id in instance_ids:
            selected = self.selected_orchestration_id
        else:
            selected = instance_ids[0] if instance_ids else None
        self.selected_orchestration_id = selected
        selected_graph = graph_by_id.get(selected or "")
        if selected_graph is None:
            self.selected_node_id = None
        elif self.selected_node_id not in {node.id for node in selected_graph.nodes}:
            self.selected_node_id = selected_graph.selected_node_id
        for graph in graphs:
            self.positions_by_orchestration[graph.orchestration_id] = graph_position_map(graph)
            self.last_seen_seq_by_orchestration[graph.orchestration_id] = max(
                self.last_seen_seq_by_orchestration.get(graph.orchestration_id, 0),
                max((event.seq for event in graph.timeline_tail), default=0),
            )

    def select_orchestration_by_index(
        self,
        instances: list[OrchestrationInstance],
        index: int,
        graphs: list[LiveOrchestrationGraph],
    ) -> None:
        if not instances:
            self.selected_orchestration_id = None
            self.selected_node_id = None
            return
        selected = instances[max(0, min(index, len(instances) - 1))]
        self.selected_orchestration_id = selected.orchestration_id
        graph = next((item for item in graphs if item.orchestration_id == selected.orchestration_id), None)
        self.selected_node_id = graph.selected_node_id if graph is not None else None

    def move_selected_node(self, graph: LiveOrchestrationGraph | None, step: int) -> None:
        if graph is None or not graph.nodes:
            self.selected_node_id = None
            return
        node_ids = [node.id for node in sorted(graph.nodes, key=lambda item: ((item.row or 0), item.lane_id or "", item.id))]
        if self.selected_node_id not in node_ids:
            self.selected_node_id = graph.selected_node_id or node_ids[0]
            return
        current = node_ids.index(self.selected_node_id)
        self.selected_node_id = node_ids[(current + step) % len(node_ids)]

    def focus_attention_node(self, graph: LiveOrchestrationGraph | None) -> None:
        if graph is None:
            self.selected_node_id = None
            return
        if graph.attention_node_ids:
            self.selected_node_id = graph.attention_node_ids[0]
        elif graph.active_node_ids:
            self.selected_node_id = graph.active_node_ids[0]
        elif graph.nodes:
            self.selected_node_id = graph.nodes[0].id

    def toggle_pin(self) -> None:
        if self.selected_orchestration_id and self.pinned_orchestration_id != self.selected_orchestration_id:
            self.pinned_orchestration_id = self.selected_orchestration_id
        else:
            self.pinned_orchestration_id = None
