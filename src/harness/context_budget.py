from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from harness.config import default_config, load_config


DEFAULT_CHARS_PER_TOKEN = 4
DEFAULT_TOKENIZER_ENCODING = "cl100k_base"
APPROXIMATE_TOKEN_BUDGET_WARNING = "approximate_token_budget_only"


@dataclass(frozen=True)
class TokenFit:
    text: str
    token_count: int
    truncated: bool
    original_token_count: int


@dataclass(frozen=True)
class ContextBudgetReport:
    schema_version: str
    tokenizer: str
    model_profile: str | None
    max_input_tokens: int
    used_input_tokens: int
    approximate: bool
    warnings: list[str]

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "tokenizer": self.tokenizer,
            "model_profile": self.model_profile,
            "max_input_tokens": self.max_input_tokens,
            "used_input_tokens": self.used_input_tokens,
            "approximate": self.approximate,
            "warnings": list(self.warnings),
        }


@runtime_checkable
class TokenBudgeter(Protocol):
    name: str
    approximate: bool

    def count(self, text: str) -> int:
        ...

    def fit(self, text: str, max_tokens: int, *, marker: str = "\n[TRUNCATED: context budget]\n") -> TokenFit:
        ...


class HeuristicTokenBudgeter:
    name = "heuristic_chars_per_token"
    approximate = True

    def count(self, text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // DEFAULT_CHARS_PER_TOKEN)

    def fit(self, text: str, max_tokens: int, *, marker: str = "\n[TRUNCATED: context budget]\n") -> TokenFit:
        return _fit_by_count(self, text, max_tokens, marker=marker)


class TiktokenBudgeter:
    approximate = False

    def __init__(self, encoding: object, *, encoding_name: str, model_name: str | None = None) -> None:
        self._encoding = encoding
        self.name = f"tiktoken:{encoding_name}" if model_name is None else f"tiktoken:{encoding_name}:{model_name}"

    def count(self, text: str) -> int:
        if not text:
            return 0
        encode = getattr(self._encoding, "encode")
        return len(encode(text))

    def fit(self, text: str, max_tokens: int, *, marker: str = "\n[TRUNCATED: context budget]\n") -> TokenFit:
        return _fit_by_count(self, text, max_tokens, marker=marker)


def budgeter_for_project(project_root: Path, *, model_profile: str | None = None) -> TokenBudgeter:
    try:
        cfg = load_config(project_root)
    except FileNotFoundError:
        cfg = default_config()
    profile = model_profile or cfg.chat.default_model_profile
    backend = cfg.backends.get(profile)
    model_name = str((backend.settings or {}).get("model") or "") if backend is not None else ""
    if _is_openai_like_profile(profile) or _is_openai_like_model(model_name):
        tokenizer = _tiktoken_budgeter(model_name or None)
        if tokenizer is not None:
            return tokenizer
    return HeuristicTokenBudgeter()


def model_profile_for_project(project_root: Path, *, model_profile: str | None = None) -> str:
    if model_profile:
        return model_profile
    try:
        return load_config(project_root).chat.default_model_profile
    except FileNotFoundError:
        return default_config().chat.default_model_profile


def legacy_char_budget_to_tokens(budget_chars: int) -> int:
    return max(0, budget_chars // DEFAULT_CHARS_PER_TOKEN)


def budget_report(
    budgeter: TokenBudgeter,
    *,
    model_profile: str | None,
    max_input_tokens: int,
    used_input_tokens: int,
) -> ContextBudgetReport:
    warnings = [APPROXIMATE_TOKEN_BUDGET_WARNING] if budgeter.approximate else []
    return ContextBudgetReport(
        schema_version="harness.context_budget_report/v1",
        tokenizer=budgeter.name,
        model_profile=model_profile,
        max_input_tokens=max_input_tokens,
        used_input_tokens=used_input_tokens,
        approximate=budgeter.approximate,
        warnings=warnings,
    )


def _fit_by_count(
    budgeter: TokenBudgeter,
    text: str,
    max_tokens: int,
    *,
    marker: str,
) -> TokenFit:
    original_tokens = budgeter.count(text)
    if original_tokens <= max_tokens:
        return TokenFit(text=text, token_count=original_tokens, truncated=False, original_token_count=original_tokens)
    if max_tokens <= 0:
        return TokenFit(text="", token_count=0, truncated=True, original_token_count=original_tokens)

    marker_tokens = budgeter.count(marker)
    if marker_tokens >= max_tokens:
        fitted_marker = _largest_prefix_within_budget(budgeter, marker, max_tokens)
        return TokenFit(
            text=fitted_marker,
            token_count=budgeter.count(fitted_marker),
            truncated=True,
            original_token_count=original_tokens,
        )

    prefix_budget = max_tokens - marker_tokens
    prefix = _largest_prefix_within_budget(budgeter, text, prefix_budget)
    fitted = prefix + marker
    while fitted and budgeter.count(fitted) > max_tokens:
        prefix = prefix[:-1]
        fitted = prefix + marker
    return TokenFit(
        text=fitted,
        token_count=budgeter.count(fitted),
        truncated=True,
        original_token_count=original_tokens,
    )


def _largest_prefix_within_budget(budgeter: TokenBudgeter, text: str, max_tokens: int) -> str:
    if max_tokens <= 0 or not text:
        return ""
    if budgeter.count(text) <= max_tokens:
        return text
    low = 0
    high = len(text)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid]
        if budgeter.count(candidate) <= max_tokens:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best


def _is_openai_like_profile(model_profile: str | None) -> bool:
    if not model_profile:
        return False
    normalized = model_profile.casefold()
    return any(marker in normalized for marker in ("codex", "openai", "gpt"))


def _is_openai_like_model(model_name: str | None) -> bool:
    if not model_name:
        return False
    normalized = model_name.casefold()
    return any(marker in normalized for marker in ("gpt", "o1", "o3", "o4", "codex"))


def _tiktoken_budgeter(model_name: str | None) -> TokenBudgeter | None:
    try:
        import tiktoken  # type: ignore[import-not-found]
    except ImportError:
        return None
    encoding = None
    encoding_name = DEFAULT_TOKENIZER_ENCODING
    if model_name:
        try:
            encoding = tiktoken.encoding_for_model(model_name)
            encoding_name = getattr(encoding, "name", encoding_name)
        except (KeyError, ValueError):
            encoding = None
    if encoding is None:
        try:
            encoding = tiktoken.get_encoding(DEFAULT_TOKENIZER_ENCODING)
        except (KeyError, ValueError):
            return None
    encoding_name = str(getattr(encoding, "name", encoding_name))
    return TiktokenBudgeter(encoding, encoding_name=encoding_name, model_name=model_name)
