import json
from pathlib import Path

from typer.testing import CliRunner

from harness.agent_discovery import (
    AGENT_DISCOVERY_CATALOG_SCHEMA_VERSION,
    DELEGATE_ALLOCATION_SCHEMA_VERSION,
    build_agent_discovery_catalog,
    evaluate_delegate_allocation,
)
from harness.cli.main import app
from harness.config import default_config
from harness.local_server import _route_get
from harness.memory.sqlite_store import SQLiteStore


runner = CliRunner()


def _assert_passive_safety(safety: dict) -> None:
    assert safety["read_only"] is True
    assert safety["metadata_only"] is True
    assert safety["source_body_loaded"] is False
    assert safety["provider_called"] is False
    assert safety["network_called"] is False
    assert safety["agent_execution_started"] is False
    assert safety["tool_execution_started"] is False
    assert safety["adapter_execution_started"] is False
    assert safety["process_started"] is False
    assert safety["filesystem_modified"] is False
    assert safety["credential_accessed"] is False
    assert safety["permission_granting"] is False
    assert safety["budget_granting"] is False
    assert safety["model_context_allowed"] is False


def test_agent_discovery_catalog_uses_local_cards_without_project_init(tmp_path: Path) -> None:
    catalog = build_agent_discovery_catalog(tmp_path, workbench_id="coding")
    payload = catalog.model_dump(mode="json")
    cards = {card["agent_id"]: card for card in payload["cards"]}

    assert payload["schema_version"] == AGENT_DISCOVERY_CATALOG_SCHEMA_VERSION
    assert payload["ok"] is True
    assert payload["initialized"] is False
    assert payload["workbench_id"] == "coding"
    assert payload["summary"]["card_count"] == payload["summary"]["discoverable_count"]
    assert payload["summary"]["invalid_count"] == 0
    assert "security_reviewer" in cards
    security = cards["security_reviewer"]
    assert security["schema_version"] == "harness.agent_discovery_card/v1"
    assert security["source_kind"] == "builtin_registry"
    assert security["status"] == "discoverable"
    assert security["kind"] == "reviewer"
    assert security["tool_policy_id"] == "read_only"
    assert "security" in security["tags"]
    assert security["authority"]["agent_execution_allowed"] is False
    assert security["authority"]["tool_execution_allowed"] is False
    assert security["authority"]["permission_granting"] is False
    assert payload["sample_allocation"]["schema_version"] == DELEGATE_ALLOCATION_SCHEMA_VERSION
    assert payload["sample_allocation"]["selected_agent_ids"] == ["security_reviewer"]
    _assert_passive_safety(payload["safety"])
    _assert_passive_safety(security["safety"])
    assert not (tmp_path / ".harness").exists()


def test_delegate_allocation_selects_security_reviewer_without_execution(tmp_path: Path) -> None:
    allocation = evaluate_delegate_allocation(
        tmp_path,
        workbench_id="coding",
        task_type="security_review",
        required_kind="reviewer",
        required_tags=["security"],
        required_tool_policy_id="read_only",
        max_candidates=1,
    )
    payload = allocation.model_dump(mode="json")
    selected_bids = [bid for bid in payload["bids"] if bid["bid_id"] in set(payload["selected_bid_ids"])]

    assert payload["schema_version"] == DELEGATE_ALLOCATION_SCHEMA_VERSION
    assert payload["ok"] is True
    assert payload["announcement"]["schema_version"] == "harness.delegate_task_announcement/v1"
    assert payload["announcement"]["contents_included"] is False
    assert payload["announcement"]["authority"]["task_record_creation_allowed"] is False
    assert payload["announcement"]["authority"]["agent_execution_allowed"] is False
    assert payload["announcement"]["authority"]["tool_execution_allowed"] is False
    assert payload["announcement"]["authority"]["permission_granting"] is False
    assert payload["announcement"]["authority"]["budget_granting"] is False
    assert payload["selected_agent_ids"] == ["security_reviewer"]
    assert payload["summary"]["selected_count"] == 1
    assert selected_bids[0]["agent_id"] == "security_reviewer"
    assert selected_bids[0]["status"] == "eligible"
    assert selected_bids[0]["bid_terms"]["runtime_authority_granted"] is False
    assert selected_bids[0]["bid_terms"]["permission_granting"] is False
    assert selected_bids[0]["matched"]["tags"] == ["security"]
    _assert_passive_safety(payload["safety"])
    _assert_passive_safety(selected_bids[0]["safety"])
    assert not (tmp_path / ".harness").exists()


