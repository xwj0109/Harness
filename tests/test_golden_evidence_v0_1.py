import json
import re
import subprocess
from copy import deepcopy
from pathlib import Path

from typer.testing import CliRunner

from harness.approvals import ApprovalStore
from harness.backends.codex_cli import CodexCliBackend, CodexRunResult, NETWORK_NOT_ENFORCEABLE
from harness.chat import ChatSessionState, handle_chat_input
from harness.chat_model import ChatContext, ChatDelta, ChatMessage, ChatResponse
from harness.cli.main import app
from harness.memory.sqlite_store import SQLiteStore
from harness.models import BackendCapabilities, BackendStatus


runner = CliRunner()


class StaticModel:
    def __init__(self, response: str) -> None:
        self.response = response

    def stream(self, messages: list[ChatMessage], context: ChatContext):
        yield ChatDelta(content=self.complete(messages, context).content)

    def complete(self, messages: list[ChatMessage], context: ChatContext) -> ChatResponse:
        return ChatResponse(content=self.response)


class GoldenCodexBackend(CodexCliBackend):
    def __init__(self, config, *, final_message: str = "golden evidence", edit_text: str | None = None):
        super().__init__(config)
        self.final_message = final_message
        self.edit_text = edit_text

    def preflight(self):
        return BackendStatus(
            available=True,
            metadata=self.config.metadata,
            capabilities=BackendCapabilities(
                supports_exec=True,
                supports_cd=True,
                supports_read_only_sandbox=True,
                supports_workspace_write_sandbox=True,
                supports_ask_for_approval=True,
                supports_json_events=True,
                supports_output_last_message=True,
            ),
        )

    def run_read_only(self, project_root, prompt, final_message_path):
        if final_message_path:
            final_message_path.write_text(self.final_message, encoding="utf-8")
        return CodexRunResult(
            ["codex", "exec", "--cd", str(project_root), "--sandbox", "read-only"],
            "",
            "",
            0,
            [],
            self.final_message,
        )

    def run_edit(self, isolated_workspace, prompt, final_message_path):
        if self.edit_text is not None:
            (Path(isolated_workspace) / "app.py").write_text(self.edit_text, encoding="utf-8")
        if final_message_path:
            final_message_path.write_text(self.final_message, encoding="utf-8")
        return (
            CodexRunResult(
                ["codex", "exec", "--cd", str(isolated_workspace), "--sandbox", "workspace-write"],
                "",
                "",
                0,
                [],
                self.final_message,
            ),
            self.preflight().capabilities,
            NETWORK_NOT_ENFORCEABLE,
        )


def _install_deterministic_preflights(monkeypatch) -> None:
    class FakeCodexBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=True,
                metadata=self.config.metadata,
                capabilities=self.config.capabilities.model_copy(update={"supports_exec": True}),
            )

    class FakeLocalBackend:
        def __init__(self, config):
            self.config = config

        def preflight(self):
            return BackendStatus(
                available=False,
                reason="local unavailable",
                metadata=self.config.metadata,
                capabilities=self.config.capabilities,
            )

    class FakeDockerVersion:
        returncode = 0
        stdout = "Docker version test\n"
        stderr = ""

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", FakeCodexBackend)
    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", FakeLocalBackend)
    monkeypatch.setattr("harness.cli.main.shutil.which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr("harness.cli.main.subprocess.run", lambda *args, **kwargs: FakeDockerVersion())


def _json_result(args, expected_exit=0, input_text=None):
    result = runner.invoke(app, args, input=input_text)
    assert result.exit_code == expected_exit, result.output
    return json.loads(result.output)


def _request(tool: str, arguments: dict) -> str:
    return json.dumps({"type": "harness.tool_request/v1", "tool": tool, "arguments": arguments})


def _init_git_project(project: Path) -> None:
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)
    (project / ".gitignore").write_text(".harness/\n", encoding="utf-8")
    (project / "README.md").write_text("# Golden Project\n", encoding="utf-8")
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=project, check=True, capture_output=True)


def _create_objective(project: Path, title: str = "Golden objective") -> dict:
    return _json_result(
        [
            "objectives",
            "add",
            "--title",
            title,
            "--workbench",
            "coding",
            "--project",
            str(project),
            "--output",
            "json",
        ]
    )["objective"]


def _create_task(project: Path, objective_id: str, *, title: str, adapter: str, task_type: str) -> dict:
    return _json_result(
        [
            "tasks",
            "add",
            "--title",
            title,
            "--objective",
            objective_id,
            "--workbench",
            "coding",
            "--execution-adapter",
            adapter,
            "--task-type",
            task_type,
            "--project",
            str(project),
            "--output",
            "json",
        ]
    )["task"]


