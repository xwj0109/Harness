import json
import sqlite3
import hashlib

import pytest

from harness.agent_authoring import load_agent_bundle
from harness.config import default_config
from harness.memory.sqlite_store import SQLiteStore
from harness.models import ObjectiveStatus, TaskLeaseStatus, TaskStatus


def _write_project_agent_bundle(path, *, agent_id: str = "custom_quant_researcher") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "agent.yaml").write_text(
        f"""
schema_version: harness.agent_bundle/v1
workbench_id: quant
agent:
  id: {agent_id}
  kind: specialist
  role: Custom quant research.
  model_profile: local_reasoning
  tool_policy: read_only
  memory_scope: quant
  parent: quant_research
  outputs:
    - custom_research_note.md
  tags:
    - custom
""".lstrip(),
        encoding="utf-8",
    )
    profiles_dir = path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "default.yaml").write_text(
        f"""
id: {agent_id}.default
agent_id: {agent_id}
description: Default custom profile.
knowledge_domains:
  - commodities
preferred_outputs:
  - custom_research_note.md
review_responsibilities: []
forbidden_actions:
  - live_trading
tags:
  - custom
metadata: {{}}
""".lstrip(),
        encoding="utf-8",
    )


def test_store_create_run_event_artifact_and_report(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="test run", task_type="phase_1a_test")
    event = store.append_event(run.id, "info", "test", "hello", {"a": 1})
    paths = store.initialize_run_artifacts(run.id)
    artifact = store.register_artifact(run.id, "events", paths["events"])
    report = store.generate_final_report(run.id)

    assert store.get_run(run.id).goal == "test run"
    assert len(store.list_runs()) == 1
    assert store.list_events(run.id)[0].id == event.id
    assert store.list_artifacts(run.id)[0].id == artifact.id
    assert artifact.schema_version == "harness.artifact/v1"
    assert artifact.sha256
    assert artifact.size_bytes == paths["events"].stat().st_size
    assert artifact.evidence_status == "verified"
    assert paths["events"].read_text(encoding="utf-8")
    assert report.exists()
    assert "Run " in report.read_text(encoding="utf-8")


