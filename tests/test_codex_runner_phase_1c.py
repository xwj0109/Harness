from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from harness.approvals import ApprovalProfile, ApprovalStore
from harness.backends.codex_cli import CodexCliBackend, CodexRunResult
from harness.codex_runner import CodexRepoPlanningRunner, HostedSecretBlocked
from harness.config import default_config
from harness.memory.sqlite_store import SQLiteStore
from harness.models import BackendCapabilities


def approval(tmp_path):
    return ApprovalProfile(
        id="appr_test",
        backend="codex_cli",
        project_root=str(tmp_path),
        data_boundary="hosted_provider",
        task_types=["repo_planning"],
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        created_at=datetime.now(timezone.utc),
    )


class FakeCodexBackend(CodexCliBackend):
    def __init__(self, config, result, supports_sandbox=True):
        super().__init__(config)
        self.result = result
        self.supports_sandbox = supports_sandbox

    def preflight(self):
        from harness.models import BackendStatus

        return BackendStatus(
            available=True,
            metadata=self.config.metadata,
            capabilities=BackendCapabilities(
                supports_exec=True,
                supports_read_only_sandbox=self.supports_sandbox,
                supports_json_events=True,
            ),
        )

    def run_read_only(self, project_root, prompt, final_message_path):
        if final_message_path:
            final_message_path.write_text(self.result.final_message or "", encoding="utf-8")
        return self.result


def test_hosted_payload_secret_scanning_blocks_and_redacts(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    result = CodexRunResult([], "", "", 0, [], "ok")
    backend = FakeCodexBackend(default_config().backends["codex_cli"], result)
    runner = CodexRepoPlanningRunner(tmp_path, store, backend, ApprovalStore(tmp_path))
    with pytest.raises(HostedSecretBlocked):
        runner.run(
            "plan with OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz",
            "repo_planning",
            approval(tmp_path),
        )


def test_codex_runner_persists_metadata_and_artifacts(tmp_path, monkeypatch) -> None:
    subprocess_outputs = iter(["", ""])

    def fake_git(args, **kwargs):
        assert args == ["git", "status", "--porcelain"]
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=next(subprocess_outputs), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_git)
    store = SQLiteStore(tmp_path)
    store.initialize()
    result = CodexRunResult(
        command=["codex", "exec", "--sandbox", "read-only", "plan"],
        stdout='{"event":"done"}\n',
        stderr="",
        exit_status=0,
        json_events=[{"event": "done"}],
        final_message="Plan safely.",
    )
    backend = FakeCodexBackend(default_config().backends["codex_cli"], result)
    output = CodexRepoPlanningRunner(tmp_path, store, backend, ApprovalStore(tmp_path)).run(
        "plan safest fix",
        "repo_planning",
        approval(tmp_path),
    )
    run = store.get_run(output["run_id"])
    assert run.approval_id == "appr_test"
    assert run.status == "completed"
    artifacts = {artifact.kind for artifact in store.list_artifacts(run.id)}
    assert {"codex_stdout", "codex_stderr", "codex_events", "codex_final_message"} <= artifacts


def test_codex_repo_planning_run_existing_uses_existing_run_without_second_run(tmp_path, monkeypatch) -> None:
    subprocess_outputs = iter(["", ""])

    def fake_git(args, **kwargs):
        assert args == ["git", "status", "--porcelain"]
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=next(subprocess_outputs), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_git)
    store = SQLiteStore(tmp_path)
    store.initialize()
    approval_profile = approval(tmp_path)
    run = store.create_run(
        goal="plan safest fix",
        task_type="repo_planning",
        status="running",
        backend=default_config().backends["codex_cli"],
        approval_id=approval_profile.id,
    )
    result = CodexRunResult(
        command=["codex", "exec", "--sandbox", "read-only", "plan"],
        stdout="",
        stderr="",
        exit_status=0,
        json_events=[],
        final_message="Plan safely.",
    )
    backend = FakeCodexBackend(default_config().backends["codex_cli"], result)

    output = CodexRepoPlanningRunner(tmp_path, store, backend, ApprovalStore(tmp_path)).run_existing(
        run.id,
        "plan safest fix",
        "repo_planning",
        approval_profile,
    )

    assert output["run_id"] == run.id
    assert len(store.list_runs()) == 1
    assert store.get_run(run.id).status == "completed"
    artifacts = {artifact.kind for artifact in store.list_artifacts(run.id)}
    assert {"events", "transcript", "final_report", "codex_final_message"} <= artifacts


def test_codex_runner_detects_policy_violation_on_dirty_post_status(tmp_path, monkeypatch) -> None:
    subprocess_outputs = iter(["", " M file.py\n"])

    def fake_git(args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=next(subprocess_outputs), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_git)
    store = SQLiteStore(tmp_path)
    store.initialize()
    result = CodexRunResult(["codex"], "", "", 0, [], "Plan")
    backend = FakeCodexBackend(default_config().backends["codex_cli"], result)
    output = CodexRepoPlanningRunner(tmp_path, store, backend, ApprovalStore(tmp_path)).run(
        "plan",
        "repo_planning",
        approval(tmp_path),
    )
    run = store.get_run(output["run_id"])
    assert run.status == "policy_violation"
    assert output["policy_violation"] is True
    events = store.list_events(run.id)
    assert any(event.event_type == "policy_violation" for event in events)
    report = tmp_path / ".harness" / "runs" / output["run_id"] / "final_report.md"
    assert "POLICY VIOLATION" in report.read_text(encoding="utf-8")


def test_codex_runner_sanitizes_stdout_stderr_events_and_report(tmp_path, monkeypatch) -> None:
    subprocess_outputs = iter(["", ""])

    def fake_git(args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=next(subprocess_outputs), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_git)
    store = SQLiteStore(tmp_path)
    store.initialize()
    secret = "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz"
    result = CodexRunResult(
        command=["codex", "exec", "--sandbox", "read-only", "plan"],
        stdout=secret,
        stderr=secret,
        exit_status=0,
        json_events=[{"message": secret}],
        final_message=secret,
    )
    backend = FakeCodexBackend(default_config().backends["codex_cli"], result)
    output = CodexRepoPlanningRunner(tmp_path, store, backend, ApprovalStore(tmp_path)).run(
        "plan",
        "repo_planning",
        approval(tmp_path),
    )
    run_dir = tmp_path / ".harness" / "runs" / output["run_id"]
    for path in [
        run_dir / "codex_stdout.txt",
        run_dir / "codex_stderr.txt",
        run_dir / "codex_events.jsonl",
        run_dir / "codex_final_message.md",
        run_dir / "transcript.jsonl",
        run_dir / "events.jsonl",
        run_dir / "final_report.md",
    ]:
        assert "sk-abcdefghijklmnopqrstuvwxyz" not in path.read_text(encoding="utf-8")
