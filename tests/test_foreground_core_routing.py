from __future__ import annotations

import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from harness.backends.codex_cli import CodexRunResult
from harness.cli.main import app
from harness.core_service import CoreRunExecutionResult
from harness.models import BackendStatus


runner = CliRunner()


def _assert_core_shape(payload: dict) -> None:
    assert payload["schema_version"] == "harness.core_run/v1"
    for key in (
        "ok",
        "mode",
        "decision",
        "task_id",
        "lease_id",
        "run_id",
        "adapter_id",
        "manifest",
        "errors",
        "next_commands",
    ):
        assert key in payload


def test_foreground_explicit_plan_json_uses_core_service(tmp_path, monkeypatch) -> None:
    calls: list[dict] = []

    class FakeCoreService:
        def start_goal(self, goal, mode, project_root, output_format="json"):
            calls.append(
                {
                    "goal": goal,
                    "mode": mode,
                    "project_root": Path(project_root),
                    "output_format": output_format,
                }
            )
            return CoreRunExecutionResult(
                ok=True,
                mode=mode,
                decision="fake_core_route",
                project_root=Path(project_root),
                task_id="task_fake",
                lease_id="lease_fake",
                run_id=None,
                adapter_id="repo_planning",
                manifest=None,
                errors=[],
                next_commands=[],
            )

    monkeypatch.setattr("harness.cli.main.HarnessCoreService", FakeCoreService)

    result = runner.invoke(
        app,
        ["plan this change", "--agent", "plan", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "goal": "plan this change",
            "mode": "repo_planning",
            "project_root": tmp_path.resolve(),
            "output_format": "json",
        }
    ]
    payload = json.loads(result.output)
    _assert_core_shape(payload)
    assert payload["decision"] == "fake_core_route"
    assert payload["adapter_id"] == "repo_planning"


def test_foreground_plan_without_hosted_approval_blocks_with_core_shape(tmp_path) -> None:
    result = runner.invoke(
        app,
        ["plan this change", "--agent", "plan", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    _assert_core_shape(payload)
    assert payload["ok"] is False
    assert payload["mode"] == "repo_planning"
    assert payload["decision"] == "execution_adapter_rejected"
    assert payload["task_id"]
    assert payload["lease_id"]
    assert payload["run_id"] is None
    assert payload["adapter_id"] == "repo_planning"
    assert payload["manifest"] is None
    assert any("hosted_provider_codex" in error for error in payload["errors"])


def test_foreground_explicit_build_json_uses_core_service(tmp_path, monkeypatch) -> None:
    calls: list[dict] = []

    class FakeCoreService:
        def start_goal(self, goal, mode, project_root, output_format="json"):
            calls.append(
                {
                    "goal": goal,
                    "mode": mode,
                    "project_root": Path(project_root),
                    "output_format": output_format,
                }
            )
            return CoreRunExecutionResult(
                ok=True,
                mode=mode,
                decision="fake_core_route",
                project_root=Path(project_root),
                task_id="task_fake",
                lease_id="lease_fake",
                run_id=None,
                adapter_id="codex_isolated_edit",
                manifest=None,
                errors=[],
                next_commands=[],
            )

    monkeypatch.setattr("harness.cli.main.HarnessCoreService", FakeCoreService)

    result = runner.invoke(
        app,
        ["make this change", "--agent", "build", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "goal": "make this change",
            "mode": "codex_isolated_edit",
            "project_root": tmp_path.resolve(),
            "output_format": "json",
        }
    ]
    payload = json.loads(result.output)
    _assert_core_shape(payload)
    assert payload["decision"] == "fake_core_route"
    assert payload["adapter_id"] == "codex_isolated_edit"


def test_foreground_build_without_hosted_approval_blocks_with_core_shape(tmp_path) -> None:
    result = runner.invoke(
        app,
        ["make this change", "--agent", "build", "--project", str(tmp_path), "--output", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    _assert_core_shape(payload)
    assert payload["ok"] is False
    assert payload["mode"] == "codex_isolated_edit"
    assert payload["decision"] == "execution_adapter_rejected"
    assert payload["task_id"]
    assert payload["lease_id"]
    assert payload["run_id"] is None
    assert payload["adapter_id"] == "codex_isolated_edit"
    assert payload["manifest"] is None
    assert any("hosted_provider_codex" in error for error in payload["errors"])


def test_foreground_direct_mode_does_not_route_through_core(tmp_path, monkeypatch) -> None:
    _initialize_direct_prompt_project(tmp_path)
    _install_exploding_core_service(monkeypatch)
    _install_fake_codex_backend(monkeypatch)

    result = runner.invoke(
        app,
        [
            "make this change",
            "--agent",
            "build",
            "--mode",
            "direct",
            "--project",
            str(tmp_path),
            "--output",
            "json",
            "--no-stream",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.codex_direct_agent/v1"
    assert payload["status"] == "completed"


def test_foreground_default_prompt_does_not_route_through_core(tmp_path, monkeypatch) -> None:
    _initialize_direct_prompt_project(tmp_path)
    _install_exploding_core_service(monkeypatch)
    _install_fake_codex_backend(monkeypatch)

    result = runner.invoke(
        app,
        ["make this change", "--project", str(tmp_path), "--output", "json", "--no-stream"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "harness.codex_direct_agent/v1"
    assert payload["status"] == "completed"


def _initialize_direct_prompt_project(project_root: Path) -> None:
    assert runner.invoke(app, ["init", "--project", str(project_root)]).exit_code == 0
    subprocess.run(["git", "init"], cwd=project_root, check=True, capture_output=True)
    (project_root / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=project_root, check=True, capture_output=True)


def _install_exploding_core_service(monkeypatch) -> None:
    class ExplodingCoreService:
        def start_goal(self, *args, **kwargs):
            raise AssertionError("this foreground path must not route through HarnessCoreService")

    monkeypatch.setattr("harness.cli.main.HarnessCoreService", ExplodingCoreService)


def _install_fake_codex_backend(monkeypatch) -> None:
    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config
            self.name = config.name

        def preflight(self):
            return BackendStatus(available=True, metadata=self.config.metadata, capabilities=self.config.capabilities)

        def run_direct_agent(self, project_root, prompt, final_message_path, *, model=None, reasoning_effort=None):
            final_message_path.write_text("Direct foreground path completed.", encoding="utf-8")
            self.config.settings["last_codex_approval_mode"] = "on-request via --ask-for-approval"
            self.config.settings["last_codex_sandbox_mode"] = "workspace-write"
            return (
                CodexRunResult(["codex", "exec", prompt], "", "", 0, [], "Direct foreground path completed."),
                self.config.capabilities,
                "",
            )

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
