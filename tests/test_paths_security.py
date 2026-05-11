from pathlib import Path

import pytest

from harness.paths import PathSecurityError, resolve_under_project
from harness.security import (
    SecretBlockedError,
    assert_not_secret_path,
    sanitize_for_logging,
    scan_text_for_secrets,
)


def test_resolve_under_project_allows_project_file(tmp_path) -> None:
    path = tmp_path / "src" / "a.py"
    path.parent.mkdir()
    path.write_text("x", encoding="utf-8")
    assert resolve_under_project(tmp_path, "src/a.py") == path.resolve()


def test_resolve_under_project_blocks_traversal(tmp_path) -> None:
    with pytest.raises(PathSecurityError):
        resolve_under_project(tmp_path, "../outside.txt")


def test_resolve_under_project_blocks_absolute_outside(tmp_path) -> None:
    with pytest.raises(PathSecurityError):
        resolve_under_project(tmp_path, Path("/tmp/outside.txt"))


def test_resolve_under_project_blocks_symlink_escape(tmp_path) -> None:
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(outside)
    with pytest.raises(PathSecurityError):
        resolve_under_project(tmp_path, "link")


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.local",
        "secret.pem",
        "secret.key",
        "db.sqlite",
        "secrets/file.txt",
        ".codex/auth.json",
    ],
)
def test_secret_paths_blocked(tmp_path, name) -> None:
    path = tmp_path / name
    with pytest.raises(SecretBlockedError):
        assert_not_secret_path(path)


def test_secret_scanner_redacts_values() -> None:
    findings = scan_text_for_secrets("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz\n")
    assert findings
    assert "abcdefghijklmnopqrstuvwxyz" not in findings[0].preview
    assert "[REDACTED]" in findings[0].preview


@pytest.mark.parametrize(
    "secret",
    [
        "Authorization: Bearer abcdefghijklmnop",
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz",
        "AWS_ACCESS_KEY_ID=AKIA1234567890ABCDEF",
        "password: correcthorsebatterystaple",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_secret_scanner_covers_common_secret_shapes(secret) -> None:
    findings = scan_text_for_secrets(secret)
    assert findings
    assert secret[-8:] not in findings[0].preview


def test_sanitize_for_logging_removes_secret_values() -> None:
    secret = "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz"
    sanitized = sanitize_for_logging({"line": secret, "nested": [secret]})
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in str(sanitized)
    assert "[REDACTED_SECRET]" in str(sanitized)
