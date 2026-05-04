from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from harness.approvals import ApprovalProfile, ApprovalStore
from harness.backends.codex_cli import (
    CodexCliBackend,
    CodexEditCommandUnavailable,
    CodexRunResult,
    NETWORK_NOT_ENFORCEABLE,
)
from harness.codex_edit_runner import ApplyBackDecision, CodexCodeEditRunner
from harness.codex_runner import HostedBoundaryApprovalRequired
from harness.config import default_config, write_default_config
from harness.memory.sqlite_store import SQLiteStore
from harness.models import BackendCapabilities, BackendStatus


EDIT_HELP = """
Usage: codex exec [OPTIONS] [PROMPT]
  --json
  --cd <DIR>
  --model <MODEL>
  --sandbox <SANDBOX_MODE> [possible values: read-only, workspace-write, danger-full-access]
  --ask-for-approval <POLICY> [possible values: on-request, on-failure, never]
  --output-last-message <FILE>
"""


def completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


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


def init_clean_project(project: Path) -> None:
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)
    (project / ".gitignore").write_text(".harness/\n", encoding="utf-8")
    (project / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)


class FakeEditBackend(CodexCliBackend):
    def __init__(self, config, edit_text="value = 2\n", secret=""):
        super().__init__(config)
        self.edit_text = edit_text
        self.secret = secret
        self.seen_project_root = None
        self.seen_prompt = None
        self.seen_final_message_path = None

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
        self.seen_project_root = Path(isolated_workspace)
        self.seen_prompt = prompt
        self.seen_final_message_path = final_message_path
        (Path(isolated_workspace) / "app.py").write_text(self.edit_text, encoding="utf-8")
        if final_message_path:
            final_message_path.write_text(f"final {self.secret}", encoding="utf-8")
        return (
            CodexRunResult(
                command=["codex", "exec", "--cd", str(isolated_workspace), "--sandbox", "workspace-write"],
                stdout=f'{{"event":"done","message":"{self.secret}"}}\n',
                stderr=self.secret,
                exit_status=0,
                json_events=[{"event": "done", "message": self.secret}],
                final_message=f"final {self.secret}",
            ),
            self.preflight().capabilities,
            NETWORK_NOT_ENFORCEABLE,
        )


class StaticApplyBackApproval:
    def __init__(self, decision: str):
        self.decision = decision

    def decide(self, diff_summary: str, full_diff: str, diff_artifact: Path) -> ApplyBackDecision:
        return ApplyBackDecision(decision=self.decision)


def test_missing_hosted_approval_profile_fails_closed(tmp_path) -> None:
    init_clean_project(tmp_path)
    write_default_config(tmp_path)
    store = SQLiteStore(tmp_path)
    store.initialize()
    runner = CodexCodeEditRunner(
        tmp_path,
        store,
        FakeEditBackend(default_config().backends["codex_cli"]),
        ApprovalStore(tmp_path),
    )

    with pytest.raises(HostedBoundaryApprovalRequired):
        runner.run("change value", "codex_code_edit", approval=None)


def test_codex_code_edit_runs_in_isolated_workspace_and_reports_diff(tmp_path) -> None:
    init_clean_project(tmp_path)
    before = (tmp_path / "app.py").read_bytes()
    store = SQLiteStore(tmp_path)
    store.initialize()
    backend = FakeEditBackend(default_config().backends["codex_cli"])

    result = CodexCodeEditRunner(tmp_path, store, backend, ApprovalStore(tmp_path)).run(
        "change value",
        "codex_code_edit",
        approval(tmp_path),
    )

    assert result["status"] == "completed_denied"
    assert result["changed_files"] == ["app.py"]
    assert backend.seen_project_root != tmp_path
    assert tmp_path not in backend.seen_project_root.parents
    assert (tmp_path / "app.py").read_bytes() == before
    assert not Path(result["isolated_workspace"]).exists()
    run_dir = tmp_path / ".harness" / "runs" / result["run_id"]
    assert "+value = 2" in (run_dir / "isolated_unified_diff.patch").read_text(encoding="utf-8")
    report = (run_dir / "final_report.md").read_text(encoding="utf-8")
    assert "Codex completed but changes were denied." in report


