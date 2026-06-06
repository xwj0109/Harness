import json
from pathlib import Path

from typer.testing import CliRunner

from harness.agent_contracts import AGENT_CONTRACT_SCHEMA_VERSION
from harness.agent_handoff import AGENT_HANDOFF_ENVELOPE_SCHEMA_VERSION, build_agent_handoff_envelope
from harness.cli.main import app
from harness.memory.sqlite_store import SQLiteStore
from harness.models import SessionPermissionStatus


runner = CliRunner()


def _create_delegated_task(tmp_path: Path):
    store = SQLiteStore(tmp_path)
    store.initialize()
    parent = store.create_session(title="Parent")
    child = store.fork_session(parent.id, title="Child")
    task = store.create_task(
        "Inspect handoff",
        description="Inspect delegated work.",
        agent_id="repo_inspector",
        workbench_id="coding",
        metadata={
            "schema_version": "harness.session_tool_task_metadata/v1",
            "task_type": "session_delegate",
            "execution_adapter": "session_child_task",
            "execution_started": False,
            "hidden_process_started": False,
            "parent_session_id": parent.id,
            "child_session_id": child.id,
            "source_tool_run_id": "run_handoff_unit",
            "allowed_tools": ["read", "glob", "grep"],
            "boundary": "read_only_project",
            "output_expectation": "Short markdown summary.",
        },
        session_id=child.id,
    )
    return store, parent, child, task


def test_agent_handoff_envelope_is_typed_traceable_and_non_authoritative(tmp_path: Path) -> None:
    _, parent, child, task = _create_delegated_task(tmp_path)

    envelope = build_agent_handoff_envelope(tmp_path, task)
    payload = envelope.model_dump(mode="json")

    assert payload["schema_version"] == AGENT_HANDOFF_ENVELOPE_SCHEMA_VERSION
    assert payload["ok"] is True
    assert payload["task_id"] == task.id
    assert payload["parent_session_id"] == parent.id
    assert payload["child_session_id"] == child.id
    assert payload["execution_adapter"] == "session_child_task"
    assert payload["task_type"] == "session_delegate"
    assert payload["delegate_budget"]["schema_version"] == "harness.delegate_budget/v1"
    assert payload["delegate_budget"]["max_runtime_invocations"] == 0
    assert payload["delegate_budget"]["max_model_calls"] == 0
    assert payload["delegate_budget"]["max_tool_calls"] == 0
    assert payload["delegate_budget"]["network_policy"] == "forbidden"
    assert payload["agent_contract"]["schema_version"] == AGENT_CONTRACT_SCHEMA_VERSION
    assert payload["agent_contract"]["ok"] is True
    assert payload["agent_contract"]["agent_id"] == "repo_inspector"
    assert payload["agent_contract"]["tool_policy_id"] == "read_only"
    assert payload["agent_contract"]["authority"]["agent_execution_allowed"] is False
    assert payload["agent_contract"]["authority"]["permission_granting"] is False
    assert payload["trace_context"]["schema_version"] == "harness.agent_handoff_trace_context/v1"
    assert payload["trace_context"]["traceparent"].startswith(f"00-{payload['trace_context']['trace_id']}-")
    assert payload["integrity"]["payload_sha256"]
    assert payload["integrity"]["artifact_bodies_included"] is False
    assert payload["integrity"]["credential_values_included"] is False
    assert payload["authority"]["adapter_execution_allowed"] is False
    assert payload["authority"]["process_start_allowed"] is False
    assert payload["authority"]["network_allowed"] is False
    assert payload["authority"]["tool_execution_allowed"] is False
    assert payload["authority"]["agent_execution_allowed"] is False
    assert payload["authority"]["permission_granting"] is False
    assert payload["safety"]["read_only"] is True
    assert payload["safety"]["adapter_execution_started"] is False
    assert payload["safety"]["network_called"] is False
    assert payload["safety"]["filesystem_modified"] is False