def _add_scoped_codex_approval(project: Path, objective_id: str, *, adapter: str, task_type: str) -> None:
    ApprovalStore(project).add(
        backend="codex_cli",
        data_boundary="hosted_provider",
        task_types=[task_type],
        duration_hours=8,
        allowed_adapters=[adapter],
        allowed_objective_ids=[objective_id],
        autonomy_scope="supervised-codex",
    )


def _normalize(value, project_root):
    normalized = deepcopy(value)
    project = str(project_root.resolve())

    def visit(item):
        if isinstance(item, dict):
            return {key: visit(val) for key, val in item.items()}
        if isinstance(item, list):
            return [visit(val) for val in item]
        if isinstance(item, str):
            item = item.replace(project, "<PROJECT_ROOT>")
            item = re.sub(r"run_[0-9a-f]{12}", "<RUN_ID>", item)
            item = re.sub(r"appr_[0-9a-f]{12}", "<APPROVAL_ID>", item)
            item = re.sub(r"evt_[0-9a-f]{12}", "<ID>", item)
            item = re.sub(r"art_[0-9a-f]{12}", "<ID>", item)
            item = re.sub(r"backend_[0-9a-f]{12}", "<ID>", item)
            item = re.sub(r"\d{4}-\d{2}-\d{2}T[^\"\\s]+", "<TIMESTAMP>", item)
            return item
        return item

    return visit(normalized)


def _assert_no_config_internals(payload) -> None:
    serialized = json.dumps(payload)
    assert '"settings"' not in serialized
    assert "base_url" not in serialized
    assert "api_key" not in serialized
    assert "api_key_env" not in serialized


def test_golden_doctor_evidence_contract(tmp_path, monkeypatch) -> None:
    _install_deterministic_preflights(monkeypatch)

    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    payload = _json_result(["doctor", "--project", str(tmp_path), "--output", "json"])
    normalized = _normalize(payload, tmp_path)
    checks = {check["id"]: check for check in normalized["checks"]}

    assert normalized["schema_version"] == "harness.doctor/v1"
    assert normalized["project_root"] == "<PROJECT_ROOT>"
    assert normalized["ok"] is True
    assert set(checks) == {
        "initialized",
        "config_loadable",
        "local_artifact_ignores",
        "backend_descriptors",
        "backend_preflight",
        "docker_binary",
        "dockerfile_validation",
        "sandbox_safety",
    }
    assert checks["initialized"]["status"] == "pass"
    assert checks["config_loadable"]["status"] == "pass"
    assert checks["backend_preflight"]["status"] == "warn"
    assert checks["docker_binary"]["status"] == "pass"
    assert checks["sandbox_safety"]["status"] == "pass"
    _assert_no_config_internals(normalized)


