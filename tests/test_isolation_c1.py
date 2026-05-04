from __future__ import annotations

import subprocess

import pytest

from harness.isolation import (
    ActiveRepoDirtyError,
    IsolationManager,
    create_baseline_manifest,
    inspect_isolated_diff,
)


def run_git(cwd, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=True)


def init_repo(path) -> None:
    run_git(path, "init")
    run_git(path, "config", "user.email", "test@example.com")
    run_git(path, "config", "user.name", "Test User")


def test_git_worktree_is_preferred_and_lives_outside_active_project(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    init_repo(project)
    (project / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    run_git(project, "add", ".")
    run_git(project, "commit", "-m", "initial")

    workspace = IsolationManager().create(project)
    try:
        assert workspace.strategy == "git_worktree"
        assert project not in workspace.path.parents
        assert workspace.path != project
        assert (workspace.path / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    finally:
        workspace.cleanup()


def test_git_repo_without_valid_head_uses_copy_fallback(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    init_repo(project)
    (project / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")

    workspace = IsolationManager().create(project, allow_dirty=True)
    try:
        assert workspace.strategy == "isolated_copy"
        assert (workspace.path / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    finally:
        workspace.cleanup()


def test_worktree_creation_failure_uses_copy_fallback(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    init_repo(project)
    (project / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    run_git(project, "add", ".")
    run_git(project, "commit", "-m", "initial")

    monkeypatch.setattr(IsolationManager, "_try_git_worktree", lambda self, active, destination: False)
    workspace = IsolationManager().create(project)
    try:
        assert workspace.strategy == "isolated_copy"
        assert (workspace.path / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    finally:
        workspace.cleanup()


def test_copy_fallback_excludes_blocked_paths_and_records_manifest(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    (project / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (project / "secrets").mkdir()
    (project / "secrets" / "token.txt").write_text("secret\n", encoding="utf-8")
    (project / "node_modules").mkdir()
    (project / "node_modules" / "pkg.js").write_text("pkg\n", encoding="utf-8")

    workspace = IsolationManager().create(project)
    try:
        assert workspace.strategy == "isolated_copy"
        assert "app.py" in workspace.baseline_manifest.entries
        assert ".env" not in workspace.baseline_manifest.entries
        assert "secrets/token.txt" not in workspace.baseline_manifest.entries
        assert "node_modules/pkg.js" not in workspace.baseline_manifest.entries
        assert not (workspace.path / ".env").exists()
        assert not (workspace.path / "secrets").exists()
        assert not (workspace.path / "node_modules").exists()
    finally:
        workspace.cleanup()


def test_diff_inspection_allows_existing_text_file_modification(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    baseline = create_baseline_manifest(project)
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    (isolated / "app.py").write_text("value = 2\n", encoding="utf-8")

    result = inspect_isolated_diff(isolated, baseline)

    assert result.valid
    assert result.changed_files == ["app.py"]
    assert result.allowed_changed_files == ["app.py"]
    assert "--- a/app.py" in result.unified_diff
    assert "+value = 2" in result.unified_diff
    assert "1 file changed" in result.diff_stat


def test_diff_inspection_ignores_generated_artifacts_without_blocking_valid_source_change(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "scratch_codex_edit.py").write_text("value = 1\n", encoding="utf-8")
    (project / "agent_harness.egg-info").mkdir()
    (project / "agent_harness.egg-info" / "PKG-INFO").write_text("old\n", encoding="utf-8")
    (project / "harness").mkdir()
    (project / "harness" / ".DS_Store").write_text("old\n", encoding="utf-8")
    baseline = create_baseline_manifest(project)
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    (isolated / "scratch_codex_edit.py").write_text("value = 2\n", encoding="utf-8")
    (isolated / "agent_harness.egg-info").mkdir()
    (isolated / "agent_harness.egg-info" / "PKG-INFO").write_text("new\n", encoding="utf-8")
    (isolated / "harness").mkdir()
    (isolated / "harness" / ".DS_Store").write_text("new\n", encoding="utf-8")
    (isolated / "__pycache__").mkdir()
    (isolated / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01")

    result = inspect_isolated_diff(isolated, baseline)

    assert result.valid
    assert result.changed_files == ["scratch_codex_edit.py"]
    assert result.allowed_changed_files == ["scratch_codex_edit.py"]
    assert sorted(result.ignored_generated_artifacts) == [
        "__pycache__/x.pyc",
        "agent_harness.egg-info/PKG-INFO",
        "harness/.DS_Store",
    ]
    assert "scratch_codex_edit.py" in result.unified_diff
    assert "agent_harness.egg-info" not in result.unified_diff


def test_diff_inspection_generated_only_changes_are_not_policy_violations(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    baseline = create_baseline_manifest(project)
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    (isolated / "app.py").write_text("value = 1\n", encoding="utf-8")
    (isolated / ".DS_Store").write_text("local\n", encoding="utf-8")
    (isolated / "agent_harness.egg-info").mkdir()
    (isolated / "agent_harness.egg-info" / "SOURCES.txt").write_text("local\n", encoding="utf-8")

    result = inspect_isolated_diff(isolated, baseline)

    assert result.valid
    assert result.changed_files == []
    assert result.allowed_changed_files == []
    assert result.violations == []
    assert sorted(result.ignored_generated_artifacts) == [".DS_Store", "agent_harness.egg-info/SOURCES.txt"]
    assert result.unified_diff == ""


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("new.py", "creation"),
        (".env", "blocked_path"),
        ("secrets/token.txt", "blocked_path"),
        (".harness/config.yaml", "blocked_path"),
    ],
)
def test_diff_inspection_rejects_creation_and_blocked_paths(tmp_path, path, kind) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    baseline = create_baseline_manifest(project)
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    (isolated / "app.py").write_text("value = 1\n", encoding="utf-8")
    target = isolated / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x\n", encoding="utf-8")

    result = inspect_isolated_diff(isolated, baseline)

    assert not result.valid
    assert any(violation.path == path and violation.kind == kind for violation in result.violations)


def test_diff_inspection_rejects_deletion_binary_and_symlink_changes(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "deleted.py").write_text("value = 1\n", encoding="utf-8")
    (project / "binary.py").write_text("value = 1\n", encoding="utf-8")
    (project / "link.py").write_text("value = 1\n", encoding="utf-8")
    baseline = create_baseline_manifest(project)
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    (isolated / "binary.py").write_bytes(b"\x00\x01\x02")
    (isolated / "target.py").write_text("target\n", encoding="utf-8")
    (isolated / "link.py").symlink_to("target.py")

    result = inspect_isolated_diff(isolated, baseline)

    violations = {(violation.path, violation.kind) for violation in result.violations}
    assert ("deleted.py", "deletion") in violations
    assert ("binary.py", "binary") in violations
    assert ("link.py", "symlink") in violations


def test_dirty_repo_refuses_by_default_and_continues_with_explicit_approval(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    init_repo(project)
    (project / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    run_git(project, "add", ".")
    run_git(project, "commit", "-m", "initial")
    (project / "app.py").write_text("value = 2\n", encoding="utf-8")

    with pytest.raises(ActiveRepoDirtyError):
        IsolationManager().create(project)

    workspace = IsolationManager().create(project, allow_dirty=True)
    try:
        assert "may not include uncommitted changes" in " ".join(workspace.warnings)
    finally:
        workspace.cleanup()


def test_missing_agents_md_warns_but_is_not_created(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")

    workspace = IsolationManager().create(project)
    try:
        assert not workspace.agents_md_exists
        assert any("AGENTS.md is missing" in warning for warning in workspace.warnings)
        assert not (project / "AGENTS.md").exists()
    finally:
        workspace.cleanup()


def test_isolated_changes_leave_active_project_byte_for_byte_unchanged_before_apply_back(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    init_repo(project)
    (project / "AGENTS.md").write_text("instructions\n", encoding="utf-8")
    active_file = project / "app.py"
    active_file.write_bytes(b"value = 1\n")
    run_git(project, "add", ".")
    run_git(project, "commit", "-m", "initial")
    before = active_file.read_bytes()

    workspace = IsolationManager().create(project)
    try:
        (workspace.path / "app.py").write_bytes(b"value = 2\n")
        result = inspect_isolated_diff(workspace.path, workspace.baseline_manifest)

        assert result.valid
        assert result.changed_files == ["app.py"]
        assert active_file.read_bytes() == before
        assert run_git(project, "status", "--porcelain").stdout == ""
    finally:
        workspace.cleanup()
