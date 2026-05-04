import json
import sqlite3
import subprocess

from harness.backends.local_openai import LocalOpenAICompatibleBackend
from harness.config import default_config
from harness.edit_runner import NativeEditRunner, PatchApprovalDecision
from harness.memory.sqlite_store import SQLiteStore


class FakeHttpClient:
    def __init__(self, responses):
        self.responses = list(responses)

    def get_json(self, url, headers, timeout):
        return {"data": [{"id": "local-model"}]}

    def post_json(self, url, headers, payload, timeout):
        return {"choices": [{"message": {"content": self.responses.pop(0)}}]}


class StaticApproval:
    def __init__(self, decision, reason=None):
        self.decision = decision
        self.reason = reason

    def decide(self, patch, summary):
        return PatchApprovalDecision(decision=self.decision, reason=self.reason)


def patch_text() -> str:
    return """--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 print("old")
-value = 1
+value = 2
"""


def patch_command_json() -> str:
    return json.dumps({"command": "apply_patch", "arguments": {"patch": patch_text()}})


def setup_repo(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "app.py").write_text('print("old")\nvalue = 1\n', encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True, capture_output=True)


def test_native_edit_requires_approval_and_denied_patch_not_applied(tmp_path) -> None:
    setup_repo(tmp_path)
    cfg = default_config()
    store = SQLiteStore(tmp_path)
    store.initialize()
    backend = LocalOpenAICompatibleBackend(
        cfg.backends["local_openai_compatible"],
        FakeHttpClient(
            [
                patch_command_json(),
                '{"command":"final_answer","arguments":{"answer":"done"}}',
            ]
        ),
    )
    result = NativeEditRunner(tmp_path, cfg, store, backend, StaticApproval("denied")).run(
        "change value",
        "simple_code_edit",
    )
    assert "value = 1" in (tmp_path / "app.py").read_text(encoding="utf-8")
    assert result["patch_decisions"][0]["decision"] == "denied"
    assert result["changed_files"] == []


def test_native_edit_approved_patch_is_applied_and_reported(tmp_path) -> None:
    setup_repo(tmp_path)
    cfg = default_config()
    store = SQLiteStore(tmp_path)
    store.initialize()
    backend = LocalOpenAICompatibleBackend(
        cfg.backends["local_openai_compatible"],
        FakeHttpClient(
            [
                patch_command_json(),
                '{"command":"final_answer","arguments":{"answer":"changed"}}',
            ]
        ),
    )
    result = NativeEditRunner(tmp_path, cfg, store, backend, StaticApproval("approved")).run(
        "change value",
        "simple_code_edit",
    )
    assert "value = 2" in (tmp_path / "app.py").read_text(encoding="utf-8")
    assert result["changed_files"] == ["app.py"]
    report = tmp_path / ".harness" / "runs" / result["run_id"] / "final_report.md"
    report_text = report.read_text(encoding="utf-8")
    assert "Patch Approval Decisions" in report_text
    assert "app.py" in report_text
    assert "Final Git Diff Summary" in report_text
    assert "1 insertion" in report_text


def test_native_edit_persists_blocked_patch_event(tmp_path) -> None:
    setup_repo(tmp_path)
    cfg = default_config()
    store = SQLiteStore(tmp_path)
    store.initialize()
    blocked_patch = """--- a/.harness/config.yaml
+++ b/.harness/config.yaml
@@ -1 +1 @@
-a
+b
""".replace("++++", "+++")
    backend = LocalOpenAICompatibleBackend(
        cfg.backends["local_openai_compatible"],
        FakeHttpClient(
            [
                json.dumps({"command": "apply_patch", "arguments": {"patch": blocked_patch}}),
                '{"command":"final_answer","arguments":{"answer":"blocked"}}',
            ]
        ),
    )
    result = NativeEditRunner(tmp_path, cfg, store, backend, StaticApproval("approved")).run(
        "change blocked path",
        "simple_code_edit",
    )
    assert result["patch_decisions"][0]["decision"] == "blocked"
    events = store.list_events(result["run_id"])
    assert any(event.event_type == "patch_blocked" for event in events)


def test_native_edit_persists_approval_denial_and_sanitizes_artifacts(tmp_path) -> None:
    setup_repo(tmp_path)
    cfg = default_config()
    store = SQLiteStore(tmp_path)
    store.initialize()
    secret = "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz"
    backend = LocalOpenAICompatibleBackend(
        cfg.backends["local_openai_compatible"],
        FakeHttpClient(
            [
                patch_command_json(),
                json.dumps({"command": "final_answer", "arguments": {"answer": f"done {secret}"}}),
            ]
        ),
    )
    result = NativeEditRunner(
        tmp_path,
        cfg,
        store,
        backend,
        StaticApproval("denied", reason=f"do not apply {secret}"),
    ).run("change value", "simple_code_edit")
    assert "value = 1" in (tmp_path / "app.py").read_text(encoding="utf-8")
    events = store.list_events(result["run_id"])
    assert any(event.event_type == "patch_denied" for event in events)
    run_dir = tmp_path / ".harness" / "runs" / result["run_id"]
    artifact_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in [run_dir / "events.jsonl", run_dir / "transcript.jsonl", run_dir / "final_report.md"]
    )
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in artifact_text
    with sqlite3.connect(tmp_path / ".harness" / "harness.sqlite") as conn:
        rows = conn.execute("SELECT payload_json FROM events WHERE run_id = ?", (result["run_id"],)).fetchall()
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in "\n".join(row[0] for row in rows)


def test_changed_files_exclude_harness_and_cache_artifacts(tmp_path) -> None:
    setup_repo(tmp_path)
    cfg = default_config()
    store = SQLiteStore(tmp_path)
    store.initialize()
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".pytest_cache" / "README").write_text("cache\n", encoding="utf-8")
    backend = LocalOpenAICompatibleBackend(
        cfg.backends["local_openai_compatible"],
        FakeHttpClient(
            [
                patch_command_json(),
                '{"command":"final_answer","arguments":{"answer":"changed"}}',
            ]
        ),
    )
    result = NativeEditRunner(tmp_path, cfg, store, backend, StaticApproval("approved")).run(
        "change value",
        "simple_code_edit",
    )
    assert result["changed_files"] == ["app.py"]
