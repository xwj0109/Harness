from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SecretBlockedError(ValueError):
    pass


SECRET_FILE_NAMES = {
    ".env",
    "id_rsa",
    "id_ed25519",
    "credentials",
    "credentials.json",
    "token",
    "tokens",
    "auth.json",
}

SECRET_SUFFIXES = {
    ".pem",
    ".key",
    ".sqlite",
}

SECRET_DIR_NAMES = {
    "secrets",
    ".codex",
}


def is_secret_path(path: Path) -> bool:
    parts = set(path.parts)
    name = path.name
    if name in SECRET_FILE_NAMES:
        return True
    if name.startswith(".env."):
        return True
    if path.suffix in SECRET_SUFFIXES:
        return True
    if parts.intersection(SECRET_DIR_NAMES):
        if ".codex" in parts and name != "auth.json":
            return False
        return True
    return False


def assert_not_secret_path(path: Path) -> None:
    if is_secret_path(path):
        raise SecretBlockedError(f"Blocked secret-like path: {path.name}")


@dataclass(frozen=True)
class SecretFinding:
    kind: str
    line: int
    preview: str

    def to_dict(self) -> dict[str, str | int]:
        return {"kind": self.kind, "line": self.line, "preview": self.preview}


SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("env_assignment", re.compile(r"(?i)\b[A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*\s*=\s*['\"]?[^'\"\s]{6,}")),
    ("password", re.compile(r"(?i)\bpassword\s*[:=]\s*['\"]?[^'\"\s]{6,}")),
]


def scan_text_for_secrets(text: str) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in SECRET_PATTERNS:
            match = pattern.search(line)
            if match:
                findings.append(SecretFinding(kind=kind, line=line_no, preview=_redact(line)))
                break
    return findings


def redact_secret_text(text: str) -> str:
    redacted = text
    for _kind, pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    return redacted


def sanitize_for_logging(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secret_text(value)
    if isinstance(value, dict):
        return {key: sanitize_for_logging(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_for_logging(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_for_logging(item) for item in value)
    return value


def _redact(value: str) -> str:
    stripped = value.strip()
    if len(stripped) <= 8:
        return "[REDACTED]"
    return f"{stripped[:4]}...[REDACTED]...{stripped[-4:]}"
