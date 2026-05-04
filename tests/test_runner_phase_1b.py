from harness.backends.local_openai import LocalOpenAICompatibleBackend
from harness.config import default_config
from harness.memory.sqlite_store import SQLiteStore
from harness.runner import ReadOnlyRepoSummaryRunner


class FakeHttpClient:
    def __init__(self, responses):
        self.responses = list(responses)

    def get_json(self, url, headers, timeout):
        return {"data": [{"id": "local-model"}]}

    def post_json(self, url, headers, payload, timeout):
        return {"choices": [{"message": {"content": self.responses.pop(0)}}]}


def test_runner_executes_read_only_tools_through_command_protocol(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    cfg = default_config()
    store = SQLiteStore(tmp_path)
    store.initialize()
    backend = LocalOpenAICompatibleBackend(
        cfg.backends["local_openai_compatible"],
        FakeHttpClient(
            [
                '{"command":"list_files","arguments":{"path":"."}}',
                '{"command":"read_file","arguments":{"path":"README.md"}}',
                '{"command":"final_answer","arguments":{"answer":"Repo has a README."}}',
            ]
        ),
    )
    result = ReadOnlyRepoSummaryRunner(tmp_path, cfg, store, backend).run(
        "inspect this repo",
        "read_only_repo_summary",
    )
    assert result["final_summary"] == "Repo has a README."
    assert result["tools_executed"] == ["list_files", "read_file"]
    report = tmp_path / ".harness" / "runs" / result["run_id"] / "final_report.md"
    assert "local_openai_compatible" in report.read_text(encoding="utf-8")


def test_runner_retries_invalid_model_output(tmp_path) -> None:
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    cfg = default_config()
    store = SQLiteStore(tmp_path)
    store.initialize()
    backend = LocalOpenAICompatibleBackend(
        cfg.backends["local_openai_compatible"],
        FakeHttpClient(
            [
                "not json",
                '{"command":"final_answer","arguments":{"answer":"Recovered."}}',
            ]
        ),
    )
    result = ReadOnlyRepoSummaryRunner(tmp_path, cfg, store, backend).run(
        "inspect this repo",
        "read_only_repo_summary",
    )
    assert result["invalid_model_command_count"] == 1
    events = store.list_events(result["run_id"])
    assert any(event.event_type == "invalid_model_command" for event in events)


def test_runner_marks_run_failed_after_too_many_invalid_outputs(tmp_path) -> None:
    cfg = default_config()
    store = SQLiteStore(tmp_path)
    store.initialize()
    backend = LocalOpenAICompatibleBackend(
        cfg.backends["local_openai_compatible"],
        FakeHttpClient(["not json", "still not json"]),
    )
    result = ReadOnlyRepoSummaryRunner(
        tmp_path,
        cfg,
        store,
        backend,
        max_invalid_retries=1,
    ).run("inspect this repo", "read_only_repo_summary")
    run = store.get_run(result["run_id"])
    report = tmp_path / ".harness" / "runs" / result["run_id"] / "final_report.md"
    assert run.status == "failed"
    assert "too many invalid commands" in report.read_text(encoding="utf-8")


def test_runner_sanitizes_model_summary_in_final_report(tmp_path) -> None:
    cfg = default_config()
    store = SQLiteStore(tmp_path)
    store.initialize()
    backend = LocalOpenAICompatibleBackend(
        cfg.backends["local_openai_compatible"],
        FakeHttpClient(
            [
                '{"command":"final_answer","arguments":{"answer":"OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz"}}',
            ]
        ),
    )
    result = ReadOnlyRepoSummaryRunner(tmp_path, cfg, store, backend).run(
        "inspect this repo",
        "read_only_repo_summary",
    )
    report = tmp_path / ".harness" / "runs" / result["run_id"] / "final_report.md"
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in report.read_text(encoding="utf-8")
    assert "[REDACTED_SECRET]" in report.read_text(encoding="utf-8")


def test_read_only_repo_summary_rejects_apply_patch_command(tmp_path) -> None:
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    cfg = default_config()
    store = SQLiteStore(tmp_path)
    store.initialize()
    backend = LocalOpenAICompatibleBackend(
        cfg.backends["local_openai_compatible"],
        FakeHttpClient(
            [
                '{"command":"apply_patch","arguments":{"patch":"--- a/app.py\\n+++ b/app.py\\n@@ -1 +1 @@\\n-value = 1\\n+value = 2\\n"}}',
                '{"command":"final_answer","arguments":{"answer":"Patch rejected."}}',
            ]
        ),
    )
    result = ReadOnlyRepoSummaryRunner(tmp_path, cfg, store, backend).run(
        "inspect this repo",
        "read_only_repo_summary",
    )
    assert result["invalid_model_command_count"] == 1
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    events = store.list_events(result["run_id"])
    assert any(
        event.event_type == "invalid_model_command"
        and "apply_patch is not allowed" in event.payload["error"]
        for event in events
    )
