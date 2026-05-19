from __future__ import annotations

from harness.context_budget import (
    APPROXIMATE_TOKEN_BUDGET_WARNING,
    HeuristicTokenBudgeter,
    TiktokenBudgeter,
    budget_report,
    budgeter_for_project,
    legacy_char_budget_to_tokens,
)


def test_heuristic_budgeter_matches_legacy_character_estimate() -> None:
    budgeter = HeuristicTokenBudgeter()

    assert budgeter.count("") == 0
    assert budgeter.count("abc") == 1
    assert budgeter.count("abcd") == 1
    assert budgeter.count("abcdefgh") == 2
    assert legacy_char_budget_to_tokens(32_000) == 8_000


def test_heuristic_fit_truncates_within_token_budget() -> None:
    budgeter = HeuristicTokenBudgeter()

    fit = budgeter.fit("a" * 100, 8, marker="\n[truncated]\n")

    assert fit.truncated is True
    assert fit.original_token_count == 25
    assert fit.token_count <= 8
    assert fit.text.endswith("\n[truncated]\n")
    assert len(fit.text) < 100


def test_budgeter_for_project_falls_back_when_tiktoken_unavailable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("harness.context_budget._tiktoken_budgeter", lambda _model_name: None)

    budgeter = budgeter_for_project(tmp_path, model_profile="codex_cli")

    assert isinstance(budgeter, HeuristicTokenBudgeter)
    assert budgeter.approximate is True


def test_tiktoken_budgeter_uses_supplied_encoding_without_backend_calls() -> None:
    class FakeEncoding:
        name = "fake_encoding"

        def encode(self, text: str) -> list[str]:
            return text.split()

    budgeter = TiktokenBudgeter(FakeEncoding(), encoding_name="fake_encoding", model_name="gpt-test")

    assert budgeter.approximate is False
    assert budgeter.count("one two three") == 3
    assert budgeter.fit("one two three four five", 3, marker=" ...").token_count <= 3


def test_budget_report_marks_heuristic_budgeting_as_approximate() -> None:
    report = budget_report(
        HeuristicTokenBudgeter(),
        model_profile="codex_cli",
        max_input_tokens=100,
        used_input_tokens=12,
    )

    assert report.schema_version == "harness.context_budget_report/v1"
    assert report.approximate is True
    assert APPROXIMATE_TOKEN_BUDGET_WARNING in report.warnings
    assert report.to_payload()["used_input_tokens"] == 12

