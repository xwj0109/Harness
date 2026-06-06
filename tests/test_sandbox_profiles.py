import json

from typer.testing import CliRunner

import harness.execution as execution_module
from harness.cli.main import app
from harness.execution import execute_lease, list_execution_adapter_descriptors, validate_execution_task_payload
from harness.memory.sqlite_store import SQLiteStore
from harness.models import SandboxNetworkPolicy
from harness.sandbox_profiles import get_sandbox_profile, list_sandbox_profiles


runner = CliRunner()


def _patch_dry_run_network_allowed_budget(monkeypatch):
    adapters = execution_module.builtin_execution_adapters()
    dry_run = adapters["dry_run"]
    bad_budget = dry_run.descriptor.delegate_budget.model_copy(update={"network_policy": SandboxNetworkPolicy.ALLOWED})
    bad_descriptor = dry_run.descriptor.model_copy(update={"delegate_budget": bad_budget})

    class BadBudgetAdapter:
        id = dry_run.id
        descriptor = bad_descriptor

        def inspect_eligibility(self, project_root, lease, task, attempt):
            return execution_module._base_adapter_eligibility(self.descriptor, lease, task, attempt)

        def execute(self, project_root, lease_id, owner="local_daemon"):
            raise AssertionError("invalid delegate budget descriptor must fail before adapter execution")

    monkeypatch.setattr(
        execution_module,
        "builtin_execution_adapters",
        lambda: {**adapters, "dry_run": BadBudgetAdapter()},
    )


def test_builtin_sandbox_profiles_are_stable_and_unique() -> None:
    profiles = list_sandbox_profiles()
    ids = [profile.id for profile in profiles]

    assert ids == ["none", "read_only_codex", "isolated_workspace_codex", "docker_test_sandbox"]
    assert len(ids) == len(set(ids))
    assert all(profile.schema_version == "harness.sandbox_profile/v1" for profile in profiles)
    assert get_sandbox_profile("none").tier.value == "none"


def test_registered_adapters_have_valid_sandbox_profiles() -> None:
    profiles = {profile.id for profile in list_sandbox_profiles()}
    by_id = {descriptor.id: descriptor for descriptor in list_execution_adapter_descriptors()}

    assert all(descriptor.sandbox_profile_id in profiles for descriptor in by_id.values())
    assert by_id["dry_run"].sandbox_profile_id == "none"
    assert by_id["read_only_summary"].sandbox_profile_id == "read_only_codex"
    assert by_id["repo_planning"].sandbox_profile_id == "read_only_codex"
    assert by_id["codex_isolated_edit"].sandbox_profile_id == "isolated_workspace_codex"


