from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from harness.security import sanitize_for_logging


class ManagedActionRisk(str, Enum):
    READ_ONLY = "read_only"
    LOCAL_WORKSPACE_WRITE_LOW = "local_workspace_write_low"
    LOCAL_WORKSPACE_WRITE_MEDIUM = "local_workspace_write_medium"
    SANDBOXED_EXECUTION = "sandboxed_execution"
    HOSTED_PROVIDER = "hosted_provider"
    ACTIVE_REPO_APPLY_BACK = "active_repo_apply_back"
    DESTRUCTIVE = "destructive"
    EXTERNAL_NETWORK = "external_network"


class ManagedActionDecisionStatus(str, Enum):
    AUTO_ALLOWED = "auto_allowed"
    APPROVAL_REQUIRED = "approval_required"
    DENIED = "denied"
    UNSUPPORTED = "unsupported"


class ManagedActionSandboxStatus(str, Enum):
    NOT_RUN = "not_run"
    SAFE = "safe"
    DANGEROUS = "dangerous"


class ManagedActionSandboxAssessment(BaseModel):
    schema_version: str = "harness.managed_action_sandbox_assessment/v1"
    status: ManagedActionSandboxStatus
    sandbox_profile: str = "managed_action_preflight"
    executor: str
    dangerous: bool = False
    reasons: list[str] = Field(default_factory=list)
    expected_paths: list[str] = Field(default_factory=list)


class ManagedActionRoute(BaseModel):
    schema_version: str = "harness.managed_action_route/v1"
    intent: str
    confidence: Literal["exact", "pattern", "fallback"]
    risk: ManagedActionRisk
    executor: str
    normalized_arguments: dict[str, Any] = Field(default_factory=dict)
    required_approvals: list[str] = Field(default_factory=list)
    expected_outputs: list[str] = Field(default_factory=list)
    policy_notes: list[str] = Field(default_factory=list)


class ManagedActionDecision(BaseModel):
    schema_version: str = "harness.managed_action_decision/v1"
    status: ManagedActionDecisionStatus
    route: ManagedActionRoute
    reasons: list[str] = Field(default_factory=list)
    requires_human: bool = False
    sandbox_assessment: ManagedActionSandboxAssessment | None = None


class ManagedActionResult(BaseModel):
    schema_version: str = "harness.managed_action_result/v1"
    ok: bool
    status: str
    intent: str
    run_id: str | None = None
    created_paths: list[Path] = Field(default_factory=list)
    changed_paths: list[Path] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    report_path: Path | None = None
    manifest_path: Path | None = None
    message: str
    next_actions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