def test_delegate_allocation_invalid_kind_fails_closed(tmp_path: Path) -> None:
    allocation = evaluate_delegate_allocation(
        tmp_path,
        workbench_id="coding",
        required_kind="not_a_kind",  # type: ignore[arg-type]
        max_candidates=1,
    )
    payload = allocation.model_dump(mode="json")

    assert payload["schema_version"] == DELEGATE_ALLOCATION_SCHEMA_VERSION
    assert payload["ok"] is False
    assert payload["selected_agent_ids"] == []
    assert payload["summary"]["selected_count"] == 0
    assert payload["summary"]["eligible_count"] == 0
    assert payload["next_actions"] == ["Adjust task requirements or import a matching project agent before delegating."]
    assert all(any(reason.startswith("kind_mismatch:") for reason in bid["rejected_reasons"]) for bid in payload["bids"])
    _assert_passive_safety(payload["safety"])
    assert not (tmp_path / ".harness").exists()


def test_agents_discover_and_allocate_cli_are_read_only_without_project_init(tmp_path: Path) -> None:
    discovered = runner.invoke(
        app,
        ["agents", "discover", "--project", str(tmp_path), "--workbench", "coding", "--output", "json"],
    )
    allocated = runner.invoke(
        app,
        [
            "agents",
            "allocate",
            "--project",
            str(tmp_path),
            "--workbench",
            "coding",
            "--task-type",
            "security_review",
            "--required-kind",
            "reviewer",
            "--required-tag",
            "security",
            "--required-tool-policy",
            "read_only",
            "--max-candidates",
            "1",
            "--output",
            "json",
        ],
    )

    assert discovered.exit_code == 0, discovered.output
    discovery_payload = json.loads(discovered.output)
    assert discovery_payload["schema_version"] == AGENT_DISCOVERY_CATALOG_SCHEMA_VERSION
    assert discovery_payload["ok"] is True
    assert "security_reviewer" in {card["agent_id"] for card in discovery_payload["cards"]}
    assert allocated.exit_code == 0, allocated.output
    allocation_payload = json.loads(allocated.output)
    assert allocation_payload["schema_version"] == DELEGATE_ALLOCATION_SCHEMA_VERSION
    assert allocation_payload["selected_agent_ids"] == ["security_reviewer"]
    assert allocation_payload["announcement"]["authority"]["agent_execution_allowed"] is False
    assert allocation_payload["safety"]["agent_execution_started"] is False
    assert allocation_payload["safety"]["permission_granting"] is False
    assert not (tmp_path / ".harness").exists()


def test_agent_discovery_local_server_routes_are_metadata_only(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path)
    cfg = default_config()
    discovery = _route_get(
        "/agents/discovery",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
        query={"workbench": ["coding"]},
    )
    allocation = _route_get(
        "/agents/allocation",
        project_root=tmp_path,
        store=store,
        cfg=cfg,
        host="127.0.0.1",
        port=8765,
        query={
            "workbench": ["coding"],
            "task_type": ["security_review"],
            "required_kind": ["reviewer"],
            "required_tag": ["security"],
            "required_tool_policy": ["read_only"],
            "max_candidates": ["1"],
        },
    )

    assert discovery is not None
    assert discovery["schema_version"] == AGENT_DISCOVERY_CATALOG_SCHEMA_VERSION
    assert discovery["summary_projection"]["schema_version"] == "harness.agent_discovery_summary/v1"
    assert discovery["summary_projection"]["selected_sample_agent_ids"] == ["security_reviewer"]
    assert discovery["safety"]["agent_execution_started"] is False
    assert discovery["safety"]["permission_granting"] is False
    assert allocation is not None
    assert allocation["schema_version"] == DELEGATE_ALLOCATION_SCHEMA_VERSION
    assert allocation["selected_agent_ids"] == ["security_reviewer"]
    assert allocation["announcement"]["contents_included"] is False
    assert allocation["announcement"]["authority"]["task_record_creation_allowed"] is False
    assert allocation["safety"]["provider_called"] is False
    assert allocation["safety"]["permission_granting"] is False
    assert not (tmp_path / ".harness").exists()
