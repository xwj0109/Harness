from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from harness.approvals import ApprovalProfile, ApprovalStore
from harness.backends.codex_cli import CodexCliBackend, CodexRunResult, NETWORK_NOT_ENFORCEABLE
from harness.codex_edit_runner import ApplyBackDecision, CodexCodeEditRunner
from harness.config import default_config, write_default_config
from harness.execution import execute_lease
from harness.memory.sqlite_store import SQLiteStore
from harness.models import BackendCapabilities, BackendStatus


def approval(project, task_type="codex_code_edit") -> ApprovalProfile:
    return ApprovalProfile(
        id="appr_edit",
        backend="codex_cli",
        project_root=str(project),
        data_boundary="hosted_provider",
        task_types=[task_type],
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        created_at=datetime.now(timezone.utc),
    )


def init_clean_project(project: Path, extra_files: dict[str, str] | None = None) -> None:
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)
    (project / ".gitignore").write_text(".harness/\n", encoding="utf-8")
    (project / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    for relative_path, text in (extra_files or {}).items():
        path = project / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)


class StaticApplyBackApproval:
    def __init__(self, decision: str):
        self.decision = decision

    def decide(self, diff_summary: str, full_diff: str, diff_artifact: Path) -> ApplyBackDecision:
        assert diff_artifact.exists()
        return ApplyBackDecision(decision=self.decision)


class FakeEditBackend(CodexCliBackend):
    def __init__(self, config, edit=None, final_message="advisory"):
        super().__init__(config)
        self.edit = edit or (lambda isolated: (Path(isolated) / "app.py").write_text("value = 2\n", encoding="utf-8"))
        self.final_message = final_message

    def preflight(self):
        return BackendStatus(
            available=True,
            metadata=self.config.metadata,
            capabilities=BackendCapabilities(
                supports_exec=True,
                supports_cd=True,
                supports_workspace_write_sandbox=True,
                supports_ask_for_approval=True,
                supports_json_events=True,
                supports_output_last_message=True,
            ),
        )

    def run_edit(self, isolated_workspace, prompt, final_message_path):
        self.edit(Path(isolated_workspace))
        if final_message_path:
            final_message_path.write_text(self.final_message, encoding="utf-8")
        return (
            CodexRunResult(
                ["codex", "exec", "--cd", str(isolated_workspace), "--sandbox", "workspace-write"],
                "",
                "",
                0,
                [],
                self.final_message,
            ),
            self.preflight().capabilities,
            NETWORK_NOT_ENFORCEABLE,
        )


def run_edit(project: Path, backend: CodexCliBackend, decision: str):
    store = SQLiteStore(project)
    store.initialize()
    return CodexCodeEditRunner(
        project,
        store,
        backend,
        ApprovalStore(project),
        apply_back_approval_provider=StaticApplyBackApproval(decision),
    ).run("change value", "codex_code_edit", approval(project))


def test_denied_apply_back_leaves_active_project_byte_for_byte_unchanged(tmp_path) -> None:
    init_clean_project(tmp_path)
    before = (tmp_path / "app.py").read_bytes()

    result = run_edit(tmp_path, FakeEditBackend(default_config().backends["codex_cli"]), "denied")

    assert result["status"] == "completed_denied"
    assert result["apply_back_decision"] == "denied"
    assert (tmp_path / "app.py").read_bytes() == before
    events = SQLiteStore(tmp_path).list_events(result["run_id"])
    assert any(event.event_type == "apply_back_decision" and event.payload["decision"] == "denied" for event in events)


def test_approved_apply_back_modifies_only_validated_existing_text_files(tmp_path) -> None:
    init_clean_project(tmp_path, {"other.py": "other = 1\n"})
    before_other = (tmp_path / "other.py").read_bytes()

    result = run_edit(tmp_path, FakeEditBackend(default_config().backends["codex_cli"]), "approved")

    assert result["status"] == "completed_applied"
    assert result["applied_files"] == ["app.py"]
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert (tmp_path / "other.py").read_bytes() == before_other
    assert result["freshness_result"]["ok"] is True


