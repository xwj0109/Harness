from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from typer.testing import CliRunner

from harness.approvals import ApprovalProfile, ApprovalStore
from harness.backends.codex_cli import CodexCliBackend, CodexRunResult, NETWORK_NOT_ENFORCEABLE
from harness.cli.main import app
from harness.codex_edit_runner import ApplyBackDecision, CodexCodeEditRunner
from harness.config import default_config
from harness.live_artifacts import write_live_run_artifacts
from harness.memory.sqlite_store import SQLiteStore
from harness.models import BackendCapabilities, BackendStatus, RunEventType
from harness.sandbox import DockerRunResult, DockerSandboxConfig


runner = CliRunner()


def test_golden_live_inspect_replays_transcript_artifacts(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="inspect live run", task_type="read_only_repo_summary", status="completed")
    store.append_run_event(run.id, RunEventType.RUN_STARTED, {"agent": "repo_inspector"})
    store.append_run_event(run.id, RunEventType.POLICY_RESOLVED, {"hosted_provider": "approved"})
    store.append_run_event(run.id, RunEventType.BACKEND_STARTED, {"backend": "mock", "streaming": True})
    store.append_run_event(run.id, RunEventType.MODEL_MESSAGE_DELTA, {"delta": "I will inspect the repository."})
    store.append_run_event(run.id, RunEventType.TOOL_CALL_STARTED, {"tool": "repo_read"})
    store.append_run_event(run.id, RunEventType.TOOL_CALL_FINISHED, {"tool": "repo_read"})
    store.append_run_event(run.id, RunEventType.RUN_FINISHED, {"status": "completed"})

    paths = write_live_run_artifacts(store, run.id)
    events = runner.invoke(app, ["events", run.id, "--project", str(tmp_path), "--jsonl"])
    transcript = runner.invoke(app, ["transcript", run.id, "--project", str(tmp_path)])

    assert events.exit_code == 0, events.output
    assert [json.loads(line)["seq"] for line in events.output.splitlines()] == list(range(1, 8))
    assert transcript.exit_code == 0, transcript.output
    assert "● Tool call: repo_read" in transcript.output
    assert paths["transcript"].exists()
    assert paths["procedure"].exists()
    assert paths["final_report"].exists()
    manifest_kinds = {artifact.kind for artifact in store.build_run_manifest(run.id).artifacts}
    assert {"events", "transcript", "procedure", "final_report", "manifest"} <= manifest_kinds


def test_golden_live_fake_edit_streamed_diff_report_and_deny_apply_unchanged_repo(tmp_path) -> None:
    _init_clean_project(tmp_path)
    before = (tmp_path / "app.py").read_bytes()

    result = _run_fake_edit(tmp_path, decision="denied")

    assert result["status"] == "completed_denied"
    assert (tmp_path / "app.py").read_bytes() == before
    store = SQLiteStore(tmp_path)
    events = store.list_events(result["run_id"])
    event_types = [event.event_type for event in events]
    assert "workspace.prepared" in event_types
    assert "backend.started" in event_types
    assert "file.write" in event_types
    assert "diff.updated" in event_types
    assert "approval.required" in event_types
    assert "run.finished" in event_types
    assert event_types.index("approval.required") < event_types.index("apply_back_decision")

    paths = write_live_run_artifacts(store, result["run_id"])
    report = paths["final_report"].read_text(encoding="utf-8")
    assert "src/parser.py" not in report
    assert "app.py" in report
    assert "active_repo_apply_back" in json.dumps([event.payload for event in events])


def test_golden_live_fake_edit_approve_apply_changes_repo(tmp_path) -> None:
    _init_clean_project(tmp_path)

    result = _run_fake_edit(tmp_path, decision="approved")

    assert result["status"] == "completed_applied"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    store = SQLiteStore(tmp_path)
    events = store.list_events(result["run_id"])
    assert any(event.event_type == "apply_back_applied" for event in events)
    assert any(event.event_type == "run.finished" and event.payload["status"] == "completed_applied" for event in events)


def test_golden_live_docker_denied_stream_has_no_docker_invocation(tmp_path, monkeypatch) -> None:
    ResettableDockerRunner.reset()
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    monkeypatch.setattr("harness.test_runner.DockerSandboxRunner", ResettableDockerRunner)

    result = runner.invoke(app, ["tests", "run", "--project", str(tmp_path), "--", "pytest", "-q"], input="d\n")

    assert result.exit_code == 0, result.output
    assert ResettableDockerRunner.run_calls == []
    run_id = result.output.split("Created run ", 1)[1].splitlines()[0]
    events = SQLiteStore(tmp_path).list_events(run_id)
    assert any(event.event_type == "approval.required" and event.payload["approval_kind"] == "docker_execution" for event in events)
    assert any(event.event_type == "test.finished" and event.payload["status"] == "execution_denied" for event in events)
    assert any(event.event_type == "run.finished" and event.payload["status"] == "execution_denied" for event in events)


