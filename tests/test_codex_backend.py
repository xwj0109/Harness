from __future__ import annotations

import builtins
import subprocess
from pathlib import Path

import pytest

from harness.backends.codex_cli import AUTH_ERROR, CodexDangerousFlagError, CodexCliBackend, CodexSandboxUnavailable
from harness.config import default_config


EXEC_HELP = """
Usage: codex exec [OPTIONS] [PROMPT]
  --json
  --cd <DIR>
  --model <MODEL>
  --sandbox <SANDBOX_MODE> [possible values: read-only, workspace-write, danger-full-access]
  --output-last-message <FILE>
  --output-schema <FILE>
"""


def completed(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


def codex_config():
    return default_config().backends["codex_cli"]


def test_codex_capability_detection_from_help(monkeypatch) -> None:
    def fake_run(args, **kwargs):
        if args == ["codex", "--help"]:
            return completed(args, stdout="Commands:\n  exec\n  login\n")
        if args == ["codex", "exec", "--help"]:
            return completed(args, stdout=EXEC_HELP)
        if args == ["codex", "login", "--help"]:
            return completed(args, stdout="Commands:\n  status\n")
        return completed(args, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    capabilities = CodexCliBackend(codex_config()).detect_capabilities()
    assert capabilities.supports_exec
    assert capabilities.supports_json_events
    assert capabilities.supports_cd
    assert capabilities.supports_model_arg
    assert capabilities.supports_read_only_sandbox
    assert capabilities.supports_output_last_message
    assert capabilities.supports_output_schema
    assert capabilities.supports_login_status


def test_codex_command_construction_uses_detected_capabilities(monkeypatch, tmp_path) -> None:
    def fake_run(args, **kwargs):
        if args == ["codex", "--help"]:
            return completed(args, stdout="Commands:\n  exec\n")
        if args == ["codex", "exec", "--help"]:
            return completed(args, stdout=EXEC_HELP)
        if args == ["codex", "login", "--help"]:
            return completed(args, stdout="status")
        return completed(args, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    command = CodexCliBackend(codex_config()).build_read_only_command(
        tmp_path,
        "plan",
        tmp_path / "final.md",
    )
    assert command[:2] == ["codex", "exec"]
    assert "--json" in command
    assert ["--cd", str(tmp_path)] == command[command.index("--cd") : command.index("--cd") + 2]
    assert ["--model", "gpt-5.5"] == command[command.index("--model") : command.index("--model") + 2]
    assert ["-c", 'model_reasoning_effort="low"'] in [
        command[index : index + 2] for index, value in enumerate(command[:-1]) if value == "-c"
    ]
    assert ["--sandbox", "read-only"] == command[command.index("--sandbox") : command.index("--sandbox") + 2]
    assert "--output-last-message" in command
    assert "plan" == command[-1]


def test_codex_edit_command_uses_model_and_low_reasoning(monkeypatch, tmp_path) -> None:
    exec_help = EXEC_HELP + "\n  --ask-for-approval <APPROVAL_POLICY>\n"

    def fake_run(args, **kwargs):
        if args == ["codex", "--help"]:
            return completed(args, stdout="Commands:\n  exec\n")
        if args == ["codex", "exec", "--help"]:
            return completed(args, stdout=exec_help)
        if args == ["codex", "login", "--help"]:
            return completed(args, stdout="status")
        return completed(args, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    command, _capabilities, _network_status = CodexCliBackend(codex_config()).build_edit_command(
        tmp_path,
        "edit safely",
        tmp_path / "final.md",
    )

    assert ["--cd", str(tmp_path)] == command[command.index("--cd") : command.index("--cd") + 2]
    assert ["--model", "gpt-5.5"] == command[command.index("--model") : command.index("--model") + 2]
    assert ["-c", 'model_reasoning_effort="low"'] in [
        command[index : index + 2] for index, value in enumerate(command[:-1]) if value == "-c"
    ]
    assert ["--sandbox", "workspace-write"] == command[command.index("--sandbox") : command.index("--sandbox") + 2]


def test_codex_rejects_invalid_reasoning_effort_before_execution(monkeypatch, tmp_path) -> None:
    def fake_run(args, **kwargs):
        if args == ["codex", "--help"]:
            return completed(args, stdout="Commands:\n  exec\n")
        if args == ["codex", "exec", "--help"]:
            return completed(args, stdout=EXEC_HELP)
        if args == ["codex", "login", "--help"]:
            return completed(args, stdout="status")
        return completed(args, stdout="")

    cfg = codex_config().model_copy(deep=True)
    cfg.settings["model_reasoning_effort"] = "danger-full-access"
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(CodexDangerousFlagError, match="Unsupported Codex reasoning effort"):
        CodexCliBackend(cfg).build_read_only_command(tmp_path, "plan", None)


def test_codex_unauthenticated_failure(monkeypatch) -> None:
    def fake_run(args, **kwargs):
        if args == ["codex", "--help"]:
            return completed(args, stdout="Commands:\n  exec\n")
        if args == ["codex", "exec", "--help"]:
            return completed(args, stdout=EXEC_HELP)
        if args == ["codex", "login", "--help"]:
            return completed(args, stdout="status")
        if args == ["codex", "login", "status"]:
            return completed(args, returncode=1, stderr="not logged in")
        return completed(args)

    monkeypatch.setattr(subprocess, "run", fake_run)
    status = CodexCliBackend(codex_config()).preflight()
    assert not status.available
    assert status.reason == AUTH_ERROR


def test_codex_does_not_read_auth_file(monkeypatch) -> None:
    def fake_run(args, **kwargs):
        if args == ["codex", "--help"]:
            return completed(args, stdout="Commands:\n  exec\n")
        if args == ["codex", "exec", "--help"]:
            return completed(args, stdout=EXEC_HELP)
        if args == ["codex", "login", "--help"]:
            return completed(args, stdout="status")
        if args == ["codex", "login", "status"]:
            return completed(args, stdout="logged in")
        return completed(args)

    original_open = builtins.open

    def guarded_open(file, *args, **kwargs):
        if "auth.json" in str(file):
            raise AssertionError("Codex auth file must not be read")
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(builtins, "open", guarded_open)
    assert CodexCliBackend(codex_config()).preflight().available


def test_codex_read_only_fails_closed_without_sandbox(monkeypatch, tmp_path) -> None:
    def fake_run(args, **kwargs):
        if args == ["codex", "--help"]:
            return completed(args, stdout="Commands:\n  exec\n")
        if args == ["codex", "exec", "--help"]:
            return completed(args, stdout="--json --cd --model")
        if args == ["codex", "login", "--help"]:
            return completed(args, stdout="status")
        return completed(args)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(CodexSandboxUnavailable):
        CodexCliBackend(codex_config()).build_read_only_command(tmp_path, "plan", None)


def test_codex_subprocesses_remove_openai_api_key(monkeypatch) -> None:
    seen_envs = []

    def fake_run(args, **kwargs):
        seen_envs.append(kwargs.get("env", {}))
        if args == ["codex", "--help"]:
            return completed(args, stdout="Commands:\n  exec\n")
        if args == ["codex", "exec", "--help"]:
            return completed(args, stdout=EXEC_HELP)
        if args == ["codex", "login", "--help"]:
            return completed(args, stdout="status")
        if args == ["codex", "login", "status"]:
            return completed(args, stdout="logged in")
        return completed(args)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-propagate")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert CodexCliBackend(codex_config()).preflight().available
    assert seen_envs
    assert all("OPENAI_API_KEY" not in env for env in seen_envs)


def test_codex_final_message_artifact_is_sanitized(monkeypatch, tmp_path) -> None:
    def fake_run(args, **kwargs):
        if args == ["codex", "--help"]:
            return completed(args, stdout="Commands:\n  exec\n")
        if args == ["codex", "exec", "--help"]:
            return completed(args, stdout=EXEC_HELP)
        if args == ["codex", "login", "--help"]:
            return completed(args, stdout="status")
        if args[:2] == ["codex", "exec"]:
            final_path = Path(args[args.index("--output-last-message") + 1])
            final_path.write_text("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz", encoding="utf-8")
            return completed(args, stdout="", stderr="")
        return completed(args)

    monkeypatch.setattr(subprocess, "run", fake_run)
    final_path = tmp_path / "final.md"
    result = CodexCliBackend(codex_config()).run_read_only(tmp_path, "plan", final_path)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in result.final_message
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in final_path.read_text(encoding="utf-8")
    assert "[REDACTED_SECRET]" in final_path.read_text(encoding="utf-8")


def test_codex_command_never_uses_dangerous_bypass_flags(monkeypatch, tmp_path) -> None:
    def fake_run(args, **kwargs):
        if args == ["codex", "--help"]:
            return completed(args, stdout="Commands:\n  exec\n")
        if args == ["codex", "exec", "--help"]:
            return completed(args, stdout=EXEC_HELP)
        if args == ["codex", "login", "--help"]:
            return completed(args, stdout="status")
        return completed(args)

    monkeypatch.setattr(subprocess, "run", fake_run)
    command = CodexCliBackend(codex_config()).build_read_only_command(tmp_path, "plan", None)
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert "--full-auto" not in command
    assert command[command.index("--sandbox") + 1] == "read-only"
