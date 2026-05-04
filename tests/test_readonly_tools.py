import subprocess

from harness.config import DEFAULT_CONTEXT_EXCLUDES
from harness.tools.base import ToolContext
from harness.tools.readonly import GitDiffTool, GitStatusTool, ListFilesTool, ReadFileTool, MAX_READ_BYTES


def test_list_files_excludes_defaults_and_blocks_secrets(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')", encoding="utf-8")
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness" / "x").write_text("x", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("x", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=value", encoding="utf-8")
    context = ToolContext(project_root=tmp_path, context_excludes=[".harness/", "node_modules/"])
    result = ListFilesTool().run({"path": "."}, context)
    assert result.ok
    assert result.data["files"] == ["src/app.py"]
    assert ".env" in result.data["blocked_secret_paths"]


def test_list_files_excludes_all_default_context_paths(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')", encoding="utf-8")
    (tmp_path / ".DS_Store").write_text("ignored", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "__pycache__").mkdir()
    (tmp_path / "nested" / "__pycache__" / "ignored.pyc").write_text("ignored", encoding="utf-8")
    (tmp_path / "agent_harness.egg-info").mkdir()
    (tmp_path / "agent_harness.egg-info" / "PKG-INFO").write_text("ignored", encoding="utf-8")
    for dirname in [
        ".harness",
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        "dist",
        "build",
    ]:
        path = tmp_path / dirname
        path.mkdir()
        (path / "ignored.txt").write_text("ignored", encoding="utf-8")
    context = ToolContext(project_root=tmp_path, context_excludes=DEFAULT_CONTEXT_EXCLUDES)
    result = ListFilesTool().run({"path": "."}, context)
    assert result.ok
    assert result.data["files"] == ["src/app.py"]


def test_read_file_reads_text_and_blocks_secret_and_traversal(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=value", encoding="utf-8")
    context = ToolContext(project_root=tmp_path)
    assert ReadFileTool().run({"path": "a.txt"}, context).output == "hello"
    assert ReadFileTool().run({"path": ".env"}, context).error_type == "secret_path"
    assert ReadFileTool().run({"path": "../x"}, context).error_type == "path_security"


def test_read_file_rejects_binary_and_too_large_files(tmp_path) -> None:
    (tmp_path / "binary.bin").write_bytes(b"abc\x00def")
    (tmp_path / "large.txt").write_bytes(b"x" * (MAX_READ_BYTES + 1))
    context = ToolContext(project_root=tmp_path)
    assert ReadFileTool().run({"path": "binary.bin"}, context).error_type == "binary"
    assert ReadFileTool().run({"path": "large.txt"}, context).error_type == "too_large"


def test_git_tools_work_in_git_repo(tmp_path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    context = ToolContext(project_root=tmp_path)
    status = GitStatusTool().run({}, context)
    diff = GitDiffTool().run({}, context)
    assert status.ok
    assert diff.ok


def test_git_tools_fail_gracefully_outside_repo(tmp_path) -> None:
    context = ToolContext(project_root=tmp_path)
    assert not GitStatusTool().run({}, context).ok
    assert not GitDiffTool().run({}, context).ok