def test_registered_adapter_run_manifest_uses_descriptor_sandbox_profile(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    objective = store.create_objective("Manifest profile objective")
    upstream = store.create_task(
        title="Upstream evidence",
        objective_id=objective.id,
        metadata={
            "execution_adapter": "dry_run",
            "task_type": "phase_1a_test",
            "workflow_stage": "test_sandbox",
        },
    )
    review = store.create_task(
        title="Implementation review",
        objective_id=objective.id,
        agent_id="implementation_reviewer",
        depends_on=[upstream.id],
        metadata={
            "execution_adapter": "review_gate",
            "task_type": "implementation_review",
            "workflow_stage": "implementation_review",
            "review_role": "implementation_reviewer",
            "review_gate": True,
            "completion_gate": True,
            "review_target_stage": "test_sandbox",
        },
    )

    run = store.create_run(
        goal="Review manifest profile",
        task_type="implementation_review",
        task_id=review.id,
        objective_id=objective.id,
    )
    manifest = store.build_run_manifest(run.id)

    assert manifest.sandbox_profile is not None
    assert manifest.sandbox_profile["schema_version"] == "harness.sandbox_profile/v1"
    assert manifest.sandbox_profile["id"] == "none"
    assert manifest.delegate_budget is not None
    assert manifest.delegate_budget["adapter_id"] == "review_gate"
    assert manifest.delegate_budget["budget"]["schema_version"] == "harness.delegate_budget/v1"
    assert manifest.delegate_budget["budget"]["network_policy"] == "forbidden"
    assert manifest.delegate_budget["budget_limited"] is True
    assert manifest.delegate_budget["gaps"] == []


def test_registered_adapter_execution_denies_unresolvable_sandbox_profile(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    store.create_task(
        title="Sandbox profile guard",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None

    def missing_profile(profile_id: str):
        raise KeyError(f"Sandbox profile not found: {profile_id}")

    monkeypatch.setattr("harness.execution.get_sandbox_profile", missing_profile)

    result = execute_lease(tmp_path, leased.lease.id, owner=leased.lease.owner)

    assert result.ok is False
    assert result.security_decision is not None
    assert result.security_decision.decision.value == "deny"
    assert result.security_decision.reason_code == "sandbox_profile_mismatch"
    assert result.security_decision.sandbox_profile_id == "none"
    assert result.blocked_state_explanations[0].code.value == "sandbox_profile_mismatch"
    assert "unknown sandbox_profile_id=none" in result.security_decision.reasons[0]


def test_registered_task_creation_denies_invalid_delegate_budget_descriptor(tmp_path, monkeypatch) -> None:
    _patch_dry_run_network_allowed_budget(monkeypatch)

    reasons = validate_execution_task_payload(
        execution_adapter="dry_run",
        task_type="phase_1a_test",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )

    assert any("delegate_budget network_policy=allowed" in reason for reason in reasons)

    store = SQLiteStore(tmp_path)
    store.initialize()
    try:
        store.create_task(
            title="Invalid descriptor task",
            metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
        )
    except ValueError as exc:
        assert "delegate_budget network_policy=allowed" in str(exc)
    else:
        raise AssertionError("invalid delegate budget descriptor must fail before task creation")

    assert store.list_tasks() == []


def test_registered_adapter_execution_denies_invalid_delegate_budget_descriptor(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    store.create_task(
        title="Delegate budget descriptor guard",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None

    _patch_dry_run_network_allowed_budget(monkeypatch)

    result = execute_lease(tmp_path, leased.lease.id, owner=leased.lease.owner)

    assert result.ok is False
    assert result.run is None
    assert result.manifest is None
    assert result.security_decision is not None
    assert result.security_decision.decision.value == "deny"
    assert result.security_decision.reason_code == "delegate_budget_mismatch"
    assert result.security_decision.sandbox_profile_id == "none"
    assert result.blocked_state_explanations[0].code.value == "unsafe_metadata"
    assert any("delegate_budget network_policy=allowed" in reason for reason in result.security_decision.reasons)


def test_sandbox_profile_cli_is_read_only_without_init(tmp_path, monkeypatch) -> None:
    def fail_backend(*_args, **_kwargs):
        raise AssertionError("sandbox profile commands must not preflight backends or Docker")

    monkeypatch.setattr("harness.cli.main.CodexCliBackend", fail_backend)
    monkeypatch.setattr("harness.cli.main.LocalOpenAICompatibleBackend", fail_backend)
    monkeypatch.setattr("harness.cli.main.DockerImageManager", fail_backend)

    listed = runner.invoke(app, ["sandbox", "profiles", "--project", str(tmp_path), "--output", "json"])
    inspected = runner.invoke(app, ["sandbox", "inspect", "none", "--project", str(tmp_path), "--output", "json"])
    missing = runner.invoke(app, ["sandbox", "inspect", "missing", "--project", str(tmp_path), "--output", "json"])

    assert listed.exit_code == 0, listed.output
    listed_payload = json.loads(listed.output)
    assert listed_payload["schema_version"] == "harness.sandbox_profiles/v1"
    assert {profile["id"] for profile in listed_payload["profiles"]} == {
        "none",
        "read_only_codex",
        "isolated_workspace_codex",
        "docker_test_sandbox",
    }
    assert inspected.exit_code == 0, inspected.output
    inspected_payload = json.loads(inspected.output)
    assert inspected_payload["schema_version"] == "harness.sandbox_profile/v1"
    assert inspected_payload["id"] == "none"
    assert inspected_payload["tier"] == "none"
    assert missing.exit_code == 1
    missing_payload = json.loads(missing.output)
    assert missing_payload["schema_version"] == "harness.sandbox_profile/v1"
    assert missing_payload["ok"] is False
    assert missing_payload["errors"] == ["Sandbox profile not found: missing"]
    assert not (tmp_path / ".harness").exists()