def test_project_agent_import_persists_lists_and_inspects(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    bundle_path = tmp_path / "agents" / "custom_quant_researcher"
    _write_project_agent_bundle(bundle_path)
    loaded = load_agent_bundle(bundle_path)

    imported = store.import_project_agent(loaded)
    listed = store.list_project_agents()
    inspected = store.get_project_agent("custom_quant_researcher")

    assert imported.schema_version == "harness.project_agent/v1"
    assert imported.agent_id == "custom_quant_researcher"
    assert imported.workbench_id == "quant"
    assert imported.source_path == bundle_path.resolve()
    assert imported.content_sha256
    assert imported.agent["id"] == "custom_quant_researcher"
    assert imported.profiles[0]["id"] == "custom_quant_researcher.default"
    assert [agent.agent_id for agent in listed] == ["custom_quant_researcher"]
    assert inspected.content_sha256 == imported.content_sha256


def test_project_agent_import_rejects_duplicate_and_unknown_inspect(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    bundle_path = tmp_path / "agents" / "custom_quant_researcher"
    _write_project_agent_bundle(bundle_path)
    loaded = load_agent_bundle(bundle_path)

    store.import_project_agent(loaded)

    with pytest.raises(ValueError, match="Project agent already imported: custom_quant_researcher"):
        store.import_project_agent(loaded)
    with pytest.raises(KeyError, match="Project agent not found: missing_agent"):
        store.get_project_agent("missing_agent")


def test_project_agent_table_initializes_on_existing_project(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    with store.connect() as conn:
        conn.execute("DROP TABLE project_agents")

    store.initialize()

    with store.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(project_agents)").fetchall()}
    assert {"agent_id", "workbench_id", "source_path", "content_sha256", "agent_json", "profiles_json"} <= columns


def test_store_writes_and_refreshes_run_manifest(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="test run", task_type="phase_1a_test")
    manifest_path = tmp_path / ".harness" / "runs" / run.id / "manifest.json"

    initial = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert initial["schema_version"] == "harness.manifest/v1.1"
    assert initial["run_id"] == run.id
    assert initial["goal"] == "test run"
    assert initial["task_type"] == "phase_1a_test"
    assert initial["run_mode"] == "dev"
    assert initial["status"] == "created"
    assert initial["project_root"] == str(tmp_path.resolve())
    assert initial["approval_id"] is None
    assert initial["backend_descriptor"] is None
    assert initial["backend_descriptor_sha256"] is None
    assert initial["effective_policy"]["schema_version"] == "harness.effective_policy/v1"
    assert initial["effective_policy"]["subject_kind"] == "run"
    assert initial["effective_policy"]["subject_id"] == run.id
    assert initial["effective_policy_sha256"]
    assert initial["task_id"] is None
    assert initial["objective_id"] is None
    assert initial["trace_id"] is None
    assert initial["artifacts"] == []

    paths = store.initialize_run_artifacts(run.id)
    store.register_artifact(run.id, "events", paths["events"], {"required": True})
    with_artifact = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact_entry = with_artifact["artifacts"][0]
    assert with_artifact["artifacts"] == [
        {
            "schema_version": "harness.artifact/v1",
            "id": artifact_entry["id"],
            "run_id": run.id,
            "kind": "events",
            "path": str(paths["events"]),
            "created_at": artifact_entry["created_at"],
            "sha256": hashlib.sha256(b"").hexdigest(),
            "size_bytes": 0,
            "producer": None,
            "redaction_state": "unknown",
            "evidence_status": "verified",
            "metadata": {"required": True},
        }
    ]

    store.update_run_status(run.id, "completed")
    completed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert completed["status"] == "completed"
    assert completed["updated_at"] >= initial["updated_at"]


def test_store_manifest_includes_backend_descriptor_without_settings(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    backend = default_config().backends["local_openai_compatible"]
    run = store.create_run(
        goal="test run",
        task_type="read_only_repo_summary",
        backend=backend,
        approval_id="approval_123",
    )

    manifest_path = tmp_path / ".harness" / "runs" / run.id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    descriptor = manifest["backend_descriptor"]

    assert manifest["run_mode"] == "read_only"
    assert manifest["approval_id"] == "approval_123"
    assert descriptor["name"] == "local_openai_compatible"
    assert descriptor["kind"] == "native_model"
    assert descriptor["metadata"]["data_boundary"] == "local_only"
    assert descriptor["capabilities"]["json_mode"] is True
    assert manifest["backend_descriptor_sha256"]
    assert manifest["effective_policy_sha256"]
    assert "settings" not in descriptor
    assert "base_url" not in json.dumps(descriptor)


def test_store_redacts_secret_like_event_payloads(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="test run", task_type="phase_1a_test")
    secret = "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz"
    store.append_event(run.id, "info", "secret_test", "payload redaction", {"secret": secret})
    events_jsonl = (tmp_path / ".harness" / "runs" / run.id / "events.jsonl").read_text(
        encoding="utf-8"
    )
    events = store.list_events(run.id)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in events_jsonl
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in str(events[0].payload)
    assert "[REDACTED_SECRET]" in events_jsonl


def test_store_initializes_tasks_table_without_breaking_runs(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()

    with sqlite3.connect(tmp_path / ".harness" / "harness.sqlite") as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        task_columns = {row[1] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        artifact_columns = {row[1] for row in conn.execute("PRAGMA table_info(artifacts)").fetchall()}

    assert {
        "runs",
        "events",
        "artifacts",
        "backend_snapshots",
        "tasks",
        "objectives",
        "task_dependencies",
        "task_attempts",
        "task_leases",
        "task_transitions",
        "run_baselines",
        "daemon_records",
        "daemon_events",
    } <= tables
    assert {
        "objective_id",
        "idempotency_key",
        "required_approvals_json",
        "approval_state",
    } <= task_columns
    assert {
        "schema_version",
        "sha256",
        "size_bytes",
        "producer",
        "redaction_state",
        "evidence_status",
    } <= artifact_columns
    run = store.create_run(goal="test run", task_type="phase_1a_test")
    assert store.get_run(run.id).id == run.id


def test_store_daemon_records_events_status_and_stop(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()

    daemon = store.ensure_daemon("local_daemon:test:123", pid=123)
    event = store.record_daemon_event(
        daemon.id,
        "tick",
        "Daemon tick.",
        {"secret": "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz"},
    )
    status = store.daemon_status()

    assert daemon.owner == "local_daemon:test:123"
    assert daemon.status.value == "running"
    assert daemon.pid == 123
    assert status.schema_version == "harness.daemon_status/v1"
    assert status.ok is True
    assert status.active_daemons[0].id == daemon.id
    assert store.get_daemon_event(event.id).metadata["secret"] == "[REDACTED_SECRET]"
    assert store.list_daemon_events(daemon.id)[0].event_type == "tick"

    stopped = store.stop_daemons(owner="local_daemon:test:123")

    assert [item.id for item in stopped] == [daemon.id]
    assert stopped[0].status.value == "stopped"
    assert store.daemon_status().active_daemons == []


def test_store_daemon_run_once_leases_without_creating_runs_or_artifacts(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    high = store.create_task(title="High", priority=10)
    low = store.create_task(title="Low", priority=0)

    result = store.daemon_run_once("local_daemon:test:123", pid=123)
    second = store.daemon_run_once("local_daemon:test:123", pid=123)

    assert result.schema_version == "harness.daemon_tick/v1"
    assert result.ok is True
    assert result.owner == "local_daemon:test:123"
    assert result.decision == "leased_task"
    assert result.selected_task is not None
    assert result.selected_task.id == high.id
    assert result.attempt is not None
    assert result.attempt.run_id is None
    assert result.lease is not None
    assert result.lease.owner == "local_daemon:test:123"
    assert second.decision == "renewed_lease"
    assert second.selected_task is None
    assert second.lease is not None
    assert second.lease.task_id == high.id
    assert store.get_task(low.id).status == TaskStatus.READY
    assert store.list_runs() == []
    assert not any((tmp_path / ".harness" / "runs").iterdir())
    event_types = [event.event_type for event in store.list_daemon_events(result.daemon_id)]
    assert event_types.count("tick") == 2
    assert event_types.count("heartbeat") == 1
    assert event_types.count("start") == 1


def test_store_daemon_run_once_reports_no_eligible_task(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    store.create_task(title="Approval", required_approvals=["hosted_provider"])

    result = store.daemon_run_once("local_daemon:test:123", pid=123)
    status = store.daemon_status()

    assert result.decision == "paused"
    assert result.selected_task is None
    assert result.attempt is None
    assert result.lease is None
    assert result.pause_reasons[0]["decision"] == "waiting_approval"
    assert result.pause_reasons[0]["required_approvals"] == ["hosted_provider"]
    assert status.paused_tasks[0]["decision"] == "waiting_approval"
    assert store.list_runs() == []


def test_store_daemon_policy_forbidden_task_is_paused_not_leased(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    forbidden = store.create_task(
        title="Forbidden active repo write",
        priority=10,
        metadata={"requires_active_repo_write": True},
    )
    eligible = store.create_task(title="Eligible", priority=0)

    result = store.daemon_run_once("local_daemon:test:123", pid=123)
    paused = store.daemon_status().paused_tasks

    assert result.decision == "leased_task"
    assert result.selected_task is not None
    assert result.selected_task.id == eligible.id
    assert result.pause_reasons[0]["task_id"] == forbidden.id
    assert result.pause_reasons[0]["decision"] == "policy_forbidden"
    assert result.pause_reasons[0]["forbidden_policy_keys"] == ["active_repo_write"]
    assert paused[0]["task_id"] == forbidden.id
    assert store.get_task(forbidden.id).status == TaskStatus.READY
    assert store.list_runs() == []


def test_store_manual_run_next_is_not_blocked_by_daemon_policy_metadata(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    task = store.create_task(
        title="Manual active repo write task",
        metadata={"requires_active_repo_write": True},
    )

    selection = store.select_next_task_for_lease()

    assert selection is not None
    assert selection["task"].id == task.id
    assert selection["task"].status == TaskStatus.LEASED


def test_store_daemon_recovery_expires_leases_and_requeues_tasks(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    task = store.create_task(title="Recover me")
    original_idempotency_key = task.idempotency_key
    selection = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert selection.lease is not None
    assert selection.attempt is not None
    past = "2026-01-01T00:00:00+00:00"
    with store.connect() as conn:
        conn.execute(
            "UPDATE task_leases SET expires_at = ? WHERE id = ?",
            (past, selection.lease.id),
        )

    recovery = store.recover_daemon_leases("local_daemon:test:123", pid=123)
    second_recovery = store.recover_daemon_leases("local_daemon:test:123", pid=123)

    recovered = store.get_task(task.id)
    attempts = store.list_task_attempts(task.id)
    leases = store.list_task_leases(task.id)
    assert recovery.schema_version == "harness.daemon_recovery/v1"
    assert [lease.id for lease in recovery.expired_leases] == [selection.lease.id]
    assert [item.id for item in recovery.recovered_tasks] == [task.id]
    assert recovery.events[0].event_type == "recover_lease"
    assert recovered.status == TaskStatus.READY
    assert recovered.idempotency_key == original_idempotency_key
    assert attempts[0].status == TaskStatus.FAILED
    assert attempts[0].failure_code == "lease_expired"
    assert attempts[0].run_id is None
    assert leases[0].status == TaskLeaseStatus.EXPIRED
    assert store.list_task_transitions(task.id)[-1].reason == "lease_expired"
    assert second_recovery.expired_leases == []
    assert second_recovery.recovered_tasks == []
    assert store.list_runs() == []
    assert not any((tmp_path / ".harness" / "runs").iterdir())


def test_store_daemon_recovery_requeues_to_waiting_approval_or_blocked(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    dependency = store.create_task(title="Dependency")
    approval_task = store.create_task(title="Approval", required_approvals=["hosted_provider"])
    blocked_task = store.create_task(title="Blocked", depends_on=[dependency.id])

    for task in [approval_task, blocked_task]:
        with store.connect() as conn:
            conn.execute(
                "UPDATE tasks SET status = ? WHERE id = ?",
                (TaskStatus.LEASED.value, task.id),
            )
            conn.execute(
                """
                INSERT INTO task_leases (
                  id, task_id, attempt_id, owner, status, acquired_at, expires_at,
                  heartbeat_at, released_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"lease_{task.id}",
                    task.id,
                    None,
                    "local_daemon:test:123",
                    TaskLeaseStatus.ACTIVE.value,
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                    None,
                    None,
                    "{}",
                ),
            )

    recovery = store.recover_daemon_leases("local_daemon:test:123", pid=123)

    assert {task.id for task in recovery.recovered_tasks} == {approval_task.id, blocked_task.id}
    assert store.get_task(approval_task.id).status == TaskStatus.WAITING_APPROVAL
    assert store.get_task(blocked_task.id).status == TaskStatus.BLOCKED


def test_store_daemon_execute_dry_run_links_lease_attempt_run_and_releases(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    task = store.create_task(
        title="Dry run",
        description="contract only",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None

    result = store.execute_dry_run_lease(leased.lease.id, owner="local_daemon:test:123")

    assert result.schema_version == "harness.daemon_execute_dry_run/v1"
    assert result.decision == "dry_run_no_tool_execution"
    assert result.task.id == task.id
    assert result.task.status == TaskStatus.SUCCEEDED
    assert result.attempt.run_id == result.run.id
    assert result.attempt.status == TaskStatus.SUCCEEDED
    assert result.attempt.started_at is not None
    assert result.attempt.finished_at is not None
    assert result.lease.status == TaskLeaseStatus.RELEASED
    assert result.lease.metadata["run_id"] == result.run.id
    assert result.run.status == "completed"
    assert result.run.task_type == "phase_1a_test"
    assert result.run.task_id == task.id
    assert result.manifest.schema_version == "harness.manifest/v1.1"
    assert result.manifest.task_id == task.id
    assert result.manifest.objective_id is None
    assert result.manifest.backend_descriptor is None
    assert result.manifest.backend_descriptor_sha256 is None
    assert {artifact.kind for artifact in store.list_artifacts(result.run.id)} >= {
        "events",
        "transcript",
        "final_report",
        "manifest",
    }
    assert store.list_task_transitions(task.id)[-2].reason == "dry_run_execution_started"
    assert store.list_task_transitions(task.id)[-1].reason == "dry_run_execution_succeeded"
    assert "execute_dry_run" in {event.event_type for event in store.list_daemon_events()}
    report = tmp_path / ".harness" / "runs" / result.run.id / "final_report.md"
    report_text = report.read_text(encoding="utf-8")
    assert "No backend, tool, Docker, shell, network, hosted provider, or paid provider was invoked." in report_text
    assert "api_key" not in report_text
    assert "OPENAI_API_KEY" not in report_text


def test_store_daemon_execute_dry_run_rejects_duplicate_and_ineligible_tasks(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    task = store.create_task(
        title="Dry run",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None
    store.execute_dry_run_lease(leased.lease.id, owner="local_daemon:test:123")

    with pytest.raises(ValueError, match="Dry-run execution requires active lease: released"):
        store.execute_dry_run_lease(leased.lease.id, owner="local_daemon:test:123")

    missing_metadata = store.create_task(title="No adapter")
    leased_missing = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased_missing.lease is not None
    assert leased_missing.selected_task is not None
    assert leased_missing.selected_task.id == missing_metadata.id
    with pytest.raises(ValueError, match="Dry-run execution requires execution_adapter=dry_run"):
        store.execute_dry_run_lease(leased_missing.lease.id, owner="local_daemon:test:123")


def test_store_daemon_execute_dry_run_rejects_approvals_wrong_type_and_forbidden_metadata(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    wrong_type = store.create_task(
        title="Wrong type",
        priority=10,
        metadata={"execution_adapter": "dry_run", "task_type": "read_only_repo_summary"},
    )
    forbidden = store.create_task(
        title="Forbidden",
        priority=5,
        metadata={
            "execution_adapter": "dry_run",
            "task_type": "phase_1a_test",
            "requires_external_network": True,
        },
    )
    approval = store.create_task(
        title="Approval",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
        required_approvals=["hosted_provider"],
    )

    wrong_type_selection = store.select_next_task_for_lease(owner="local_daemon:test:123")
    assert wrong_type_selection is not None
    with pytest.raises(ValueError, match="Dry-run execution requires task_type=phase_1a_test"):
        store.execute_dry_run_lease(wrong_type_selection["lease"].id, owner="local_daemon:test:123")

    forbidden_selection = store.select_next_task_for_lease(owner="local_daemon:test:123")
    assert forbidden_selection is not None
    assert forbidden_selection["task"].id == forbidden.id
    with pytest.raises(ValueError, match="requires_external_network"):
        store.execute_dry_run_lease(forbidden_selection["lease"].id, owner="local_daemon:test:123")

    assert store.get_task(approval.id).status == TaskStatus.WAITING_APPROVAL
    with pytest.raises(KeyError, match="Task lease not found: missing_lease"):
        store.execute_dry_run_lease("missing_lease", owner="local_daemon:test:123")


def test_store_daemon_inspect_lease_reports_dry_run_linkage_without_mutation(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    task = store.create_task(
        title="Dry run",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None
    before_runs = store.list_runs()

    before = store.inspect_task_lease(leased.lease.id)
    assert store.list_runs() == before_runs
    executed = store.execute_dry_run_lease(leased.lease.id, owner="local_daemon:test:123")
    after = store.inspect_task_lease(leased.lease.id)

    assert before.schema_version == "harness.daemon_lease/v1"
    assert before.lease.id == leased.lease.id
    assert before.task is not None
    assert before.task.id == task.id
    assert before.attempt is not None
    assert before.attempt.run_id is None
    assert before.run is None
    assert before.manifest is None
    assert before.dry_run_eligibility["eligible"] is True
    assert before.recovery_recommendation["action"] == "none"
    assert after.lease.status == TaskLeaseStatus.RELEASED
    assert after.task is not None
    assert after.task.status == TaskStatus.SUCCEEDED
    assert after.attempt is not None
    assert after.attempt.run_id == executed.run.id
    assert after.run is not None
    assert after.run.id == executed.run.id
    assert after.manifest is not None
    assert after.manifest.task_id == task.id
    assert after.dry_run_eligibility["eligible"] is False
    assert after.recovery_recommendation["action"] == "none"


def test_store_read_only_lease_validation_and_inspection(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    task = store.create_task(
        title="Read-only",
        metadata={"execution_adapter": "read_only_summary", "task_type": "read_only_repo_summary"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None

    lease, attempt, validated_task = store.validate_read_only_lease_for_execution(leased.lease.id)
    inspection = store.inspect_task_lease(leased.lease.id)

    assert lease.id == leased.lease.id
    assert attempt.run_id is None
    assert validated_task.id == task.id
    assert inspection.read_only_eligibility["eligible"] is True
    assert inspection.dry_run_eligibility["eligible"] is False
    assert store.list_runs() == []


def test_store_read_only_start_and_recovery_reconcile_completed_partial_state(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    backend = default_config().backends["local_openai_compatible"]
    task = store.create_task(
        title="Read-only",
        metadata={"execution_adapter": "read_only_summary", "task_type": "read_only_repo_summary"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None

    run = store.start_read_only_lease_run(
        leased.lease.id,
        backend=backend,
        owner="local_daemon:test:123",
    )
    store.update_run_status(run.id, "completed")

    recovery = store.recover_daemon_leases("local_daemon:test:123", pid=123)

    assert [item.id for item in recovery.recovered_tasks] == [task.id]
    assert recovery.events[0].event_type == "recover_read_only"
    assert store.get_task(task.id).status == TaskStatus.SUCCEEDED
    assert store.get_task_attempt(leased.attempt.id).status == TaskStatus.SUCCEEDED
    assert store.get_task_lease(leased.lease.id).status == TaskLeaseStatus.RELEASED
    assert len(store.list_runs()) == 1


def test_store_read_only_recovery_reconciles_failed_partial_state(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    backend = default_config().backends["local_openai_compatible"]
    task = store.create_task(
        title="Read-only",
        metadata={"execution_adapter": "read_only_summary", "task_type": "read_only_repo_summary"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None

    run = store.start_read_only_lease_run(
        leased.lease.id,
        backend=backend,
        owner="local_daemon:test:123",
    )
    store.update_run_status(run.id, "failed")

    recovery = store.recover_daemon_leases("local_daemon:test:123", pid=123)

    assert [item.id for item in recovery.recovered_tasks] == [task.id]
    assert recovery.events[0].event_type == "recover_read_only"
    assert store.get_task(task.id).status == TaskStatus.FAILED
    attempt = store.get_task_attempt(leased.attempt.id)
    assert attempt.status == TaskStatus.FAILED
    assert attempt.failure_code == "read_only_failed"
    assert store.get_task_lease(leased.lease.id).status == TaskLeaseStatus.RELEASED
    assert len(store.list_runs()) == 1


def test_store_read_only_recovery_fails_expired_active_nonterminal_run(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    backend = default_config().backends["local_openai_compatible"]
    task = store.create_task(
        title="Read-only",
        metadata={"execution_adapter": "read_only_summary", "task_type": "read_only_repo_summary"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None

    run = store.start_read_only_lease_run(
        leased.lease.id,
        backend=backend,
        owner="local_daemon:test:123",
    )
    with store.connect() as conn:
        conn.execute(
            "UPDATE task_leases SET status = ?, expires_at = ?, released_at = NULL WHERE id = ?",
            (TaskLeaseStatus.ACTIVE.value, "2026-01-01T00:00:00+00:00", leased.lease.id),
        )

    recovery = store.recover_daemon_leases("local_daemon:test:123", pid=123)

    assert [lease.id for lease in recovery.expired_leases] == [leased.lease.id]
    assert [item.id for item in recovery.recovered_tasks] == [task.id]
    assert recovery.events[0].event_type == "recover_read_only"
    assert store.get_run(run.id).status == "running"
    assert store.get_task(task.id).status == TaskStatus.FAILED
    attempt = store.get_task_attempt(leased.attempt.id)
    assert attempt.status == TaskStatus.FAILED
    assert attempt.failure_code == "read_only_recovery_required"
    assert store.get_task_lease(leased.lease.id).status == TaskLeaseStatus.EXPIRED
    assert len(store.list_runs()) == 1


def test_store_read_only_validation_rejects_existing_run_id_approvals_and_forbidden_metadata(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    backend = default_config().backends["local_openai_compatible"]
    linked = store.create_task(
        title="Read-only",
        priority=20,
        metadata={"execution_adapter": "read_only_summary", "task_type": "read_only_repo_summary"},
    )
    forbidden = store.create_task(
        title="Forbidden",
        priority=10,
        metadata={
            "execution_adapter": "read_only_summary",
            "task_type": "read_only_repo_summary",
            "requires_active_repo_write": True,
            "requires_docker": True,
        },
    )
    approval = store.create_task(
        title="Approval",
        priority=5,
        metadata={"execution_adapter": "read_only_summary", "task_type": "read_only_repo_summary"},
    )

    linked_selection = store.select_next_task_for_lease(owner="local_daemon:test:123")
    assert linked_selection is not None
    run = store.start_read_only_lease_run(
        linked_selection["lease"].id,
        backend=backend,
        owner="local_daemon:test:123",
    )
    assert store.get_task(linked.id).run_id == run.id
    with pytest.raises(ValueError, match="Task attempt already has run_id"):
        store.validate_read_only_lease_for_execution(linked_selection["lease"].id)

    forbidden_selection = store.select_next_task_for_lease(owner="local_daemon:test:123")
    assert forbidden_selection is not None
    with pytest.raises(ValueError, match="requires_active_repo_write, requires_docker"):
        store.validate_read_only_lease_for_execution(forbidden_selection["lease"].id)

    approval_selection = store.select_next_task_for_lease(owner="local_daemon:test:123")
    assert approval_selection is not None
    with store.connect() as conn:
        conn.execute(
            "UPDATE tasks SET required_approvals_json = ?, approval_state = ? WHERE id = ?",
            ('["hosted_provider"]', "required", approval.id),
        )
    with pytest.raises(ValueError, match="unresolved required approvals"):
        store.validate_read_only_lease_for_execution(approval_selection["lease"].id)


def test_store_daemon_recovery_reconciles_completed_dry_run_partial_state(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    store.create_task(title="Dry run", metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"})
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None
    executed = store.execute_dry_run_lease(leased.lease.id, owner="local_daemon:test:123")
    with store.connect() as conn:
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (TaskStatus.RUNNING.value, executed.task.id))
        conn.execute("UPDATE task_attempts SET status = ? WHERE id = ?", (TaskStatus.RUNNING.value, executed.attempt.id))
        conn.execute(
            "UPDATE task_leases SET status = ?, released_at = NULL WHERE id = ?",
            (TaskLeaseStatus.ACTIVE.value, executed.lease.id),
        )

    recovery = store.recover_daemon_leases("local_daemon:test:123", pid=123)

    assert len(store.list_runs()) == 1
    assert [task.id for task in recovery.recovered_tasks] == [executed.task.id]
    assert recovery.events[0].event_type == "recover_dry_run"
    assert store.get_task(executed.task.id).status == TaskStatus.SUCCEEDED
    assert store.get_task_attempt(executed.attempt.id).status == TaskStatus.SUCCEEDED
    assert store.get_task_lease(executed.lease.id).status == TaskLeaseStatus.RELEASED


def test_store_daemon_recovery_reconciles_failed_dry_run_partial_state(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    store.create_task(title="Dry run", metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"})
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None
    executed = store.execute_dry_run_lease(leased.lease.id, owner="local_daemon:test:123")
    with store.connect() as conn:
        conn.execute("UPDATE runs SET status = ? WHERE id = ?", ("failed", executed.run.id))
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (TaskStatus.RUNNING.value, executed.task.id))
        conn.execute("UPDATE task_attempts SET status = ? WHERE id = ?", (TaskStatus.RUNNING.value, executed.attempt.id))

    recovery = store.recover_daemon_leases("local_daemon:test:123", pid=123)

    assert len(store.list_runs()) == 1
    assert [task.id for task in recovery.recovered_tasks] == [executed.task.id]
    assert store.get_task(executed.task.id).status == TaskStatus.FAILED
    attempt = store.get_task_attempt(executed.attempt.id)
    assert attempt.status == TaskStatus.FAILED
    assert attempt.failure_code == "dry_run_failed"
    assert store.get_task_lease(executed.lease.id).status == TaskLeaseStatus.RELEASED


def test_store_daemon_recovery_fails_expired_active_dry_run_with_nonterminal_run(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    store.create_task(title="Dry run", metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"})
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None
    executed = store.execute_dry_run_lease(leased.lease.id, owner="local_daemon:test:123")
    with store.connect() as conn:
        conn.execute("UPDATE runs SET status = ? WHERE id = ?", ("running", executed.run.id))
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (TaskStatus.RUNNING.value, executed.task.id))
        conn.execute("UPDATE task_attempts SET status = ? WHERE id = ?", (TaskStatus.RUNNING.value, executed.attempt.id))
        conn.execute(
            "UPDATE task_leases SET status = ?, expires_at = ?, released_at = NULL WHERE id = ?",
            (TaskLeaseStatus.ACTIVE.value, "2026-01-01T00:00:00+00:00", executed.lease.id),
        )

    recovery = store.recover_daemon_leases("local_daemon:test:123", pid=123)

    assert len(store.list_runs()) == 1
    assert [lease.id for lease in recovery.expired_leases] == [executed.lease.id]
    assert store.get_task(executed.task.id).status == TaskStatus.FAILED
    attempt = store.get_task_attempt(executed.attempt.id)
    assert attempt.status == TaskStatus.FAILED
    assert attempt.failure_code == "dry_run_recovery_required"
    assert store.get_task_lease(executed.lease.id).status == TaskLeaseStatus.EXPIRED


def test_store_artifact_evidence_verifies_mismatch_and_missing(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="artifact run", task_type="phase_1a_test")
    artifact_path = tmp_path / ".harness" / "runs" / run.id / "evidence.txt"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("initial", encoding="utf-8")

    artifact = store.register_artifact(
        run.id,
        "evidence",
        artifact_path,
        producer="unit_test",
        redaction_state="redacted",
    )

    assert artifact.sha256 == hashlib.sha256(b"initial").hexdigest()
    assert artifact.size_bytes == len("initial")
    assert artifact.producer == "unit_test"
    assert artifact.redaction_state == "redacted"
    assert store.verify_artifact(artifact.id).evidence_status == "verified"

    artifact_path.write_text("changed", encoding="utf-8")
    assert store.verify_artifact(artifact.id).evidence_status == "mismatch"

    artifact_path.unlink()
    assert store.verify_artifact(artifact.id).evidence_status == "missing"


def test_store_rejects_missing_artifact_registration(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="artifact run", task_type="phase_1a_test")

    try:
        store.register_artifact(run.id, "missing", tmp_path / "missing.txt")
    except FileNotFoundError as exc:
        assert "Artifact path not found:" in str(exc)
    else:
        raise AssertionError("missing artifact should raise")


def test_store_reads_legacy_artifact_rows_after_migration(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="legacy run", task_type="phase_1a_test")
    legacy_path = tmp_path / ".harness" / "runs" / run.id / "legacy.txt"
    legacy_path.write_text("legacy", encoding="utf-8")
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO artifacts (id, run_id, kind, path, created_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("art_legacy", run.id, "legacy", str(legacy_path), "2026-01-01T00:00:00+00:00", "{}"),
        )

    store.initialize()
    artifact = store.get_artifact("art_legacy")

    assert artifact.schema_version == "harness.artifact/v1"
    assert artifact.sha256 is None
    assert artifact.size_bytes is None
    assert artifact.redaction_state == "unknown"
    assert artifact.evidence_status == "unknown"


def test_store_run_baselines_round_trip_replace_and_compare(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    first = store.create_run(goal="baseline", task_type="phase_1a_test")
    second = store.create_run(goal="compare", task_type="phase_1a_test")

    baseline = store.set_run_baseline("local-green", first.id)

    assert baseline.schema_version == "harness.baseline/v1"
    assert baseline.name == "local-green"
    assert baseline.run_id == first.id
    assert baseline.evidence_sha256
    assert baseline.snapshot["run_status"] == {"status": "created"}
    assert store.get_run_baseline("local-green") == baseline

    same = store.compare_runs(first.id, first.id)
    assert same.schema_version == "harness.compare/v1"
    assert same.matches is True
    assert same.changed_sections == []
    assert same.sections["artifacts"]["matches"] is True

    store.update_run_status(second.id, "completed")
    different = store.compare_runs(first.id, second.id)
    assert different.matches is False
    assert "run_status" in different.changed_sections
    assert different.sections["run_status"]["run_a"] == {"status": "created"}
    assert different.sections["run_status"]["run_b"] == {"status": "completed"}

    replaced = store.set_run_baseline("local-green", second.id)
    assert replaced.run_id == second.id
    assert store.get_run_baseline("local-green").run_id == second.id


def test_store_compare_reports_artifact_evidence_drift_without_contents(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    first = store.create_run(goal="first", task_type="phase_1a_test")
    second = store.create_run(goal="second", task_type="phase_1a_test")
    first_artifact = tmp_path / ".harness" / "runs" / first.id / "evidence.txt"
    second_artifact = tmp_path / ".harness" / "runs" / second.id / "evidence.txt"
    first_artifact.write_text("same", encoding="utf-8")
    second_artifact.write_text("same", encoding="utf-8")
    store.register_artifact(first.id, "pytest_stdout", first_artifact)
    artifact = store.register_artifact(second.id, "pytest_stdout", second_artifact)

    assert store.compare_runs(first.id, second.id).matches is False

    second_artifact.write_text("changed secret-looking content", encoding="utf-8")
    result = store.compare_runs(first.id, second.id)
    serialized = json.dumps(result.model_dump(mode="json"))

    assert "artifacts" in result.changed_sections
    assert "test_result_evidence" in result.changed_sections
    assert result.sections["artifacts"]["run_b"][0]["id"] == artifact.id
    assert result.sections["artifacts"]["run_b"][0]["evidence_status"] == "mismatch"
    assert "changed secret-looking content" not in serialized
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized


def test_store_baseline_errors_are_stable(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="run", task_type="phase_1a_test")

    for action, expected in [
        (lambda: store.get_run_baseline("missing"), "Baseline not found: missing"),
        (lambda: store.set_run_baseline("", run.id), "Baseline name is required"),
        (lambda: store.compare_runs(run.id, "run_missing"), "Run not found: run_missing"),
        (
            lambda: store.compare_run_to_baseline(run.id, "missing"),
            "Baseline not found: missing",
        ),
    ]:
        try:
            action()
        except (KeyError, ValueError) as exc:
            assert str(exc).strip("'") == expected
        else:
            raise AssertionError(f"{expected} should raise")


def test_store_creates_lists_filters_and_updates_tasks(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()

    low = store.create_task(title="Low priority", priority=0, agent_id="repo_inspector")
    high = store.create_task(title="High priority", description="Important", priority=5, workbench_id="coding")

    assert store.get_task(high.id).description == "Important"
    assert store.get_task(high.id).status == TaskStatus.READY
    assert store.get_task(high.id).idempotency_key is not None
    assert store.get_task(high.id).idempotency_key.startswith("task_idem_")
    assert [task.id for task in store.list_tasks()] == [high.id, low.id]
    assert [task.id for task in store.list_tasks(status="ready")] == [high.id, low.id]
    assert [task.id for task in store.list_tasks(status="queued")] == [high.id, low.id]

    succeeded = store.update_task_status(low.id, TaskStatus.SUCCEEDED)
    assert succeeded.status == TaskStatus.SUCCEEDED
    assert [task.id for task in store.list_tasks(status="ready")] == [high.id]
    assert [task.id for task in store.list_tasks(status="queued")] == [high.id]


def test_store_creates_lists_and_gets_objectives(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()

    low = store.create_objective(title="Low priority", priority=0, workbench_id="coding")
    high = store.create_objective(
        title="High priority",
        description="Important",
        priority=5,
        workbench_id="quant",
        metadata={"secret": "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz"},
    )

    loaded = store.get_objective(high.id)
    assert loaded.id == high.id
    assert loaded.title == "High priority"
    assert loaded.description == "Important"
    assert loaded.status == ObjectiveStatus.ACTIVE
    assert loaded.workbench_id == "quant"
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in json.dumps(loaded.metadata)
    assert "[REDACTED_SECRET]" in json.dumps(loaded.metadata)
    assert [objective.id for objective in store.list_objectives()] == [high.id, low.id]

    try:
        store.get_objective("obj_missing")
    except KeyError as exc:
        assert str(exc).strip("'") == "Objective not found: obj_missing"
    else:
        raise AssertionError("missing objective should raise")


def test_store_normalizes_legacy_task_statuses(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    timestamp = "2026-01-01T00:00:00+00:00"

    with store.connect() as conn:
        for task_id, status in [
            ("task_queued", "queued"),
            ("task_completed", "completed"),
            ("task_canceled", "canceled"),
        ]:
            conn.execute(
                """
                INSERT INTO tasks (
                  id, title, description, status, project_root, created_at, updated_at,
                  priority, depends_on_json, metadata_json, idempotency_key,
                  required_approvals_json
                ) VALUES (?, ?, '', ?, ?, ?, ?, 0, '[]', '{}', ?, '[]')
                """,
                (task_id, task_id, status, str(tmp_path), timestamp, timestamp, f"idem_{task_id}"),
            )

    assert store.get_task("task_queued").status == TaskStatus.READY
    assert store.get_task("task_completed").status == TaskStatus.SUCCEEDED
    assert store.get_task("task_canceled").status == TaskStatus.CANCELLED
    assert [task.id for task in store.list_tasks(status="queued")] == ["task_queued"]
    assert [task.id for task in store.list_tasks(status="completed")] == ["task_completed"]
    assert [task.id for task in store.list_tasks(status="canceled")] == ["task_canceled"]


def test_store_task_transitions_are_validated_and_recorded(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    task = store.create_task(title="Task")

    created_transitions = store.list_task_transitions(task.id)
    assert len(created_transitions) == 1
    assert created_transitions[0].from_status is None
    assert created_transitions[0].to_status == TaskStatus.READY
    assert created_transitions[0].reason == "task_created"

    updated = store.update_task_status(task.id, TaskStatus.SUCCEEDED)
    assert updated.status == TaskStatus.SUCCEEDED
    transitions = store.list_task_transitions(task.id)
    assert len(transitions) == 2
    assert transitions[1].from_status == TaskStatus.READY
    assert transitions[1].to_status == TaskStatus.SUCCEEDED
    assert transitions[1].reason == "status_updated"

    try:
        store.update_task_status(task.id, TaskStatus.READY)
    except ValueError as exc:
        assert "Invalid task transition: succeeded -> ready" in str(exc)
    else:
        raise AssertionError("invalid transition should raise")


def test_store_tasks_support_objectives_dependencies_approvals_and_graph(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    objective = store.create_objective(title="Objective", priority=3)
    upstream = store.create_task(title="Upstream", objective_id=objective.id, priority=10)
    downstream = store.create_task(
        title="Downstream",
        objective_id=objective.id,
        depends_on=[upstream.id],
        required_approvals=["hosted_provider"],
        priority=5,
    )

    loaded = store.get_task(downstream.id)
    assert loaded.objective_id == objective.id
    assert loaded.status == TaskStatus.WAITING_APPROVAL
    assert loaded.depends_on == [upstream.id]
    assert loaded.required_approvals == ["hosted_provider"]
    dependencies = store.list_task_dependencies(downstream.id)
    assert len(dependencies) == 1
    assert dependencies[0].upstream_task_id == upstream.id
    assert dependencies[0].downstream_task_id == downstream.id

    graph = store.build_task_graph(objective_id=objective.id)
    assert [item["id"] for item in graph["objectives"]] == [objective.id]
    assert {item["id"] for item in graph["tasks"]} == {upstream.id, downstream.id}
    assert graph["dependencies"][0]["upstream_task_id"] == upstream.id
    assert graph["blocked_reasons"][downstream.id] == [
        {"kind": "unsatisfied_dependency", "task_id": upstream.id, "status": "ready"},
        {
            "kind": "unresolved_required_approvals",
            "required_approvals": ["hosted_provider"],
            "approval_state": "required",
        },
    ]


def test_store_tasks_reject_unknown_references_and_dependency_cycles(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    first = store.create_task(title="First")
    second = store.create_task(title="Second", depends_on=[first.id])

    for create_kwargs, expected in [
        ({"title": "Bad objective", "objective_id": "obj_missing"}, "Objective not found: obj_missing"),
        ({"title": "Bad dependency", "depends_on": ["task_missing"]}, "Task not found: task_missing"),
    ]:
        try:
            store.create_task(**create_kwargs)
        except KeyError as exc:
            assert str(exc).strip("'") == expected
        else:
            raise AssertionError(f"{expected} should raise")

    try:
        store.create_task_dependency(second.id, first.id)
    except ValueError as exc:
        assert "Task dependency cycle detected" in str(exc)
    else:
        raise AssertionError("dependency cycle should raise")


def test_store_task_errors_are_stable_for_unknown_or_invalid_status(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    task = store.create_task(title="Task")

    try:
        store.get_task("task_missing")
    except KeyError as exc:
        assert str(exc).strip("'") == "Task not found: task_missing"
    else:
        raise AssertionError("missing task should raise")

    try:
        store.update_task_status(task.id, "invalid")
    except ValueError as exc:
        assert "invalid" in str(exc)
    else:
        raise AssertionError("invalid status should raise")


def test_store_cancel_task_uses_transition_rules(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    ready = store.create_task(title="Ready")
    failed = store.create_task(title="Failed")
    store.update_task_status(failed.id, TaskStatus.FAILED)

    cancelled = store.cancel_task(ready.id)
    cancelled_failed = store.cancel_task(failed.id)

    assert cancelled.status == TaskStatus.CANCELLED
    assert cancelled_failed.status == TaskStatus.CANCELLED
    assert store.list_task_transitions(ready.id)[-1].to_status == TaskStatus.CANCELLED

    succeeded = store.create_task(title="Succeeded")
    store.update_task_status(succeeded.id, TaskStatus.SUCCEEDED)
    for task_id, expected in [
        (succeeded.id, "Invalid task transition: succeeded -> cancelled"),
        (cancelled.id, "Invalid task transition: cancelled -> cancelled"),
    ]:
        try:
            store.cancel_task(task_id)
        except ValueError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f"{expected} should raise")


def test_store_retry_task_targets_ready_blocked_or_waiting_approval(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    dependency = store.create_task(title="Dependency")
    ready_retry = store.create_task(title="Ready retry")
    blocked_retry = store.create_task(title="Blocked retry", depends_on=[dependency.id])
    approval_retry = store.create_task(title="Approval retry", required_approvals=["hosted_provider"])
    ready_idempotency_key = ready_retry.idempotency_key

    store.update_task_status(ready_retry.id, TaskStatus.RUNNING)
    store.update_task_status(ready_retry.id, TaskStatus.FAILED)
    store.update_task_status(blocked_retry.id, TaskStatus.CANCELLED)
    with store.connect() as conn:
        conn.execute(
            "UPDATE tasks SET status = ? WHERE id IN (?, ?)",
            (TaskStatus.FAILED.value, blocked_retry.id, approval_retry.id),
        )

    assert store.retry_task(ready_retry.id).status == TaskStatus.READY
    assert store.get_task(ready_retry.id).idempotency_key == ready_idempotency_key
    assert store.retry_task(blocked_retry.id).status == TaskStatus.BLOCKED
    assert store.retry_task(approval_retry.id).status == TaskStatus.WAITING_APPROVAL
    assert store.list_task_transitions(ready_retry.id)[-1].to_status == TaskStatus.READY

    try:
        store.retry_task(dependency.id)
    except ValueError as exc:
        assert "Task retry requires failed status: ready" in str(exc)
    else:
        raise AssertionError("retry from non-failed should raise")


def test_store_select_next_task_uses_priority_and_dependencies(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    dependency = store.create_task(title="Dependency", priority=10)
    blocked_by_dependency = store.create_task(
        title="Blocked by dependency",
        priority=100,
        depends_on=[dependency.id],
    )
    runnable = store.create_task(title="Runnable", priority=1)
    cancelled = store.create_task(title="Canceled", priority=200)
    store.update_task_status(cancelled.id, TaskStatus.CANCELLED)

    selected = store.select_next_task()

    assert selected is not None
    assert selected.id == dependency.id
    assert selected.status == TaskStatus.LEASED
    assert store.get_task(blocked_by_dependency.id).status == TaskStatus.BLOCKED

    store.update_task_status(dependency.id, TaskStatus.RUNNING)
    store.update_task_status(dependency.id, TaskStatus.SUCCEEDED)
    selected_after_dependency = store.select_next_task()

    assert selected_after_dependency is not None
    assert selected_after_dependency.id == blocked_by_dependency.id
    assert selected_after_dependency.status == TaskStatus.LEASED
    assert store.get_task(runnable.id).status == TaskStatus.READY


def test_store_select_next_task_for_lease_creates_attempt_and_active_lease(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    low = store.create_task(title="Low", priority=0)
    high = store.create_task(title="High", priority=10)

    selection = store.select_next_task_for_lease()

    assert selection is not None
    assert selection["task"].id == high.id
    assert selection["task"].status == TaskStatus.LEASED
    assert selection["attempt"].task_id == high.id
    assert selection["attempt"].attempt_number == 1
    assert selection["attempt"].status == TaskStatus.LEASED
    assert selection["lease"].task_id == high.id
    assert selection["lease"].attempt_id == selection["attempt"].id
    assert selection["lease"].owner == "manual_cli"
    assert selection["lease"].status == TaskLeaseStatus.ACTIVE
    assert store.get_task(low.id).status == TaskStatus.READY
    assert store.list_task_attempts(high.id) == [selection["attempt"]]
    assert store.list_task_leases(high.id) == [selection["lease"]]


def test_store_select_next_task_for_lease_skips_active_leases_and_duplicate_calls(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    first = store.create_task(title="First", priority=10)
    second = store.create_task(title="Second", priority=5)

    first_selection = store.select_next_task_for_lease()
    second_selection = store.select_next_task_for_lease()

    assert first_selection is not None
    assert second_selection is not None
    assert first_selection["task"].id == first.id
    assert second_selection["task"].id == second.id
    assert second_selection["task"].id != first_selection["task"].id


def test_store_select_next_task_for_lease_increments_attempt_numbers(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    task = store.create_task(title="Retryable")

    first_selection = store.select_next_task_for_lease()
    assert first_selection is not None

    with store.connect() as conn:
        conn.execute(
            "UPDATE task_leases SET status = ?, released_at = ? WHERE id = ?",
            (
                TaskLeaseStatus.RELEASED.value,
                first_selection["lease"].acquired_at.isoformat(),
                first_selection["lease"].id,
            ),
        )
    store.update_task_status(task.id, TaskStatus.READY)

    second_selection = store.select_next_task_for_lease()

    assert second_selection is not None
    assert second_selection["task"].id == task.id
    assert second_selection["attempt"].attempt_number == 2


def test_store_select_next_task_for_lease_handles_blocked_and_approval_tasks(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    dependency = store.create_task(title="Dependency", priority=10)
    blocked = store.create_task(title="Blocked", priority=9, depends_on=[dependency.id])
    waiting_approval = store.create_task(
        title="Approval",
        priority=100,
        required_approvals=["hosted_provider"],
    )

    assert store.select_next_task_for_lease()["task"].id == dependency.id
    assert store.select_next_task_for_lease() is None
    assert store.get_task(blocked.id).status == TaskStatus.BLOCKED
    assert store.get_task(waiting_approval.id).status == TaskStatus.WAITING_APPROVAL

    store.update_task_status(dependency.id, TaskStatus.RUNNING)
    store.update_task_status(dependency.id, TaskStatus.SUCCEEDED)
    selected = store.select_next_task_for_lease()

    assert selected is not None
    assert selected["task"].id == blocked.id
    assert selected["task"].status == TaskStatus.LEASED