def test_golden_live_docker_approved_streams_stdout_stderr(tmp_path, monkeypatch) -> None:
    ResettableDockerRunner.reset()
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    monkeypatch.setattr("harness.test_runner.DockerSandboxRunner", ResettableDockerRunner)

    result = runner.invoke(app, ["tests", "run", "--project", str(tmp_path), "--", "pytest", "-q"], input="a\n")

    assert result.exit_code == 0, result.output
    assert ResettableDockerRunner.run_calls
    run_id = result.output.split("Created run ", 1)[1].splitlines()[0]
    events = SQLiteStore(tmp_path).list_events(run_id)
    assert any(event.event_type == "test.started" for event in events)
    assert any(event.event_type == "test.output" and event.payload["stream"] == "stdout" for event in events)
    assert any(event.event_type == "test.output" and event.payload["stream"] == "stderr" for event in events)
    assert any(event.event_type == "test.finished" and event.payload["status"] == "tests_passed" for event in events)


def _approval(project: Path) -> ApprovalProfile:
    return ApprovalProfile(
        id="appr_live_gold",
        backend="codex_cli",
        project_root=str(project),
        data_boundary="hosted_provider",
        task_types=["codex_code_edit"],
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        created_at=datetime.now(timezone.utc),
    )


def _init_clean_project(project: Path) -> None:
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)
    (project / ".gitignore").write_text(".harness/\n", encoding="utf-8")
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)


def _run_fake_edit(project: Path, *, decision: str) -> dict:
    store = SQLiteStore(project)
    store.initialize()
    return CodexCodeEditRunner(
        project,
        store,
        FakeEditBackend(default_config().backends["codex_cli"]),
        ApprovalStore(project),
        apply_back_approval_provider=StaticApplyBackApproval(decision),
    ).run("change value", "codex_code_edit", _approval(project))


class StaticApplyBackApproval:
    def __init__(self, decision: str) -> None:
        self.decision = decision

    def decide(self, diff_summary: str, full_diff: str, diff_artifact: Path) -> ApplyBackDecision:
        assert diff_artifact.exists()
        return ApplyBackDecision(decision=self.decision)


class FakeEditBackend(CodexCliBackend):
    def preflight(self):
        return BackendStatus(
            available=True,
            metadata=self.config.metadata,
            capabilities=BackendCapabilities(
                supports_exec=True,
                supports_cd=True,
                supports_read_only_sandbox=True,
                supports_workspace_write_sandbox=True,
                supports_ask_for_approval=True,
                supports_json_events=True,
                supports_output_last_message=True,
            ),
        )

    def run_edit(self, isolated_workspace, prompt, final_message_path):
        (Path(isolated_workspace) / "app.py").write_text("value = 2\n", encoding="utf-8")
        if final_message_path:
            final_message_path.write_text("changed app.py", encoding="utf-8")
        return (
            CodexRunResult(
                ["codex", "exec", "--cd", str(isolated_workspace), "--sandbox", "workspace-write"],
                "",
                "",
                0,
                [],
                "changed app.py",
            ),
            self.preflight().capabilities,
            NETWORK_NOT_ENFORCEABLE,
        )


class ResettableDockerRunner:
    run_calls: list[list[str]] = []
    preflight_calls = 0

    def __init__(self, config):
        if isinstance(config, DockerSandboxConfig):
            self.config = config
        elif hasattr(config, "model_dump"):
            self.config = DockerSandboxConfig.model_validate(config.model_dump())
        else:
            self.config = DockerSandboxConfig.model_validate(config)

    @classmethod
    def reset(cls) -> None:
        cls.run_calls = []
        cls.preflight_calls = 0

    def preflight(self):
        ResettableDockerRunner.preflight_calls += 1

    def create_workspace(self, project_root):
        from harness.sandbox import DockerSandboxRunner

        return DockerSandboxRunner(DockerSandboxConfig()).create_workspace(project_root)

    def run(self, workspace, command, timeout_seconds=None, workdir=None):
        ResettableDockerRunner.run_calls.append(list(command))
        return DockerRunResult(
            ok=True,
            exit_code=0,
            stdout="passed\n",
            stderr="warning\n",
            duration_seconds=0.1,
            timed_out=False,
            image=self.config.image,
            command=list(command),
            workdir=workdir or self.config.workdir,
        )
