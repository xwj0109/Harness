import json
import sqlite3
import subprocess

from harness.backends.local_openai import LocalOpenAICompatibleBackend
from harness.config import default_config
from harness.edit_runner import NativeEditRunner, PatchApprovalDecision
from harness.memory.sqlite_store import SQLiteStore
from harness.test_runner import TestRunDecision


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


class StaticTestApproval:
    def __init__(self, decision="approved"):
        self.decision = decision

    def decide(self, details):
        return TestRunDecision(decision=self.decision)


class FakeModelDockerTestRunner:
    calls = []
    records = []

    def __init__(self, project_root, config, store, approval_provider):
        self.project_root = project_root
        self.config = config
        self.store = store
        self.approval_provider = approval_provider

    def run_in_existing_run(self, run_id, command, cwd=None, artifact_index=1, approval_provider=None):
        decision = (approval_provider or self.approval_provider).decide("details")
        suffix = "" if artifact_index == 1 else f"_{artifact_index}"
        run_dir = self.store.runs_dir / run_id
        stdout = run_dir / f"test_stdout{suffix}.txt"
        stderr = run_dir / f"test_stderr{suffix}.txt"
        result = run_dir / f"test_result{suffix}.json"
        stdout.write_text("OPENAI_API_KEY=[REDACTED_SECRET]\npassed\n", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        status = "tests_passed" if decision.approved else "execution_denied"
        exit_code = 0 if decision.approved else None
        record = {
            "status": status,
            "command": list(command),
            "cwd": cwd,
            "image": self.config.sandbox.image,
            "network": self.config.sandbox.network,
            "timeout_seconds": self.config.sandbox.timeout_seconds,
            "memory_limit": self.config.sandbox.memory_limit,
            "cpu_limit": self.config.sandbox.cpu_limit,
            "workdir": "/workspace/tests" if cwd == "tests" else "/workspace",
            "approval_decision": decision.decision,
            "exit_code": exit_code,
            "duration_seconds": 0.1,
            "timed_out": False,
            "failure_hint": "",
            "stdout_summary": "OPENAI_API_KEY=[REDACTED_SECRET]\npassed",
            "stderr_summary": "",
            "stdout_artifact": str(stdout),
            "stderr_artifact": str(stderr),
            "result_artifact": str(result),
        }
        result.write_text(json.dumps(record), encoding="utf-8")
        self.store.register_artifact(run_id, f"test_stdout{suffix}", stdout)
        self.store.register_artifact(run_id, f"test_stderr{suffix}", stderr)
        self.store.register_artifact(run_id, f"test_result{suffix}", result)
        if decision.approved:
            FakeModelDockerTestRunner.calls.append((run_id, list(command), cwd, artifact_index))
        FakeModelDockerTestRunner.records.append(record)
        return record


def reset_fake_docker_tests():
    FakeModelDockerTestRunner.calls = []
    FakeModelDockerTestRunner.records = []


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


def test_native_edit_accepts_run_tests_and_returns_json_observation(tmp_path) -> None:
    reset_fake_docker_tests()
    setup_repo(tmp_path)
    (tmp_path / "tests").mkdir()
    before = (tmp_path / "app.py").read_bytes()
    cfg = default_config()
    store = SQLiteStore(tmp_path)
    store.initialize()
    backend = LocalOpenAICompatibleBackend(
        cfg.backends["local_openai_compatible"],
        FakeHttpClient(
            [
                json.dumps(
                    {
                        "command": "run_tests",
                        "arguments": {"command": ["python", "-m", "pytest", "-q"], "cwd": "tests"},
                    }
                ),
                '{"command":"final_answer","arguments":{"answer":"tests done"}}',
            ]
        ),
    )

    result = NativeEditRunner(
        tmp_path,
        cfg,
        store,
        backend,
        StaticApproval("denied"),
        test_approval_provider=StaticTestApproval("approved"),
        docker_test_runner_factory=FakeModelDockerTestRunner,
    ).run("run tests", "simple_code_edit")

    assert result["tools_executed"] == ["run_tests"]
    assert result["test_runs"][0]["status"] == "tests_passed"
    assert result["test_runs"][0]["exit_code"] == 0
    assert FakeModelDockerTestRunner.calls == [
        (result["run_id"], ["python", "-m", "pytest", "-q"], "tests", 1)
    ]
    assert (tmp_path / "app.py").read_bytes() == before
    run_dir = tmp_path / ".harness" / "runs" / result["run_id"]
    transcript = (run_dir / "transcript.jsonl").read_text(encoding="utf-8")
    assert '"tool": "run_tests"' in transcript
    assert '"status": "tests_passed"' in transcript
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in transcript
    report = (run_dir / "final_report.md").read_text(encoding="utf-8")
    assert "## Test Executions" in report
    assert "python -m pytest -q" in report


def test_native_edit_denied_run_tests_does_not_call_docker(tmp_path) -> None:
    reset_fake_docker_tests()
    setup_repo(tmp_path)
    cfg = default_config()
    store = SQLiteStore(tmp_path)
    store.initialize()
    backend = LocalOpenAICompatibleBackend(
        cfg.backends["local_openai_compatible"],
        FakeHttpClient(
            [
                '{"command":"run_tests","arguments":{"command":["pytest","-q"]}}',
                '{"command":"final_answer","arguments":{"answer":"denied"}}',
            ]
        ),
    )

    result = NativeEditRunner(
        tmp_path,
        cfg,
        store,
        backend,
        StaticApproval("denied"),
        test_approval_provider=StaticTestApproval("denied"),
        docker_test_runner_factory=FakeModelDockerTestRunner,
    ).run("run tests", "simple_code_edit")

    assert result["test_runs"][0]["status"] == "execution_denied"
    assert FakeModelDockerTestRunner.calls == []


def test_native_edit_multiple_run_tests_use_non_clobbering_artifacts(tmp_path) -> None:
    reset_fake_docker_tests()
    setup_repo(tmp_path)
    cfg = default_config()
    store = SQLiteStore(tmp_path)
    store.initialize()
    backend = LocalOpenAICompatibleBackend(
        cfg.backends["local_openai_compatible"],
        FakeHttpClient(
            [
                '{"command":"run_tests","arguments":{"command":["pytest","-q"]}}',
                '{"command":"run_tests","arguments":{"command":["pytest","tests/test_x.py"]}}',
                '{"command":"final_answer","arguments":{"answer":"twice"}}',
            ]
        ),
    )

    result = NativeEditRunner(
        tmp_path,
        cfg,
        store,
        backend,
        StaticApproval("denied"),
        test_approval_provider=StaticTestApproval("approved"),
        docker_test_runner_factory=FakeModelDockerTestRunner,
    ).run("run tests twice", "simple_code_edit")

    run_dir = tmp_path / ".harness" / "runs" / result["run_id"]
    assert [record["stdout_artifact"] for record in result["test_runs"]] == [
        str(run_dir / "test_stdout.txt"),
        str(run_dir / "test_stdout_2.txt"),
    ]
    assert (run_dir / "test_result.json").exists()
    assert (run_dir / "test_result_2.json").exists()


def test_native_edit_run_tests_failure_statuses_are_observations(tmp_path) -> None:
    class StatusDockerTestRunner(FakeModelDockerTestRunner):
        status = "tests_failed"
        exit_code = 1
        timed_out = False

        def run_in_existing_run(self, run_id, command, cwd=None, artifact_index=1, approval_provider=None):
            record = super().run_in_existing_run(run_id, command, cwd, artifact_index, approval_provider)
            record.update(
                {
                    "status": self.status,
                    "exit_code": self.exit_code,
                    "timed_out": self.timed_out,
                    "failure_hint": "pytest_failures" if self.status == "tests_failed" else "",
                }
            )
            return record

    for status, exit_code, timed_out in [
        ("tests_failed", 1, False),
        ("tests_timed_out", None, True),
        ("docker_unavailable", None, False),
        ("docker_image_missing", None, False),
    ]:
        reset_fake_docker_tests()
        project = tmp_path / status
        project.mkdir()
        setup_repo(project)
        cfg = default_config()
        store = SQLiteStore(project)
        store.initialize()
        backend = LocalOpenAICompatibleBackend(
            cfg.backends["local_openai_compatible"],
            FakeHttpClient(
                [
                    '{"command":"run_tests","arguments":{"command":["pytest","-q"]}}',
                    '{"command":"final_answer","arguments":{"answer":"observed"}}',
                ]
            ),
        )
        StatusDockerTestRunner.status = status
        StatusDockerTestRunner.exit_code = exit_code
        StatusDockerTestRunner.timed_out = timed_out

        result = NativeEditRunner(
            project,
            cfg,
            store,
            backend,
            StaticApproval("denied"),
            test_approval_provider=StaticTestApproval("approved"),
            docker_test_runner_factory=StatusDockerTestRunner,
        ).run("run tests", "simple_code_edit")

        assert result["final_answer"] == "observed"
        assert result["test_runs"][0]["status"] == status
        assert result["test_runs"][0]["timed_out"] is timed_out