def route_managed_action(instruction: str, project_root: Path | None = None) -> ManagedActionRoute:
    normalized = _normalize(instruction)
    markdown_filename = _filename_from_text(instruction, {".md"})
    text_filename = _filename_from_text(instruction, {".txt"})
    python_filename = _filename_from_text(instruction, {".py"})
    writable_filename = markdown_filename or text_filename
    file_write_text = _file_write_text_from_text(instruction, writable_filename)
    directory_name = _directory_name_from_text(instruction)
    note_text = _note_text_from_text(instruction)

    if _looks_like_run_tests_request(normalized):
        return ManagedActionRoute(
            intent="run_tests",
            confidence="exact" if normalized in {"test", "run tests", "run the tests"} else "pattern",
            risk=ManagedActionRisk.SANDBOXED_EXECUTION,
            executor="run_tests",
            normalized_arguments={
                "suggested_command": "pytest -q",
                "scope": "managed_action",
                "request": sanitize_for_logging(instruction),
            },
            required_approvals=["docker_execution"],
            expected_outputs=["test_result.json", "final_report.md", "manifest.json"],
            policy_notes=["Sandboxed test execution requires approval before execution."],
        )
    if writable_filename and file_write_text is not None and _looks_like_file_write_request(normalized):
        return ManagedActionRoute(
            intent="write_file",
            confidence="exact",
            risk=ManagedActionRisk.LOCAL_WORKSPACE_WRITE_LOW,
            executor="write_file",
            normalized_arguments={
                "filename": writable_filename,
                "text": file_write_text,
                "allowed_extensions": [Path(writable_filename).suffix],
                "overwrite_policy": "append_or_create",
                "request": sanitize_for_logging(instruction),
            },
            expected_outputs=["created_file", "changed_file", "final_report.md", "manifest.json"],
            policy_notes=["Local low-risk file content write."],
        )
    if _looks_like_simple_python_script_request(normalized, python_filename):
        return ManagedActionRoute(
            intent="create_python_script",
            confidence="exact" if python_filename else "pattern",
            risk=ManagedActionRisk.LOCAL_WORKSPACE_WRITE_LOW,
            executor="create_file_with_content",
            normalized_arguments={
                "filename": _python_script_filename_from_text(instruction, python_filename),
                "text": _python_script_text_from_text(instruction),
                "allowed_extensions": [".py"],
                "overwrite_policy": "never",
                "request": sanitize_for_logging(instruction),
            },
            expected_outputs=["created_file", "final_report.md", "manifest.json"],
            policy_notes=["Local low-risk Python script creation without execution."],
        )
    if _looks_like_empty_markdown_request(normalized, markdown_filename):
        return ManagedActionRoute(
            intent="create_empty_markdown_file",
            confidence="exact" if markdown_filename else "pattern",
            risk=ManagedActionRisk.LOCAL_WORKSPACE_WRITE_LOW,
            executor="create_empty_file",
            normalized_arguments={
                "filename": markdown_filename or "scratch.md",
                "default_filename": "scratch.md",
                "allowed_extensions": [".md"],
                "overwrite_policy": "never",
                "request": sanitize_for_logging(instruction),
            },
            expected_outputs=["created_file", "final_report.md", "manifest.json"],
            policy_notes=["Local low-risk empty Markdown file creation."],
        )
    if _looks_like_empty_text_request(normalized, text_filename):
        return ManagedActionRoute(
            intent="create_empty_text_file",
            confidence="exact" if text_filename else "pattern",
            risk=ManagedActionRisk.LOCAL_WORKSPACE_WRITE_LOW,
            executor="create_empty_file",
            normalized_arguments={
                "filename": text_filename or "scratch.txt",
                "default_filename": "scratch.txt",
                "allowed_extensions": [".txt"],
                "overwrite_policy": "never",
                "request": sanitize_for_logging(instruction),
            },
            expected_outputs=["created_file", "final_report.md", "manifest.json"],
            policy_notes=["Local low-risk empty text file creation."],
        )
    if _looks_like_directory_request(normalized, directory_name):
        return ManagedActionRoute(
            intent="create_directory",
            confidence="exact" if directory_name else "pattern",
            risk=ManagedActionRisk.LOCAL_WORKSPACE_WRITE_LOW,
            executor="create_directory",
            normalized_arguments={
                "dirname": directory_name or "new-folder",
                "overwrite_policy": "no_op_if_exists",
                "request": sanitize_for_logging(instruction),
            },
            expected_outputs=["final_report.md", "manifest.json"],
            policy_notes=["Local low-risk directory creation."],
        )
    if _looks_like_note_request(normalized, note_text):
        return ManagedActionRoute(
            intent="local_note",
            confidence="pattern",
            risk=ManagedActionRisk.LOCAL_WORKSPACE_WRITE_LOW,
            executor="write_note_file",
            normalized_arguments={
                "filename": "notes.md",
                "text": note_text or instruction,
                "overwrite_policy": "append",
                "request": sanitize_for_logging(instruction),
            },
            expected_outputs=["created_file", "changed_file", "final_report.md", "manifest.json"],
            policy_notes=["Local low-risk note append."],
        )
    return ManagedActionRoute(
        intent="unsupported",
        confidence="fallback",
        risk=ManagedActionRisk.READ_ONLY,
        executor="none",
        normalized_arguments={"request": sanitize_for_logging(instruction)},
        expected_outputs=[],
        policy_notes=["No managed local action route matched."],
    )


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower()).strip(" .?!")


def _filename_from_text(text: str, allowed_suffixes: set[str]) -> str | None:
    suffix_pattern = "|".join(re.escape(suffix.lstrip(".")) for suffix in sorted(allowed_suffixes))
    match = re.search(rf"(?<!\S)([A-Za-z0-9_./\\-]+\.({suffix_pattern}))(?!\S)", text)
    if not match:
        return None
    filename = match.group(1)
    if Path(filename).suffix not in allowed_suffixes:
        return None
    return filename