def test_keep_isolation_preserves_workspace(tmp_path) -> None:
    init_clean_project(tmp_path)
    write_default_config(tmp_path)
    store = SQLiteStore(tmp_path)
    store.initialize()
    backend = FakeEditBackend(default_config().backends["codex_cli"])

    result = CodexCodeEditRunner(tmp_path, store, backend, ApprovalStore(tmp_path)).run(
        "change value",
        "codex_code_edit",
        approval(tmp_path),
        keep_isolation=True,
    )

    assert result["isolation_cleanup_status"] == "kept"
    assert Path(result["isolated_workspace"]).exists()
    assert (Path(result["isolated_workspace"]) / "app.py").read_text(encoding="utf-8") == "value = 2\n"


def test_codex_outputs_are_sanitized(tmp_path) -> None:
    init_clean_project(tmp_path)
    store = SQLiteStore(tmp_path)
    store.initialize()
    secret = "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz"
    backend = FakeEditBackend(default_config().backends["codex_cli"], secret=secret)

    result = CodexCodeEditRunner(tmp_path, store, backend, ApprovalStore(tmp_path)).run(
        "change value",
        "codex_code_edit",
        approval(tmp_path),
    )

    run_dir = tmp_path / ".harness" / "runs" / result["run_id"]
    artifact_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [
            run_dir / "codex_stdout.txt",
            run_dir / "codex_stderr.txt",
            run_dir / "codex_events.jsonl",
            run_dir / "codex_final_message.md",
            run_dir / "final_report.md",
        ]
    )
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in artifact_text
    assert "[REDACTED_SECRET]" in artifact_text


def test_policy_violations_in_isolated_diff_are_reported_without_active_change(tmp_path) -> None:
    init_clean_project(tmp_path)
    before = (tmp_path / "app.py").read_bytes()

    class CreatingBackend(FakeEditBackend):
        def run_edit(self, isolated_workspace, prompt, final_message_path):
            (Path(isolated_workspace) / "new.py").write_text("new\n", encoding="utf-8")
            return (
                CodexRunResult(["codex", "exec", "--cd", str(isolated_workspace)], "", "", 0, [], ""),
                self.preflight().capabilities,
                NETWORK_NOT_ENFORCEABLE,
            )

    store = SQLiteStore(tmp_path)
    store.initialize()
    result = CodexCodeEditRunner(
        tmp_path,
        store,
        CreatingBackend(default_config().backends["codex_cli"]),
        ApprovalStore(tmp_path),
    ).run("create file", "codex_code_edit", approval(tmp_path))

    assert result["status"] == "policy_violation"
    assert result["policy_violations"][0]["kind"] == "creation"
    assert (tmp_path / "app.py").read_bytes() == before


def test_build_edit_command_detects_ask_for_approval_and_uses_isolated_cd(monkeypatch, tmp_path) -> None:
    def fake_run(args, **kwargs):
        if args == ["codex", "--help"]:
            return completed(args, stdout="Commands:\n  exec\n")
        if args == ["codex", "exec", "--help"]:
            return completed(args, stdout=EDIT_HELP)
        if args == ["codex", "login", "--help"]:
            return completed(args, stdout="status")
        return completed(args)

    monkeypatch.setattr(subprocess, "run", fake_run)
    backend = CodexCliBackend(default_config().backends["codex_cli"])
    command, capabilities, network_status = backend.build_edit_command(tmp_path, "change", tmp_path / "final.md")

    assert capabilities.supports_ask_for_approval
    assert ["--cd", str(tmp_path)] == command[command.index("--cd") : command.index("--cd") + 2]
    assert ["--sandbox", "workspace-write"] == command[command.index("--sandbox") : command.index("--sandbox") + 2]
    assert "--ask-for-approval" in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert "--yolo" not in command
    assert network_status == NETWORK_NOT_ENFORCEABLE


def test_build_edit_command_fails_closed_without_approval_gating(monkeypatch, tmp_path) -> None:
    def fake_run(args, **kwargs):
        if args == ["codex", "--help"]:
            return completed(args, stdout="Commands:\n  exec\n")
        if args == ["codex", "exec", "--help"]:
            return completed(args, stdout=EDIT_HELP.replace("--ask-for-approval", "--no-approval"))
        if args == ["codex", "login", "--help"]:
            return completed(args, stdout="status")
        return completed(args)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(CodexEditCommandUnavailable):
        CodexCliBackend(default_config().backends["codex_cli"]).build_edit_command(tmp_path, "change", None)


