import subprocess

import pytest

from harness.tools.base import ToolContext
from harness.tools.patch import ApplyPatchTool, PatchValidationError


def safe_patch() -> str:
    return """--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 print("old")
-value = 1
+value = 2
"""


def test_patch_validation_accepts_safe_unified_diff(tmp_path) -> None:
    (tmp_path / "app.py").write_text('print("old")\nvalue = 1\n', encoding="utf-8")
    summary = ApplyPatchTool().validate(safe_patch(), ToolContext(project_root=tmp_path))
    assert summary.files == ["app.py"]
    assert summary.added_lines == 1
    assert summary.removed_lines == 1


def test_patch_validation_rejects_malformed_patch(tmp_path) -> None:
    with pytest.raises(PatchValidationError):
        ApplyPatchTool().validate("not a patch", ToolContext(project_root=tmp_path))


@pytest.mark.parametrize(
    "patch",
    [
        "--- a/../outside.py\n+++ b/../outside.py\n@@ -1 +1 @@\n-a\n+b\n",
        "--- a/.env\n+++ b/.env\n@@ -1 +1 @@\n-a\n+b\n",
        "--- a/secret.pem\n+++ b/secret.pem\n@@ -1 +1 @@\n-a\n+b\n",
        "--- a/secret.key\n+++ b/secret.key\n@@ -1 +1 @@\n-a\n+b\n",
        "--- a/db.sqlite\n+++ b/db.sqlite\n@@ -1 +1 @@\n-a\n+b\n",
        "--- a/secrets/x.txt\n+++ b/secrets/x.txt\n@@ -1 +1 @@\n-a\n+b\n",
        "--- a/.harness/config.yaml\n+++ b/.harness/config.yaml\n@@ -1 +1 @@\n-a\n+b\n",
        "--- a/.git/config\n+++ b/.git/config\n@@ -1 +1 @@\n-a\n+b\n",
        "--- a/.codex/auth.json\n+++ b/.codex/auth.json\n@@ -1 +1 @@\n-a\n+b\n",
    ],
)
def test_patch_validation_rejects_blocked_paths(tmp_path, patch) -> None:
    with pytest.raises(Exception):
        ApplyPatchTool().validate(patch, ToolContext(project_root=tmp_path))


def test_patch_validation_rejects_deletions(tmp_path) -> None:
    (tmp_path / "app.py").write_text("x\n", encoding="utf-8")
    patch = "--- a/app.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    with pytest.raises(PatchValidationError):
        ApplyPatchTool().validate(patch, ToolContext(project_root=tmp_path))


def test_patch_validation_rejects_binary_patch(tmp_path) -> None:
    with pytest.raises(PatchValidationError):
        ApplyPatchTool().validate("GIT binary patch\n", ToolContext(project_root=tmp_path))


def test_approved_patch_is_applied_by_tool(tmp_path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "app.py").write_text('print("old")\nvalue = 1\n', encoding="utf-8")
    result = ApplyPatchTool().run({"patch": safe_patch()}, ToolContext(project_root=tmp_path))
    assert result.ok
    assert "value = 2" in (tmp_path / "app.py").read_text(encoding="utf-8")


