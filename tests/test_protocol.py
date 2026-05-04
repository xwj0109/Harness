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
