import json
import re
from copy import deepcopy

from typer.testing import CliRunner

from harness.cli.main import app
from harness.models import BackendStatus


runner = CliRunner()


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
    assert normalized_manifest["schema_version"] == "harness.manifest/v1"
    assert normalized_manifest["run_id"] == "<RUN_ID>"
    assert normalized_manifest["run_mode"] == "dev"
    assert normalized_manifest["status"] == "completed"
    assert normalized_manifest["backend_descriptor"] is None
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
            "backend": "codex_cli",
            "created_at": "<TIMESTAMP>",
            "data_boundary": "hosted_provider",
            "expires_at": "<TIMESTAMP>",
            "id": "<APPROVAL_ID>",
            "project_root": "<PROJECT_ROOT>",
            "reason": None,
            "revoked": True,
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
