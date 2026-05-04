from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from harness.approvals import ApprovalProfile, ApprovalStore
from harness.backends.codex_cli import CodexCliBackend, CodexRunResult, NETWORK_NOT_ENFORCEABLE
from harness.codex_edit_runner import ApplyBackDecision, CodexCodeEditRunner
from harness.config import default_config
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