def test_patch_is_atomic_when_later_file_hunk_fails(tmp_path) -> None:
    (tmp_path / "app.py").write_text("one\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("expected\n", encoding="utf-8")
    patch = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-one
+two
--- a/other.py
+++ b/other.py
@@ -1 +1 @@
-missing
+changed
"""
    patch = patch.replace("++++", "+++")
    result = ApplyPatchTool().run({"patch": patch}, ToolContext(project_root=tmp_path))
    assert not result.ok
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "one\n"
    assert (tmp_path / "other.py").read_text(encoding="utf-8") == "expected\n"


def test_patch_supports_multiple_hunks_in_one_file(tmp_path) -> None:
    (tmp_path / "app.py").write_text("a\nb\nc\nd\n", encoding="utf-8")
    patch = """--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
-a
+A
 b
@@ -3,2 +3,2 @@
 c
-d
+D
"""
    patch = patch.replace("++++", "+++")
    result = ApplyPatchTool().run({"patch": patch}, ToolContext(project_root=tmp_path))
    assert result.ok
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "A\nb\nc\nD\n"


def test_patch_supports_multiple_files(tmp_path) -> None:
    (tmp_path / "app.py").write_text("a\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("b\n", encoding="utf-8")
    patch = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-a
+A
--- a/other.py
+++ b/other.py
@@ -1 +1 @@
-b
+B
"""
    patch = patch.replace("++++", "+++")
    result = ApplyPatchTool().run({"patch": patch}, ToolContext(project_root=tmp_path))
    assert result.ok
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "A\n"
    assert (tmp_path / "other.py").read_text(encoding="utf-8") == "B\n"


def test_patch_rejects_context_mismatch_without_writing(tmp_path) -> None:
    (tmp_path / "app.py").write_text("actual\n", encoding="utf-8")
    patch = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-expected
+changed
"""
    patch = patch.replace("++++", "+++")
    result = ApplyPatchTool().run({"patch": patch}, ToolContext(project_root=tmp_path))
    assert not result.ok
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "actual\n"


def test_patch_handles_repeated_identical_lines_by_hunk_position(tmp_path) -> None:
    (tmp_path / "app.py").write_text("same\nsame\nsame\n", encoding="utf-8")
    patch = """--- a/app.py
+++ b/app.py
@@ -2,2 +2,2 @@
-same
+changed
 same
"""
    patch = patch.replace("++++", "+++")
    result = ApplyPatchTool().run({"patch": patch}, ToolContext(project_root=tmp_path))
    assert result.ok
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "same\nchanged\nsame\n"


def test_patch_handles_missing_trailing_newline(tmp_path) -> None:
    (tmp_path / "app.py").write_text("value = 1", encoding="utf-8")
    patch = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-value = 1
+value = 2
\\ No newline at end of file
"""
    patch = patch.replace("++++", "+++")
    result = ApplyPatchTool().run({"patch": patch}, ToolContext(project_root=tmp_path))
    assert result.ok
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 2"


def test_patch_preserves_crlf_when_adding_lines(tmp_path) -> None:
    (tmp_path / "app.py").write_text("a\r\nb\r\n", encoding="utf-8", newline="")
    patch = """--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
 a
+inserted
 b
"""
    patch = patch.replace("++++", "+++")
    result = ApplyPatchTool().run({"patch": patch}, ToolContext(project_root=tmp_path))
    assert result.ok
    assert (tmp_path / "app.py").read_text(encoding="utf-8", newline="") == "a\r\ninserted\r\nb\r\n"


def test_patch_rejects_context_excluded_paths(tmp_path) -> None:
    node_modules = tmp_path / "node_modules"
    node_modules.mkdir()
    (node_modules / "pkg.js").write_text("a\n", encoding="utf-8")
    patch = "--- a/node_modules/pkg.js\n+++ b/node_modules/pkg.js\n@@ -1 +1 @@\n-a\n+b\n"
    result = ApplyPatchTool().run(
        {"patch": patch},
        ToolContext(project_root=tmp_path, context_excludes=["node_modules/"]),
    )
    assert not result.ok
    assert (node_modules / "pkg.js").read_text(encoding="utf-8") == "a\n"


def test_patch_validation_rejects_file_creation(tmp_path) -> None:
    patch = "--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+new\n"
    with pytest.raises(PatchValidationError):
        ApplyPatchTool().validate(patch, ToolContext(project_root=tmp_path))


def test_patch_validation_rejects_rename(tmp_path) -> None:
    (tmp_path / "old.py").write_text("x\n", encoding="utf-8")
    patch = "--- a/old.py\n+++ b/new.py\n@@ -1 +1 @@\n-x\n+y\n"
    with pytest.raises(PatchValidationError):
        ApplyPatchTool().validate(patch, ToolContext(project_root=tmp_path))


def test_patch_validation_rejects_binary_file_marker(tmp_path) -> None:
    with pytest.raises(PatchValidationError):
        ApplyPatchTool().validate("Binary files a/image.png and b/image.png differ\n", ToolContext(project_root=tmp_path))
