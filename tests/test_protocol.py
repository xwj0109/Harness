import json

import pytest

from harness.protocol import CommandValidationError, parse_model_command


def test_parse_valid_commands() -> None:
    command = parse_model_command('{"command":"list_files","arguments":{"path":"."}}')
    assert command.command == "list_files"
    assert command.arguments == {"path": "."}


def test_parse_rejects_malformed_json() -> None:
    with pytest.raises(CommandValidationError):
        parse_model_command("{not json")


def test_parse_accepts_markdown_fenced_json() -> None:
    command = parse_model_command(
        """```json
{"command":"list_files","arguments":{"path":"."}}
```"""
    )
    assert command.command == "list_files"
    assert command.arguments == {"path": "."}


def test_parse_rejects_unknown_tool() -> None:
    with pytest.raises(CommandValidationError):
        parse_model_command('{"command":"run_shell","arguments":{"command":"pwd"}}')


def test_parse_does_not_normalize_unknown_command() -> None:
    with pytest.raises(CommandValidationError):
        parse_model_command('{"command":"run_shell","arguments":""}')


def test_parse_rejects_out_of_policy_arguments() -> None:
    with pytest.raises(CommandValidationError):
        parse_model_command('{"command":"git_status","arguments":{"path":"."}}')


def test_parse_final_answer() -> None:
    command = parse_model_command('{"command":"final_answer","arguments":{"answer":"done"}}')
    assert command.final_text == "done"


def test_parse_normalizes_list_files_string_shorthand() -> None:
    command = parse_model_command('{"command":"list_files","arguments":"."}')
    assert command.command == "list_files"
    assert command.arguments == {"path": "."}


def test_parse_normalizes_list_files_empty_string_shorthand() -> None:
    command = parse_model_command('{"command":"list_files","arguments":""}')
    assert command.command == "list_files"
    assert command.arguments == {"path": "."}


def test_parse_normalizes_read_file_string_shorthand() -> None:
    command = parse_model_command('{"command":"read_file","arguments":"some/path.py"}')
    assert command.command == "read_file"
    assert command.arguments == {"path": "some/path.py"}


def test_parse_normalizes_read_file_file_path_alias() -> None:
    command = parse_model_command('{"command":"read_file","arguments":{"file_path":"README.md"}}')
    assert command.command == "read_file"
    assert command.arguments == {"path": "README.md"}


def test_parse_normalizes_git_status_empty_string_shorthand() -> None:
    command = parse_model_command('{"command":"git_status","arguments":""}')
    assert command.command == "git_status"
    assert command.arguments == {}


def test_parse_normalizes_git_diff_empty_string_shorthand() -> None:
    command = parse_model_command('{"command":"git_diff","arguments":""}')
    assert command.command == "git_diff"
    assert command.arguments == {}


def test_parse_normalizes_final_answer_string_shorthand() -> None:
    command = parse_model_command('{"command":"final_answer","arguments":"done"}')
    assert command.command == "final_answer"
    assert command.arguments == {"answer": "done"}
    assert command.final_text == "done"


def test_apply_patch_rejected_by_default_for_read_only_tasks() -> None:
    with pytest.raises(CommandValidationError):
        parse_model_command('{"command":"apply_patch","arguments":{"patch":"--- a/x\\n+++ b/x\\n"}}')


def test_apply_patch_string_shorthand_is_rejected() -> None:
    with pytest.raises(CommandValidationError):
        parse_model_command(
            '{"command":"apply_patch","arguments":"--- a/x\\n+++ b/x\\n"}',
            allow_apply_patch=True,
        )


def test_apply_patch_allowed_when_enabled_for_edit_tasks() -> None:
    command = parse_model_command(
        '{"command":"apply_patch","arguments":{"patch":"--- a/x\\n+++ b/x\\n"}}',
        allow_apply_patch=True,
    )
    assert command.command == "apply_patch"


def test_run_tests_parses_when_enabled() -> None:
    command = parse_model_command(
        '{"command":"run_tests","arguments":{"command":["python","-m","pytest","-q"]}}',
        allow_run_tests=True,
    )
    assert command.command == "run_tests"
    assert command.arguments == {"command": ["python", "-m", "pytest", "-q"]}


def test_run_tests_rejected_by_default() -> None:
    with pytest.raises(CommandValidationError):
        parse_model_command('{"command":"run_tests","arguments":{"command":["pytest","-q"]}}')


def test_allow_run_tests_does_not_allow_apply_patch() -> None:
    with pytest.raises(CommandValidationError):
        parse_model_command(
            '{"command":"apply_patch","arguments":{"patch":"--- a/x\\n+++ b/x\\n"}}',
            allow_run_tests=True,
        )


def test_allow_apply_patch_does_not_allow_run_tests() -> None:
    with pytest.raises(CommandValidationError):
        parse_model_command(
            '{"command":"run_tests","arguments":{"command":["pytest","-q"]}}',
            allow_apply_patch=True,
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {"command": []},
        {"command": "python -m pytest -q"},
        {"command": ["pytest", "&&", "echo"]},
        {"command": ["pytest", "-q;"]},
    ],
)
def test_run_tests_rejects_invalid_commands(arguments) -> None:
    with pytest.raises(CommandValidationError):
        parse_model_command(
            json.dumps({"command": "run_tests", "arguments": arguments}),
            allow_run_tests=True,
        )


def test_run_tests_accepts_valid_cwd_inside_project(tmp_path) -> None:
    (tmp_path / "tests").mkdir()
    command = parse_model_command(
        '{"command":"run_tests","arguments":{"command":["pytest","-q"],"cwd":"tests"}}',
        allow_run_tests=True,
        project_root=tmp_path,
    )
    assert command.arguments["cwd"] == "tests"


@pytest.mark.parametrize("cwd", ["../outside", "/tmp", "missing", "file.txt"])
def test_run_tests_rejects_invalid_cwd(tmp_path, cwd) -> None:
    (tmp_path / "file.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(CommandValidationError):
        parse_model_command(
            json.dumps({"command": "run_tests", "arguments": {"command": ["pytest", "-q"], "cwd": cwd}}),
            allow_run_tests=True,
            project_root=tmp_path,
        )