def _directory_name_from_text(text: str) -> str | None:
    match = re.search(r"(?:folder|directory|dir)\s+(?:called|named)\s+([A-Za-z0-9][A-Za-z0-9_.-]*)", text, re.I)
    if match:
        dirname = match.group(1).strip(".")
        if "/" in dirname or "\\" in dirname or not dirname:
            return None
        return dirname
    match = re.search(
        r"(?:create|make|add)\s+(?:a\s+|an\s+|new\s+)?(?:folder|directory|dir)\s+([A-Za-z0-9][A-Za-z0-9_.-]*)",
        text,
        re.I,
    )
    if not match:
        return None
    dirname = match.group(1).strip(".")
    if dirname.lower() in {"that", "this", "the", "a", "an", "here"}:
        return None
    if "/" in dirname or "\\" in dirname or not dirname:
        return None
    return dirname


def _note_text_from_text(text: str) -> str | None:
    match = re.search(r"(?:write|add|save)\s+(?:a\s+)?note(?:\s+that|\s*:)?\s+(.+)", text, re.I)
    return match.group(1).strip() if match else None


def _file_write_text_from_text(text: str, filename: str | None) -> str | None:
    if not filename:
        return None
    quoted = re.search(r"['\"]([^'\"]*)['\"]", text)
    if quoted:
        return quoted.group(1)
    before_filename = re.search(
        rf"\b(?:write|add|append|put|save)\s+(.+?)\s+(?:to|into|in)\s+{re.escape(filename)}(?:\s|$)",
        text,
        re.I,
    )
    if before_filename:
        candidate = _clean_requested_file_text(before_filename.group(1))
        if candidate is not None:
            return candidate
    after_filename = re.search(
        rf"{re.escape(filename)}\s+(?:with|containing|that\s+says|content|text)\s*:?\s*(.+)",
        text,
        re.I,
    )
    if after_filename:
        candidate = _clean_requested_file_text(after_filename.group(1))
        if candidate is not None:
            return candidate
    colon_after_filename = re.search(rf"{re.escape(filename)}\s*:\s*(.+)", text, re.I)
    if colon_after_filename:
        candidate = _clean_requested_file_text(colon_after_filename.group(1))
        if candidate is not None:
            return candidate
    return None


def _clean_requested_file_text(candidate: str) -> str | None:
    text = candidate.strip()
    text = re.sub(r"^(?:the\s+)?(?:content|text)\s+(?:as|to\s+be)\s+", "", text, flags=re.I).strip()
    if not text:
        return None
    if text.casefold() in {"empty", "blank", "nothing"}:
        return None
    return text


def _looks_like_empty_markdown_request(normalized: str, filename: str | None) -> bool:
    words = set(normalized.split())
    if not {"create", "make", "add", "do"}.intersection(words):
        return False
    if "write" in words and "empty" not in words and "blank" not in words:
        return False
    if filename and filename.endswith(".md"):
        return True
    if ".md" not in normalized and "markdown" not in normalized:
        return False
    return any(marker in normalized for marker in {"empty", "blank", ".md", "markdown"})


def _looks_like_empty_text_request(normalized: str, filename: str | None) -> bool:
    words = set(normalized.split())
    if not {"create", "make", "add", "do"}.intersection(words):
        return False
    if "write" in words and "empty" not in words and "blank" not in words:
        return False
    if filename and filename.endswith(".txt"):
        return True
    if ".txt" not in normalized and "text file" not in normalized:
        return False
    return any(marker in normalized for marker in {"empty", "blank", ".txt", "text file"})


def _looks_like_directory_request(normalized: str, dirname: str | None) -> bool:
    if dirname is not None:
        return True
    return bool(re.search(r"\b(create|make|add)\s+(?:a\s+|an\s+|new\s+)?(?:folder|directory|dir)\b", normalized))


def _looks_like_note_request(normalized: str, note_text: str | None) -> bool:
    words = set(normalized.split())
    return bool({"write", "add", "save"}.intersection(words)) and ("note" in words or note_text is not None)


def _looks_like_file_write_request(normalized: str) -> bool:
    words = set(normalized.split())
    return bool({"write", "add", "append", "put", "save", "create", "make"}.intersection(words))