def test_run_edit_removes_openai_api_key_and_uses_isolated_cwd(monkeypatch, tmp_path) -> None:
    seen = {}

    def fake_run(args, **kwargs):
        if args == ["codex", "--help"]:
            return completed(args, stdout="Commands:\n  exec\n")
        if args == ["codex", "exec", "--help"]:
            return completed(args, stdout=EDIT_HELP)
        if args == ["codex", "login", "--help"]:
            return completed(args, stdout="status")
        if args[:2] == ["codex", "exec"]:
            seen["args"] = args
            seen["cwd"] = kwargs["cwd"]
            seen["env"] = kwargs["env"]
            final_path = Path(args[args.index("--output-last-message") + 1])
            final_path.write_text("done", encoding="utf-8")
            return completed(args, stdout='{"event":"done"}\n')
        return completed(args)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-propagate")
    monkeypatch.setattr(subprocess, "run", fake_run)
    result, _capabilities, _network_status = CodexCliBackend(default_config().backends["codex_cli"]).run_edit(
        tmp_path,
        "change",
        tmp_path / "final.md",
    )

    assert result.exit_status == 0
    assert seen["cwd"] == tmp_path
    assert "OPENAI_API_KEY" not in seen["env"]
    assert ["--cd", str(tmp_path)] == seen["args"][seen["args"].index("--cd") : seen["args"].index("--cd") + 2]


def test_dangerous_flags_from_config_are_rejected(monkeypatch, tmp_path) -> None:
    def fake_run(args, **kwargs):
        if args == ["codex", "--help"]:
            return completed(args, stdout="Commands:\n  exec\n")
        if args == ["codex", "exec", "--help"]:
            return completed(args, stdout=EDIT_HELP)
        if args == ["codex", "login", "--help"]:
            return completed(args, stdout="status")
        return completed(args)

    cfg = default_config().backends["codex_cli"].model_copy(deep=True)
    cfg.settings["ask_for_approval"] = "--yolo"
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ValueError):
        CodexCliBackend(cfg).build_edit_command(tmp_path, "change", None)


def test_codex_code_edit_does_not_use_local_or_paid_backend(monkeypatch, tmp_path) -> None:
    from typer.testing import CliRunner

    from harness.cli.main import app

    init_clean_project(tmp_path)
    write_default_config(tmp_path)
    store = SQLiteStore(tmp_path)
    store.initialize()
    ApprovalStore(tmp_path).save_all([approval(tmp_path)])
    calls = {"local": 0, "codex": 0}

    class ForbiddenLocal:
        def __init__(self, config):
            calls["local"] += 1
            raise AssertionError("local fallback must not be used")

    class CliFakeBackend(FakeEditBackend):
        def __init__(self, config):
            calls["codex"] += 1
            super().__init__(config)

    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", ForbiddenLocal)
    monkeypatch.setattr("harness.cli.main.CodexCliBackend", CliFakeBackend)
    result = CliRunner().invoke(
        app,
        ["run", "change value", "--project", str(tmp_path), "--task-type", "codex_code_edit"],
        input="d\n",
    )

    assert result.exit_code == 0
    assert calls == {"local": 0, "codex": 1}
    assert "Apply-back decision: denied" in result.output


def test_existing_repo_planning_route_still_uses_read_only(monkeypatch, tmp_path) -> None:
    from typer.testing import CliRunner

    from harness.cli.main import app

    init_clean_project(tmp_path)
    write_default_config(tmp_path)
    store = SQLiteStore(tmp_path)
    store.initialize()
    ApprovalStore(tmp_path).save_all([approval(tmp_path, task_type="repo_planning")])
    seen = {}

    class PlanningBackend(FakeEditBackend):
        def preflight(self):
            status = super().preflight()
            status.capabilities.supports_read_only_sandbox = True
            return status

        def run_read_only(self, project_root, prompt, final_message_path):
            seen["project_root"] = project_root
            return CodexRunResult(["codex", "exec", "--sandbox", "read-only"], "", "", 0, [], "plan")

        def run_edit(self, isolated_workspace, prompt, final_message_path):  # pragma: no cover - assertion path
            raise AssertionError("repo_planning must not use edit execution")

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", PlanningBackend)
    result = CliRunner().invoke(app, ["run", "plan", "--project", str(tmp_path), "--task-type", "repo_planning"])

    assert result.exit_code == 0
    assert seen["project_root"] == tmp_path


def test_environment_does_not_need_openai_api_key_for_code_edit(tmp_path, monkeypatch) -> None:
    init_clean_project(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store = SQLiteStore(tmp_path)
    store.initialize()
    result = CodexCodeEditRunner(
        tmp_path,
        store,
        FakeEditBackend(default_config().backends["codex_cli"]),
        ApprovalStore(tmp_path),
    ).run("change value", "codex_code_edit", approval(tmp_path))
    assert result["status"] == "completed_denied"