def test_apply_back_never_uses_codex_final_message(tmp_path) -> None:
    init_clean_project(tmp_path)
    backend = FakeEditBackend(
        default_config().backends["codex_cli"],
        final_message="Please set app.py to value = 999",
    )

    result = run_edit(tmp_path, backend, "approved")

    assert result["status"] == "completed_applied"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert "999" not in (tmp_path / "app.py").read_text(encoding="utf-8")


def test_active_project_change_during_codex_execution_fails_freshness_closed(tmp_path) -> None:
    init_clean_project(tmp_path)

    def edit(isolated: Path) -> None:
        (isolated / "app.py").write_text("value = 2\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("value = 9\n", encoding="utf-8")

    result = run_edit(tmp_path, FakeEditBackend(default_config().backends["codex_cli"], edit=edit), "approved")

    assert result["status"] == "apply_back_failed"
    assert "changed since isolation" in result["apply_back_failure"]
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 9\n"


def test_target_file_hash_mismatch_blocks_apply_back(tmp_path) -> None:
    init_clean_project(tmp_path)

    def edit(isolated: Path) -> None:
        (isolated / "app.py").write_text("value = 2\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("value = 3\n", encoding="utf-8")

    result = run_edit(tmp_path, FakeEditBackend(default_config().backends["codex_cli"], edit=edit), "approved")

    assert result["status"] == "apply_back_failed"
    checks = result["freshness_result"]["target_hash_checks"]
    assert checks[0]["path"] == "app.py"
    assert checks[0]["matches"] is False
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 3\n"


@pytest.mark.parametrize(
    "edit",
    [
        lambda isolated: (isolated / "new.py").write_text("new\n", encoding="utf-8"),
        lambda isolated: (isolated / "app.py").unlink(),
        lambda isolated: (isolated / "app.py").write_bytes(b"\x00\x01"),
        lambda isolated: ((isolated / "target.py").write_text("target\n", encoding="utf-8"), (isolated / "app.py").unlink(), (isolated / "app.py").symlink_to("target.py")),
        lambda isolated: ((isolated / ".harness").mkdir(), (isolated / ".harness" / "config.yaml").write_text("x\n", encoding="utf-8")),
    ],
)
def test_unsupported_or_blocked_changes_are_not_applied(tmp_path, edit) -> None:
    init_clean_project(tmp_path)
    before = (tmp_path / "app.py").read_bytes()

    result = run_edit(tmp_path, FakeEditBackend(default_config().backends["codex_cli"], edit=edit), "approved")

    assert result["status"] == "policy_violation"
    assert result["applied_files"] == []
    assert (tmp_path / "app.py").read_bytes() == before


def test_apply_back_failure_is_atomic_and_restores_pre_apply_bytes(tmp_path, monkeypatch) -> None:
    init_clean_project(tmp_path, {"other.py": "other = 1\n"})

    def edit(isolated: Path) -> None:
        (isolated / "app.py").write_text("value = 2\n", encoding="utf-8")
        (isolated / "other.py").write_text("other = 2\n", encoding="utf-8")

    original_write_bytes = Path.write_bytes

    def flaky_write_bytes(path: Path, data: bytes):
        if path == tmp_path / "other.py":
            raise OSError("simulated write failure")
        return original_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", flaky_write_bytes)
    result = run_edit(tmp_path, FakeEditBackend(default_config().backends["codex_cli"], edit=edit), "approved")

    assert result["status"] == "apply_back_failed"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    assert (tmp_path / "other.py").read_text(encoding="utf-8") == "other = 1\n"


def test_keep_isolation_remains_correct_after_approved_apply_back(tmp_path) -> None:
    init_clean_project(tmp_path)
    store = SQLiteStore(tmp_path)
    store.initialize()
    write_default_config(tmp_path)
    result = CodexCodeEditRunner(
        tmp_path,
        store,
        FakeEditBackend(default_config().backends["codex_cli"]),
        ApprovalStore(tmp_path),
        apply_back_approval_provider=StaticApplyBackApproval("approved"),
    ).run("change value", "codex_code_edit", approval(tmp_path), keep_isolation=True)

    assert result["status"] == "completed_applied"
    assert result["isolation_cleanup_status"] == "kept"
    assert Path(result["isolated_workspace"]).exists()
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 2\n"


def test_generated_artifacts_do_not_block_valid_source_apply_back(tmp_path) -> None:
    init_clean_project(tmp_path, {"scratch_codex_edit.py": "value = 1\n"})

    def edit(isolated: Path) -> None:
        (isolated / "scratch_codex_edit.py").write_text("value = 2\n", encoding="utf-8")
        (isolated / "agent_harness.egg-info").mkdir()
        (isolated / "agent_harness.egg-info" / "SOURCES.txt").write_text("generated\n", encoding="utf-8")
        (isolated / "harness").mkdir(exist_ok=True)
        (isolated / "harness" / ".DS_Store").write_text("local\n", encoding="utf-8")

    result = run_edit(tmp_path, FakeEditBackend(default_config().backends["codex_cli"], edit=edit), "approved")

    assert result["status"] == "completed_applied"
    assert result["applied_files"] == ["scratch_codex_edit.py"]
    assert result["changed_files"] == ["scratch_codex_edit.py"]
    assert sorted(result["ignored_generated_artifacts"]) == [
        "agent_harness.egg-info/SOURCES.txt",
        "harness/.DS_Store",
    ]
    assert (tmp_path / "scratch_codex_edit.py").read_text(encoding="utf-8") == "value = 2\n"
    assert not (tmp_path / "agent_harness.egg-info").exists()
    run_dir = tmp_path / ".harness" / "runs" / result["run_id"]
    patch = (run_dir / "isolated_unified_diff.patch").read_text(encoding="utf-8")
    assert "scratch_codex_edit.py" in patch
    assert "agent_harness.egg-info" not in patch
    assert ".DS_Store" not in patch


def test_generated_only_changes_have_no_apply_back_and_no_policy_violation(tmp_path) -> None:
    init_clean_project(tmp_path)

    def edit(isolated: Path) -> None:
        (isolated / ".DS_Store").write_text("local\n", encoding="utf-8")
        (isolated / "agent_harness.egg-info").mkdir()
        (isolated / "agent_harness.egg-info" / "PKG-INFO").write_text("generated\n", encoding="utf-8")

    before = (tmp_path / "app.py").read_bytes()
    result = run_edit(tmp_path, FakeEditBackend(default_config().backends["codex_cli"], edit=edit), "approved")

    assert result["status"] == "completed"
    assert result["changed_files"] == []
    assert result["applied_files"] == []
    assert result["policy_violations"] == []
    assert sorted(result["ignored_generated_artifacts"]) == [".DS_Store", "agent_harness.egg-info/PKG-INFO"]
    assert (tmp_path / "app.py").read_bytes() == before


def test_true_blocked_paths_still_block_apply_back_even_with_generated_artifacts(tmp_path) -> None:
    init_clean_project(tmp_path)
    before = (tmp_path / "app.py").read_bytes()

    def edit(isolated: Path) -> None:
        (isolated / "app.py").write_text("value = 2\n", encoding="utf-8")
        (isolated / ".env").write_text("SECRET=value\n", encoding="utf-8")
        (isolated / ".DS_Store").write_text("local\n", encoding="utf-8")

    result = run_edit(tmp_path, FakeEditBackend(default_config().backends["codex_cli"], edit=edit), "approved")

    assert result["status"] == "policy_violation"
    assert result["applied_files"] == []
    assert any(violation["path"] == ".env" for violation in result["policy_violations"])
    assert result["ignored_generated_artifacts"] == [".DS_Store"]
    assert (tmp_path / "app.py").read_bytes() == before


def test_codex_run_existing_uses_existing_run_without_second_run(tmp_path) -> None:
    init_clean_project(tmp_path)
    store = SQLiteStore(tmp_path)
    store.initialize()
    write_default_config(tmp_path)
    approval_profile = approval(tmp_path)
    run = store.create_run(
        goal="change value",
        task_type="codex_code_edit",
        status="running",
        backend=default_config().backends["codex_cli"],
        approval_id=approval_profile.id,
    )

    result = CodexCodeEditRunner(
        tmp_path,
        store,
        FakeEditBackend(default_config().backends["codex_cli"]),
        ApprovalStore(tmp_path),
    ).run_existing(run.id, "change value", "codex_code_edit", approval_profile)

    assert result["run_id"] == run.id
    assert len(store.list_runs()) == 1
    assert store.get_run(run.id).status == "completed_denied"
    assert {artifact.kind for artifact in store.list_artifacts(run.id)} >= {
        "events",
        "transcript",
        "final_report",
        "isolated_unified_diff",
    }


def test_codex_isolated_adapter_missing_approval_rejects_before_run(tmp_path) -> None:
    init_clean_project(tmp_path)
    store = SQLiteStore(tmp_path)
    store.initialize()
    store.create_task(
        title="Codex edit",
        metadata={"execution_adapter": "codex_isolated_edit", "task_type": "codex_code_edit"},
    )
    leased = store.select_next_task_for_lease(owner="local_daemon:test:123")
    assert leased is not None

    result = execute_lease(tmp_path, leased["lease"].id, owner="local_daemon:test:123")

    assert result.ok is False
    assert result.decision == "codex_isolated_edit_blocked_policy"
    assert result.run is None
    assert "Missing valid hosted-provider Codex approval" in result.rejection_reasons[0]
    assert store.list_runs() == []
    assert any(event.event_type == "execution_adapter_rejected" for event in store.list_daemon_events())


def test_codex_isolated_adapter_denied_apply_back_succeeds_without_mutation(tmp_path, monkeypatch) -> None:
    init_clean_project(tmp_path)
    before = (tmp_path / "app.py").read_bytes()
    store = SQLiteStore(tmp_path)
    store.initialize()
    write_default_config(tmp_path)
    ApprovalStore(tmp_path).add(
        backend="codex_cli",
        data_boundary="hosted_provider",
        task_types=["codex_code_edit"],
        duration_days=1,
    )
    task = store.create_task(
        title="Codex edit",
        metadata={"execution_adapter": "codex_isolated_edit", "task_type": "codex_code_edit"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None
    monkeypatch.setattr("harness.execution.CodexCliBackend", FakeEditBackend)

    result = execute_lease(tmp_path, leased.lease.id, owner="local_daemon:test:123")

    assert result.ok is True
    assert result.decision == "codex_isolated_edit_completed_denied"
    assert result.run is not None
    assert result.run.status == "completed_denied"
    assert result.task is not None
    assert result.task.id == task.id
    assert result.task.status.value == "succeeded"
    assert result.attempt is not None
    assert result.attempt.status.value == "succeeded"
    assert (tmp_path / "app.py").read_bytes() == before
    assert result.adapter_result["apply_back_decision"] == "denied"


def test_codex_isolated_adapter_rejects_requires_hosted_boundary_metadata(tmp_path) -> None:
    init_clean_project(tmp_path)
    store = SQLiteStore(tmp_path)
    store.initialize()
    write_default_config(tmp_path)
    ApprovalStore(tmp_path).add(
        backend="codex_cli",
        data_boundary="hosted_provider",
        task_types=["codex_code_edit"],
        duration_days=1,
    )
    store.create_task(
        title="Codex edit",
        metadata={
            "execution_adapter": "codex_isolated_edit",
            "task_type": "codex_code_edit",
            "requires_hosted_boundary": True,
        },
    )
    leased = store.select_next_task_for_lease(owner="local_daemon:test:123")
    assert leased is not None

    result = execute_lease(tmp_path, leased["lease"].id, owner="local_daemon:test:123")

    assert result.ok is False
    assert result.decision == "execution_adapter_rejected"
    assert result.run is None
    assert "requires_hosted_boundary" in result.rejection_reasons[0]