def _looks_like_run_tests_request(normalized: str) -> bool:
    return normalized in {"test", "run tests", "run the tests"}


def _looks_like_simple_python_script_request(normalized: str, filename: str | None) -> bool:
    words = set(normalized.split())
    if filename and bool({"create", "make", "add", "write"}.intersection(words)):
        return True
    if "python" not in words:
        return False
    if not {"create", "make", "add", "write"}.intersection(words):
        return False
    return any(marker in normalized for marker in (" script", " code", " file", " program", " prints", " print"))


def _python_script_text_from_text(text: str) -> str:
    if _looks_like_black_scholes_request(text):
        return "\n".join(
            [
                "from __future__ import annotations",
                "",
                "import argparse",
                "import math",
                "",
                "",
                "def normal_cdf(value: float) -> float:",
                "    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))",
                "",
                "",
                "def black_scholes_price(",
                "    spot: float,",
                "    strike: float,",
                "    time_to_expiry: float,",
                "    risk_free_rate: float,",
                "    volatility: float,",
                "    option_type: str = \"call\",",
                ") -> float:",
                "    if spot <= 0 or strike <= 0:",
                "        raise ValueError(\"spot and strike must be positive\")",
                "    if time_to_expiry <= 0:",
                "        raise ValueError(\"time_to_expiry must be positive\")",
                "    if volatility <= 0:",
                "        raise ValueError(\"volatility must be positive\")",
                "",
                "    sigma_sqrt_t = volatility * math.sqrt(time_to_expiry)",
                "    d1 = (",
                "        math.log(spot / strike)",
                "        + (risk_free_rate + 0.5 * volatility * volatility) * time_to_expiry",
                "    ) / sigma_sqrt_t",
                "    d2 = d1 - sigma_sqrt_t",
                "    discounted_strike = strike * math.exp(-risk_free_rate * time_to_expiry)",
                "",
                "    normalized_type = option_type.lower()",
                "    if normalized_type == \"call\":",
                "        return spot * normal_cdf(d1) - discounted_strike * normal_cdf(d2)",
                "    if normalized_type == \"put\":",
                "        return discounted_strike * normal_cdf(-d2) - spot * normal_cdf(-d1)",
                "    raise ValueError(\"option_type must be 'call' or 'put'\")",
                "",
                "",
                "def main() -> None:",
                "    parser = argparse.ArgumentParser(description=\"Black-Scholes option pricing\")",
                "    parser.add_argument(\"--spot\", type=float, default=100.0)",
                "    parser.add_argument(\"--strike\", type=float, default=100.0)",
                "    parser.add_argument(\"--time\", type=float, default=1.0, help=\"Years to expiry\")",
                "    parser.add_argument(\"--rate\", type=float, default=0.05, help=\"Continuously compounded risk-free rate\")",
                "    parser.add_argument(\"--vol\", type=float, default=0.2, help=\"Annualized volatility\")",
                "    parser.add_argument(\"--type\", choices=(\"call\", \"put\"), default=\"call\")",
                "    args = parser.parse_args()",
                "    price = black_scholes_price(args.spot, args.strike, args.time, args.rate, args.vol, args.type)",
                "    print(f\"{args.type.title()} price: {price:.6f}\")",
                "",
                "",
                "if __name__ == \"__main__\":",
                "    main()",
            ]
        )
    quoted = re.search(r"['\"]([^'\"]+)['\"]", text)
    if quoted:
        sentence = quoted.group(1).strip()
        return f"print({sentence!r})"
    match = re.search(r"\bprints?\s+(.+)$", text, re.I)
    if match:
        sentence = match.group(1).strip(" .")
        if sentence and sentence.lower() not in {"a simple sentence", "simple sentence", "a sentence"}:
            return f"print({sentence!r})"
    return "print('Hello from Harness.')"


def _python_script_filename_from_text(text: str, filename: str | None) -> str:
    if filename:
        return filename
    if _looks_like_black_scholes_request(text):
        return "black_scholes_pricing.py"
    return "simple_script.py"


def _looks_like_black_scholes_request(text: str) -> bool:
    normalized = _normalize(text).replace("-", " ")
    return "black scholes" in normalized or "blackscholes" in normalized