def test_golden_manifest_and_show_evidence_contract(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    created = runner.invoke(
        app,
        [
            "dev",
            "create-run",
            "--goal",
            "golden diagnostic run",
            "--task-type",
            "phase_1a_test",
            "--project",
            str(tmp_path),
        ],
    )
    assert created.exit_code == 0, created.output
    run_id = created.output.split("Created run ", 1)[1].splitlines()[0]

    manifest_path = tmp_path / ".harness" / "runs" / run_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    shown = _json_result(["show", run_id, "--project", str(tmp_path), "--output", "json"])
    normalized_manifest = _normalize(manifest, tmp_path)
    normalized_shown = _normalize(shown, tmp_path)

    assert normalized_shown == normalized_manifest
    assert normalized_manifest["schema_version"] == "harness.manifest/v1.1"
    assert normalized_manifest["run_id"] == "<RUN_ID>"
    assert normalized_manifest["run_mode"] == "dev"
    assert normalized_manifest["status"] == "completed"
    assert normalized_manifest["backend_descriptor"] is None
    assert normalized_manifest["effective_policy"]["schema_version"] == "harness.effective_policy/v1"
    assert normalized_manifest["effective_policy"]["subject_kind"] == "run"
    assert normalized_manifest["effective_policy_sha256"]
    assert {artifact["kind"] for artifact in normalized_manifest["artifacts"]} == {
        "events",
        "transcript",
        "final_report",
        "manifest",
    }


def test_golden_backend_and_approval_json_evidence_contract(tmp_path, monkeypatch) -> None:
    _install_deterministic_preflights(monkeypatch)

    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    backends = _json_result(["backends", "--project", str(tmp_path), "--output", "json"])
    preflight = _json_result(["backends", "preflight", "--project", str(tmp_path), "--output", "json"])

    backend_names = {backend["name"] for backend in backends["backends"]}
    backend_by_name = {backend["name"]: backend for backend in backends["backends"]}
    preflight_by_name = {backend["name"]: backend for backend in preflight["backends"]}
    assert backends["schema_version"] == "harness.backends/v1"
    assert preflight["schema_version"] == "harness.backend_preflight/v1"
    assert backend_names == {"codex_cli", "local_openai_compatible", "paid_openai_compatible"}
    assert backend_by_name["paid_openai_compatible"]["constraints"] == [
        "disabled_by_default",
        "no_automatic_fallback",
        "preflight_skipped",
    ]
    assert preflight_by_name["codex_cli"]["available"] is True
    assert preflight_by_name["local_openai_compatible"]["available"] is False
    assert preflight_by_name["paid_openai_compatible"]["available"] is False
    assert preflight_by_name["paid_openai_compatible"]["reason"] == "Paid backend preflight skipped; disabled by default."
    _assert_no_config_internals(backends)
    _assert_no_config_internals(preflight)

    added = runner.invoke(
        app,
        [
            "approvals",
            "add",
            "--backend",
            "codex_cli",
            "--data-boundary",
            "hosted_provider",
            "--task-types",
            "repo_planning",
            "--duration-days",
            "30",
            "--project",
            str(tmp_path),
        ],
    )
    assert added.exit_code == 0, added.output
    approval_id = added.output.split("Created approval ", 1)[1].strip()
    assert runner.invoke(app, ["approvals", "revoke", approval_id, "--project", str(tmp_path)]).exit_code == 0

    approvals = _normalize(_json_result(["approvals", "--project", str(tmp_path), "--output", "json"]), tmp_path)
    assert approvals["schema_version"] == "harness.approvals/v1"
    assert approvals["approvals"] == [
        {
            "allowed_adapters": [],
            "allowed_objective_ids": [],
            "allowed_task_types": ["repo_planning"],
            "allowed_workbenches": [],
            "autonomy_scope": None,
            "backend": "codex_cli",
            "created_at": "<TIMESTAMP>",
            "data_boundary": "hosted_provider",
            "expires_at": "<TIMESTAMP>",
            "id": "<APPROVAL_ID>",
            "max_context_bytes": None,
            "max_runs": None,
            "max_total_runtime_seconds": None,
            "project_root": "<PROJECT_ROOT>",
            "reason": None,
            "revoked": True,
            "revoked_at": "<TIMESTAMP>",
            "task_types": ["repo_planning"],
        }
    ]


def test_golden_hosted_boundary_denial_fails_closed_without_run(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    before_runs = list((tmp_path / ".harness" / "runs").iterdir())

    result = runner.invoke(
        app,
        [
            "run",
            "plan a safe change",
            "--project",
            str(tmp_path),
            "--task-type",
            "repo_planning",
        ],
        input="n\n",
    )

    assert result.exit_code != 0
    assert "Hosted data-boundary approval required" in result.output
    assert "Hosted data-boundary approval denied." in result.output
    assert list((tmp_path / ".harness" / "runs").iterdir()) == before_runs


def test_golden_autonomous_repo_summary_writes_artifact_evidence(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    (tmp_path / "README.md").write_text("# Golden Summary\n", encoding="utf-8")
    objective = _create_objective(tmp_path, "Golden repo summary")
    task = _create_task(
        tmp_path,
        objective["id"],
        title="Summarize repository",
        adapter="read_only_summary",
        task_type="read_only_repo_summary",
    )
    _add_scoped_codex_approval(
        tmp_path,
        objective["id"],
        adapter="read_only_summary",
        task_type="read_only_repo_summary",
    )
    monkeypatch.setattr(
        "harness.daemon_adapters.CodexCliBackend",
        lambda config: GoldenCodexBackend(config, final_message="repo summary evidence"),
    )

    result = _json_result(
        [
            "objectives",
            "run",
            objective["id"],
            "--autonomy",
            "supervised-codex",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ]
    )

    assert result["schema_version"] == "harness.autonomous_objective_run/v1"
    assert result["stop_reason"] == "objective_succeeded"
    assert result["adapter_dispatches"] == 1
    step = result["step_results"][0]
    assert step["task_id"] == task["id"]
    manifest = SQLiteStore(tmp_path).build_run_manifest(step["run_id"]).model_dump(mode="json")
    assert manifest["task_id"] == task["id"]
    assert manifest["objective_id"] == objective["id"]
    assert manifest["approval_id"].startswith("appr_")
    assert manifest["autonomy_decision_id"].startswith("adec_")
    assert {"final_report", "manifest", "events"} <= {artifact["kind"] for artifact in manifest["artifacts"]}


def test_golden_autonomous_planning_creates_objective_task_graph_evidence(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    state = ChatSessionState(autonomy_profile_id="safe-local")
    model = StaticModel(
        _request(
            "create_task_graph",
            {
                "goal": "golden local planning",
                "tasks": [
                    {
                        "title": "Plan locally",
                        "execution_adapter": "dry_run",
                        "task_type": "phase_1a_test",
                    },
                    {
                        "title": "Review local plan",
                        "execution_adapter": "dry_run",
                        "task_type": "phase_1a_test",
                        "depends_on_indexes": [0],
                    },
                ],
            },
        )
    )

    response = handle_chat_input("create a local autonomous plan", tmp_path, state, chat_model=model)
    graph = _json_result(["tasks", "graph", "--project", str(tmp_path), "--output", "json"])

    assert response["kind"] == "action_contract_executed"
    assert response["autonomy_decision"]["status"] == "auto_allowed"
    assert response["objective"]["id"] == graph["objectives"][0]["id"]
    assert len(graph["tasks"]) == 2
    assert [
        {
            "task_id": dependency["downstream_task_id"],
            "depends_on_task_id": dependency["upstream_task_id"],
        }
        for dependency in graph["dependencies"]
    ] == [{"task_id": graph["tasks"][1]["id"], "depends_on_task_id": graph["tasks"][0]["id"]}]
    decisions = [
        json.loads(line)
        for line in (tmp_path / ".harness" / "autonomy" / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert decisions[0]["tool_name"] == "create_task_graph"
    assert decisions[0]["status"] == "auto_allowed"


def test_golden_supervised_codex_autonomous_repo_planning_manifest(tmp_path, monkeypatch) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    objective = _create_objective(tmp_path, "Golden repo planning")
    task = _create_task(
        tmp_path,
        objective["id"],
        title="Plan implementation",
        adapter="repo_planning",
        task_type="repo_planning",
    )
    _add_scoped_codex_approval(tmp_path, objective["id"], adapter="repo_planning", task_type="repo_planning")
    monkeypatch.setattr(
        "harness.execution.CodexCliBackend",
        lambda config: GoldenCodexBackend(config, final_message="implementation plan evidence"),
    )

    result = _json_result(
        [
            "objectives",
            "run",
            objective["id"],
            "--autonomy",
            "supervised-codex",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ]
    )

    assert result["stop_reason"] == "objective_succeeded"
    assert result["step_results"][0]["adapter_id"] == "repo_planning"
    manifest = SQLiteStore(tmp_path).build_run_manifest(result["step_results"][0]["run_id"]).model_dump(mode="json")
    assert manifest["run_mode"] == "planning"
    assert manifest["task_id"] == task["id"]
    assert manifest["approval_id"].startswith("appr_")
    assert manifest["autonomous_approval_id"].startswith("auto_")
    assert manifest["autonomous_outcome_id"].startswith("aout_")
    assert manifest["effective_policy_sha256"]


def test_golden_supervised_codex_autonomous_isolated_edit_diff_without_active_mutation(tmp_path, monkeypatch) -> None:
    _init_git_project(tmp_path)
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "harness ignore"], cwd=tmp_path, check=True, capture_output=True)
    before = (tmp_path / "app.py").read_bytes()
    objective = _create_objective(tmp_path, "Golden isolated edit")
    task = _create_task(
        tmp_path,
        objective["id"],
        title="Prepare isolated edit",
        adapter="codex_isolated_edit",
        task_type="codex_code_edit",
    )
    _add_scoped_codex_approval(tmp_path, objective["id"], adapter="codex_isolated_edit", task_type="codex_code_edit")
    monkeypatch.setattr(
        "harness.execution.CodexCliBackend",
        lambda config: GoldenCodexBackend(config, final_message="isolated edit evidence", edit_text="value = 2\n"),
    )

    result = _json_result(
        [
            "objectives",
            "run",
            objective["id"],
            "--autonomy",
            "supervised-codex",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ]
    )

    assert result["stop_reason"] == "objective_succeeded"
    assert (tmp_path / "app.py").read_bytes() == before
    manifest = SQLiteStore(tmp_path).build_run_manifest(result["step_results"][0]["run_id"]).model_dump(mode="json")
    assert manifest["task_id"] == task["id"]
    assert manifest["status"] == "completed_denied"
    assert any("diff" in artifact["kind"] for artifact in manifest["artifacts"])
    events = SQLiteStore(tmp_path).list_events(result["step_results"][0]["run_id"])
    assert any(event.event_type == "apply_back_decision" and event.payload["decision"] == "denied" for event in events)


def test_golden_kill_switch_open_pauses_autonomous_objective_without_run(tmp_path) -> None:
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    objective = _create_objective(tmp_path, "Golden kill switch")
    task = _create_task(
        tmp_path,
        objective["id"],
        title="Blocked dry run",
        adapter="dry_run",
        task_type="phase_1a_test",
    )
    disabled = _json_result(
        [
            "controls",
            "disable",
            "--target-kind",
            "adapter",
            "--target-id",
            "dry_run",
            "--reason",
            "golden kill switch",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ]
    )

    result = _json_result(
        [
            "objectives",
            "run",
            objective["id"],
            "--autonomy",
            "safe-local",
            "--project",
            str(tmp_path),
            "--output",
            "json",
        ]
    )

    assert disabled["control"]["disabled"] is True
    assert result["ok"] is False
    assert result["stop_reason"] == "denied"
    assert result["adapter_dispatches"] == 0
    assert result["pause_reasons"][0]["task_id"] == task["id"]
    assert "kill switch" in " ".join(result["pause_reasons"][0]["reasons"])
    assert SQLiteStore(tmp_path).list_runs() == []


def test_golden_act_runs_full_supervised_codex_workflow_without_apply_back(tmp_path, monkeypatch) -> None:
    _init_git_project(tmp_path)
    assert runner.invoke(app, ["init", "--project", str(tmp_path)]).exit_code == 0
    subprocess.run(["git", "add", ".gitignore"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "harness ignore"], cwd=tmp_path, check=True, capture_output=True)
    before = (tmp_path / "app.py").read_bytes()
    ApprovalStore(tmp_path).add(
        backend="codex_cli",
        data_boundary="hosted_provider",
        task_types=["repo_planning"],
        duration_hours=8,
        allowed_adapters=["repo_planning"],
        autonomy_scope="supervised-codex",
    )
    ApprovalStore(tmp_path).add(
        backend="codex_cli",
        data_boundary="hosted_provider",
        task_types=["codex_code_edit"],
        duration_hours=8,
        allowed_adapters=["codex_isolated_edit"],
        autonomy_scope="supervised-codex",
    )

    class FakeChatModel:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, _messages, _context):
            self.calls += 1
            if self.calls == 1:
                return ChatResponse(
                    content=_request(
                        "create_task_graph",
                        {"goal": "fix the next missing autonomy feature", "template_id": "coding_fix"},
                    )
                )
            return ChatResponse(content="Completed supervised Codex workflow and stopped before apply-back.")

    monkeypatch.setattr("harness.chat.build_default_chat_model", lambda _project_root: FakeChatModel())
    monkeypatch.setattr(
        "harness.execution.CodexCliBackend",
        lambda config: GoldenCodexBackend(config, final_message="supervised workflow evidence", edit_text="value = 2\n"),
    )

    result = _json_result(
        [
            "act",
            "inspect this repo, identify the next missing autonomy feature, implement it in isolation, run the safe tests, and produce a review report",
            "--project",
            str(tmp_path),
            "--autonomy",
            "supervised-codex",
            "--output",
            "json",
        ]
    )

    assert result["ok"] is True
    assert result["stop_reason"] == "final_answer"
    assert [item["tool"] for item in result["tool_results"]] == [
        "create_task_graph",
        "create_task_graph",
        "objectives.run",
    ]
    assert result["tool_results"][2]["stop_reason"] == "objective_succeeded"
    assert (tmp_path / "app.py").read_bytes() == before
    store = SQLiteStore(tmp_path)
    tasks = store.list_tasks()
    assert [task.metadata["execution_adapter"] for task in tasks] == [
        "repo_planning",
        "codex_isolated_edit",
        "dry_run",
        "dry_run",
        "dry_run",
        "dry_run",
    ]
    assert all(task.status.value == "succeeded" for task in tasks)
    assert [task.agent_id for task in tasks][3:5] == ["implementation_reviewer", "security_reviewer"]
    runs = store.list_runs()
    assert len(runs) == 6
    edit_run = next(run for run in runs if run.task_type == "codex_code_edit")
    assert edit_run.status == "completed_denied"
    assert any(
        event.event_type == "apply_back_decision" and event.payload["decision"] == "denied"
        for event in store.list_events(edit_run.id)
    )
    final_run_id = result["tool_results"][2]["run_id"] if "run_id" in result["tool_results"][2] else runs[-1].id
    final_manifest = store.build_run_manifest(final_run_id)
    assert final_manifest.effective_policy_sha256