def test_session_task_tool_persists_handoff_metadata_and_status_projection(tmp_path: Path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    store = SQLiteStore(tmp_path)
    session = store.create_session(title="Delegate with envelope")
    args = {
        "objective": "Inspect the repository layout and report likely entrypoints.",
        "allowed_tools": ["read", "glob", "grep"],
        "boundary": "read_only_project",
        "output_expectation": "Short markdown summary with file references.",
        "agent": "repo_inspector",
    }
    first = runner.invoke(
        app,
        ["session", "tool", session.id, "task", "--project", str(tmp_path), "--input-json", json.dumps(args), "--output", "json"],
    )
    first_payload = json.loads(first.output)["result"]
    assert first_payload["error_type"] == "permission_required"
    allowed = runner.invoke(
        app,
        [
            "session",
            "permission",
            session.id,
            "--project",
            str(tmp_path),
            "--resolve",
            first_payload["permission_id"],
            "--decision",
            "allowed",
            "--output",
            "json",
        ],
    )
    second = runner.invoke(
        app,
        ["session", "tool", session.id, "task", "--project", str(tmp_path), "--input-json", json.dumps(args), "--output", "json"],
    )

    assert allowed.exit_code == 0, allowed.output
    assert second.exit_code == 0, second.output
    result = json.loads(json.loads(second.output)["result"]["preview"])
    handoff = result["handoff"]
    assert handoff["schema_version"] == AGENT_HANDOFF_ENVELOPE_SCHEMA_VERSION
    assert handoff["ok"] is True
    assert handoff["agent_contract_schema_version"] == AGENT_CONTRACT_SCHEMA_VERSION
    assert handoff["agent_contract_ok"] is True
    assert handoff["agent_contract_id"]
    assert handoff["agent_contract_sha256"]
    assert handoff["adapter_execution_allowed"] is False
    assert handoff["permission_granting"] is False
    task = SQLiteStore(tmp_path).get_task(result["task_id"])
    assert task.metadata["handoff_schema_version"] == AGENT_HANDOFF_ENVELOPE_SCHEMA_VERSION
    assert task.metadata["handoff_envelope_id"] == handoff["envelope_id"]
    assert task.metadata["handoff_payload_sha256"] == handoff["payload_sha256"]
    assert task.metadata["handoff_traceparent"] == handoff["traceparent"]
    assert task.metadata["handoff_agent_contract_schema_version"] == AGENT_CONTRACT_SCHEMA_VERSION
    assert task.metadata["handoff_agent_contract_id"] == handoff["agent_contract_id"]
    assert task.metadata["handoff_agent_contract_sha256"] == handoff["agent_contract_sha256"]

    inspected = runner.invoke(
        app,
        ["handoffs", "inspect-task", task.id, "--project", str(tmp_path), "--output", "json"],
    )
    status = runner.invoke(
        app,
        ["session", "tool", session.id, "task-status", "--project", str(tmp_path), "--input-json", json.dumps({"task_id": task.id}), "--output", "json"],
    )

    assert inspected.exit_code == 0, inspected.output
    inspected_payload = json.loads(inspected.output)
    assert inspected_payload["schema_version"] == AGENT_HANDOFF_ENVELOPE_SCHEMA_VERSION
    assert inspected_payload["ok"] is True
    assert inspected_payload["envelope_id"] == handoff["envelope_id"]
    assert inspected_payload["agent_contract"]["schema_version"] == AGENT_CONTRACT_SCHEMA_VERSION
    assert inspected_payload["agent_contract"]["contract_id"] == handoff["agent_contract_id"]
    assert inspected_payload["agent_contract"]["authority"]["agent_execution_allowed"] is False
    assert inspected_payload["authority"]["adapter_execution_allowed"] is False
    assert status.exit_code == 0, status.output
    status_payload = json.loads(json.loads(status.output)["result"]["preview"])
    assert status_payload["handoff"]["schema_version"] == AGENT_HANDOFF_ENVELOPE_SCHEMA_VERSION
    assert status_payload["handoff"]["envelope_id"] == handoff["envelope_id"]
    assert status_payload["handoff"]["agent_contract"]["contract_id"] == handoff["agent_contract_id"]
    assert SQLiteStore(tmp_path).get_session_permission(first_payload["permission_id"]).status == SessionPermissionStatus.EXPIRED


def test_handoff_inspect_missing_task_fails_closed(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["handoffs", "inspect-task", "task_missing", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["schema_version"] == AGENT_HANDOFF_ENVELOPE_SCHEMA_VERSION
    assert payload["ok"] is False
    assert payload["safety"]["read_only"] is True
    assert payload["safety"]["adapter_execution_started"] is False
    assert payload["safety"]["process_started"] is False
    assert payload["safety"]["network_called"] is False
    assert payload["safety"]["permission_granting"] is False
    assert not (tmp_path / ".harness").exists()


def test_handoff_envelope_invalid_task_reports_schema_errors_without_authority(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    task = store.create_task(
        "Invalid handoff",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )

    envelope = build_agent_handoff_envelope(tmp_path, task)
    payload = envelope.model_dump(mode="json")

    assert payload["ok"] is False
    assert "execution_adapter must be session_child_task" in payload["validation_errors"]
    assert "task_type must be session_delegate" in payload["validation_errors"]
    assert "agent_contract must resolve for delegated task" in payload["validation_errors"]
    assert payload["agent_contract"]["ok"] is False
    assert payload["authority"]["adapter_execution_allowed"] is False
    assert payload["authority"]["network_allowed"] is False
    assert payload["authority"]["permission_granting"] is False
