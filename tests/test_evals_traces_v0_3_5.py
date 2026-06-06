from datetime import datetime
import json

from typer.testing import CliRunner

from harness.cli.main import app
from harness.config import default_config
from harness.evals import run_safety_smoke, run_security_check, run_security_layer_audit
from harness.execution import execute_lease, list_execution_adapter_descriptors
from harness.integrity import check_builtin_spec_integrity, check_workflow_template_integrity, run_integrity_check
from harness.memory.sqlite_store import SQLiteStore
from harness.models import KillSwitchTargetKind, TraceExport, TraceSpan
import harness.objective_runner as objective_runner_module
from harness.objective_runner import run_objective_autonomously
from harness.registry import builtin_spec_registry
from harness.specs import ToolPermission, ToolPolicy
from harness.traces import export_objective_trace, export_run_trace, to_otel_json

runner = CliRunner()


def test_safety_smoke_passes_on_clean_initialized_project(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="trace run", task_type="phase_1a_test")
    store.append_event(run.id, "info", "unit_event", "Unit event.", {"value": 1})

    result = run_safety_smoke(tmp_path, default_config(), store)

    assert result.schema_version == "harness.evals.safety_smoke/v1"
    assert result.ok is True
    assert {check.id for check in result.checks} == {
        "sandbox_network_disabled",
        "backend_boundaries",
        "artifact_evidence",
        "task_queue_non_execution",
        "manifest_policy_evidence",
    }
    serialized = json.dumps(result.model_dump(mode="json"))
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized


def test_safety_smoke_fails_on_artifact_drift(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="drift run", task_type="phase_1a_test")
    artifact_path = tmp_path / ".harness" / "runs" / run.id / "evidence.txt"
    artifact_path.write_text("initial", encoding="utf-8")
    artifact = store.register_artifact(run.id, "evidence", artifact_path)
    artifact_path.write_text("changed", encoding="utf-8")

    result = run_safety_smoke(tmp_path, default_config(), store)
    artifact_check = next(check for check in result.checks if check.id == "artifact_evidence")

    assert result.ok is False
    assert artifact_check.status == "fail"
    assert artifact.id in artifact_check.message
    assert "changed" not in json.dumps(result.model_dump(mode="json"))


def test_trace_export_links_run_event_artifact_policy_and_backend_metadata(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(
        goal="trace run",
        task_type="read_only_repo_summary",
        backend=default_config().backends["local_openai_compatible"],
    )
    store.append_event(
        run.id,
        "info",
        "trace_event",
        "Trace event.",
        {"api_key": "sk-event-secret-abcdefghijklmnop", "safe": "visible"},
    )
    artifact_path = tmp_path / ".harness" / "runs" / run.id / "trace.txt"
    artifact_path.write_text("trace artifact body", encoding="utf-8")
    artifact = store.register_artifact(run.id, "trace_artifact", artifact_path)

    export = export_run_trace(tmp_path, store, run.id)
    payload = to_otel_json(export)
    serialized = json.dumps(payload)
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    span_names = {span["name"] for span in spans}

    assert payload["schema_version"] == "harness.trace_export/v1"
    assert payload["ok"] is True
    assert payload["format"] == "otel-json"
    assert "opentelemetry.semconv.gen_ai" in payload["semantic_conventions"]
    assert "opentelemetry.semconv.gen_ai.mcp" in payload["semantic_conventions"]
    assert payload["trace_context"]["w3c_trace_context"] is True
    assert payload["trace_context"]["external_protocol_propagation_required"] is True
    assert payload["trace_context"]["sensitive_bodies_included"] is False
    assert payload["run_id"] == run.id
    assert len(payload["trace_id"]) == 32
    assert "harness.run" in span_names
    assert "harness.policy" in span_names
    assert "harness.backend" in span_names
    assert "harness.sandbox" in span_names
    assert "harness.event.trace_event" in span_names
    assert "harness.artifact.trace_artifact" in span_names
    assert "harness.context.run_goal" in span_names
    assert "harness.context.artifact" in span_names
    event_span = next(span for span in spans if span["name"] == "harness.event.trace_event")
    event_attributes = {attribute["key"]: attribute["value"] for attribute in event_span["attributes"]}
    assert event_attributes["event.payload"]["safe"] == "visible"
    assert event_attributes["event.payload"]["[REDACTED_KEY]"] == "[REDACTED_SECRET]"
    assert len(event_attributes["event.payload_sha256"]) == 64
    assert event_attributes["event.payload_size_bytes"] > 0
    assert event_attributes["event.payload_keys"] == ["[REDACTED_KEY]", "safe"]
    assert event_attributes["event.redaction_state"] == "redacted"
    sandbox_span = next(span for span in spans if span["name"] == "harness.sandbox")
    sandbox_attributes = {attribute["key"]: attribute["value"] for attribute in sandbox_span["attributes"]}
    assert sandbox_attributes["sandbox.profile_id"] == "read_only_codex"
    assert sandbox_attributes["sandbox.tier"] == "read_only"
    run_span = next(span for span in spans if span["name"] == "harness.run")
    run_attributes = {attribute["key"]: attribute["value"] for attribute in run_span["attributes"]}
    assert run_attributes["gen_ai.operation.name"] == "invoke_agent"
    assert run_attributes["gen_ai.system"] == "harness"
    assert run_attributes["gen_ai.agent.id"] == "read_only_repo_summary"
    assert run_attributes["gen_ai.agent.name"] == "read_only_repo_summary"
    assert "opentelemetry.semconv.gen_ai.agent" in run_attributes["harness.trace.semantic_conventions"]
    assert run_attributes["harness.trace.w3c_trace_context"] is True
    assert run_attributes["harness.trace.external_protocol_propagation_required"] is True
    assert "artifact_content_not_authority" in run_attributes["context.warning_codes"]
    assert run_attributes["trace.provenance_id"].startswith("artprov_")
    assert run_attributes["trace.producer"] == "harness.trace_export"
    context_span = next(span for span in spans if span["name"] == "harness.context.artifact")
    context_attributes = {attribute["key"]: attribute["value"] for attribute in context_span["attributes"]}
    assert context_attributes["context.trust_level"] == "artifact"
    assert context_attributes["context.redaction_state"] == "not_required"
    assert artifact.sha256 in serialized
    assert "trace artifact body" not in serialized
    assert "sk-secret" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "api_key" not in serialized
    assert "base_url" not in serialized


def test_trace_export_includes_registered_delegate_budget_queue_and_lease_timing(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    task = store.create_task(
        title="Trace registered dry run",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None
    result = execute_lease(tmp_path, leased.lease.id, owner=leased.lease.owner)
    assert result.ok is True
    assert result.run is not None

    export = export_run_trace(tmp_path, store, result.run.id)
    payload = to_otel_json(export)
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    span_names = {span["name"] for span in spans}

    assert "harness.delegate_budget" in span_names
    assert "harness.queue" in span_names
    assert "harness.lease" in span_names

    budget_span = next(span for span in spans if span["name"] == "harness.delegate_budget")
    budget_attributes = {attribute["key"]: attribute["value"] for attribute in budget_span["attributes"]}
    assert budget_attributes["delegate_budget.adapter_id"] == "dry_run"
    assert budget_attributes["delegate_budget.schema_version"] == "harness.delegate_budget/v1"
    assert budget_attributes["delegate_budget.network_policy"] == "forbidden"
    assert budget_attributes["delegate_budget.filesystem_scope"] == "harness_artifacts"
    assert budget_attributes["delegate_budget.max_runtime_invocations"] == 0
    assert budget_attributes["delegate_budget.gap_count"] == 0
    assert budget_attributes["delegate_budget.limited"] is True

    queue_span = next(span for span in spans if span["name"] == "harness.queue")
    queue_attributes = {attribute["key"]: attribute["value"] for attribute in queue_span["attributes"]}
    assert queue_attributes["task.id"] == task.id
    assert queue_attributes["lease.id"] == leased.lease.id
    assert queue_attributes["queue.wait_ms"] >= 0

    lease_span = next(span for span in spans if span["name"] == "harness.lease")
    lease_attributes = {attribute["key"]: attribute["value"] for attribute in lease_span["attributes"]}
    assert lease_attributes["lease.id"] == leased.lease.id
    assert lease_attributes["lease.status"] == "released"
    assert lease_attributes["attempt.run_id"] == result.run.id
    assert lease_attributes["lease.ttl_ms"] > 0
    assert lease_attributes["lease.runtime_ms"] >= 0

    run_span = next(span for span in spans if span["name"] == "harness.run")
    run_attributes = {attribute["key"]: attribute["value"] for attribute in run_span["attributes"]}
    assert run_attributes["delegate_budget.schema_version"] == "harness.delegate_budget/v1"
    assert run_attributes["delegate_budget.gap_count"] == 0
    assert run_attributes["lease.id"] == leased.lease.id


def test_objective_trace_export_links_objective_events_and_verification(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    objective = store.create_objective("Trace objective")
    task = store.create_task(
        title="Trace objective task",
        objective_id=objective.id,
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    assert result.ok is True

    export = export_objective_trace(tmp_path, SQLiteStore(tmp_path), objective.id)
    payload = to_otel_json(export)
    serialized = json.dumps(payload)
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    span_names = {span["name"] for span in spans}

    assert payload["schema_version"] == "harness.trace_export/v1"
    assert payload["ok"] is True
    assert payload["format"] == "otel-json"
    assert "opentelemetry.semconv.gen_ai.agent" in payload["semantic_conventions"]
    assert payload["trace_context"]["w3c_trace_context"] is True
    assert payload["objective_id"] == objective.id
    assert payload["objective_run_ids"]
    assert len(payload["trace_id"]) == 32
    assert "harness.objective" in span_names
    assert "harness.objective_run" in span_names
    assert "harness.objective_event.started" in span_names
    assert "harness.objective_event.adapter_dispatched" in span_names
    root_span = next(span for span in spans if span["name"] == "harness.objective")
    root_attributes = {attribute["key"]: attribute["value"] for attribute in root_span["attributes"]}
    assert root_attributes["objective.id"] == objective.id
    assert root_attributes["gen_ai.operation.name"] == "invoke_agent"
    assert root_attributes["gen_ai.system"] == "harness"
    assert root_attributes["gen_ai.agent.id"] == "harness.objective_runner"
    assert root_attributes["gen_ai.agent.name"] == "harness.objective"
    assert root_attributes["gen_ai.conversation.id"] == objective.id
    assert root_attributes["workflow.id"] == objective.id
    assert root_attributes["workflow.name"] == "Trace objective"
    assert root_attributes["objective.evidence_verification_ok"] is True
    assert root_attributes["trace.producer"] == "harness.trace_export"
    objective_events = [json.loads(line) for line in result.evidence_path.read_text(encoding="utf-8").splitlines()]
    assert root_attributes["objective.evidence_event_count"] == len(objective_events)
    assert root_attributes["objective.evidence_hash_chain_ok"] is True
    assert root_attributes["objective.evidence_head_sha256"] == objective_events[-1]["event_sha256"]
    started_event = next(event for event in objective_events if event["event"] == "started")
    started_span = next(span for span in export.spans if span.name == "harness.objective_event.started")
    assert started_span.start_time == datetime.fromisoformat(started_event["created_at"])
    dispatch_span = next(span for span in spans if span["name"] == "harness.objective_event.adapter_dispatched")
    dispatch_attributes = {attribute["key"]: attribute["value"] for attribute in dispatch_span["attributes"]}
    assert dispatch_attributes["objective_event.payload"]["task_id"] == task.id
    assert dispatch_attributes["objective_event.payload"]["artifact_ids"]
    assert dispatch_attributes["objective_event.id"] == dispatch_attributes["objective_event.payload"]["objective_event_id"]
    assert dispatch_attributes["objective_event.index"] == dispatch_attributes["objective_event.payload"]["event_index"]
    assert len(dispatch_attributes["objective_event.payload_sha256"]) == 64
    assert dispatch_attributes["objective_event.payload_size_bytes"] > 0
    assert "objective_event_id" in dispatch_attributes["objective_event.payload_keys"]
    assert result.step_results[0].run_id in serialized
    assert "api_key" not in serialized
    assert "OPENAI_API_KEY" not in serialized
    assert "base_url" not in serialized


def test_objective_trace_export_includes_lease_guard_stopped_event(monkeypatch, tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    objective = store.create_objective("Guarded trace objective")
    task = store.create_task(
        title="Guarded trace task",
        objective_id=objective.id,
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    store.disable_execution_control(
        KillSwitchTargetKind.ADAPTER,
        "dry_run",
        reason="operator pause",
        actor="test",
    )
    monkeypatch.setattr(objective_runner_module, "_kill_switch_active", lambda *_args, **_kwargs: False)
    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")

    export = export_objective_trace(tmp_path, SQLiteStore(tmp_path), objective.id)
    payload = to_otel_json(export)
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    span_names = {span["name"] for span in spans}
    guard_span = next(span for span in spans if span["name"] == "harness.objective_event.lease_guard_stopped")
    guard_attributes = {attribute["key"]: attribute["value"] for attribute in guard_span["attributes"]}

    assert result.ok is False
    assert result.stop_reason == "control_disabled"
    assert payload["ok"] is True
    assert "harness.objective_event.lease_guard_stopped" in span_names
    assert "harness.objective_event.adapter_dispatched" not in span_names
    assert guard_attributes["objective_event.payload"]["task_id"] == task.id
    assert guard_attributes["objective_event.payload"]["lease_id"] is None
    assert guard_attributes["objective_event.payload"]["stop_reason"] == "control_disabled"
    assert guard_attributes["objective_event.payload"]["guard_pause_reasons"][0]["decision"] == "control_disabled"
    assert len(guard_attributes["objective_event.payload_sha256"]) == 64


def test_objective_trace_export_marks_not_ok_when_evidence_verification_fails(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    objective = store.create_objective("Tampered trace objective")
    store.create_task(
        title="Tampered trace task",
        objective_id=objective.id,
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    assert result.ok is True
    lines = []
    for raw_line in result.evidence_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw_line)
        if event["event"] == "adapter_dispatched":
            event["artifact_ids"] = ["art_missing"]
        lines.append(json.dumps(event, sort_keys=True))
    result.evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    export = export_objective_trace(tmp_path, SQLiteStore(tmp_path), objective.id)
    payload = to_otel_json(export)
    root_span = next(
        span
        for span in payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
        if span["name"] == "harness.objective"
    )
    root_attributes = {attribute["key"]: attribute["value"] for attribute in root_span["attributes"]}

    assert payload["ok"] is False
    assert root_attributes["objective.evidence_verification_ok"] is False
    assert root_attributes["objective.evidence_hash_chain_ok"] is False
    assert root_attributes["objective.evidence_verification_summary"]["fail"] >= 1


def test_objective_trace_cli_text_surfaces_failed_evidence_verification(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    objective = store.create_objective("Tampered trace CLI objective")
    store.create_task(
        title="Tampered trace CLI task",
        objective_id=objective.id,
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    assert result.ok is True
    lines = []
    for raw_line in result.evidence_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw_line)
        if event["event"] == "adapter_dispatched":
            event["artifact_ids"] = ["art_missing"]
        lines.append(json.dumps(event, sort_keys=True))
    result.evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    text = runner.invoke(app, ["traces", "export-objective", objective.id, "--project", str(tmp_path)])
    json_result = runner.invoke(
        app,
        ["traces", "export-objective", objective.id, "--project", str(tmp_path), "--output", "json"],
    )

    assert text.exit_code == 0, text.output
    assert "Evidence verification: fail" in text.output
    assert "Evidence hash chain: fail" in text.output
    assert json_result.exit_code == 0, json_result.output
    assert json.loads(json_result.output)["ok"] is False


def test_integrity_check_passes_uninitialized_project_and_cli_alias(tmp_path) -> None:
    direct = run_integrity_check(tmp_path)
    evals = runner.invoke(app, ["evals", "run", "--suite", "integrity", "--project", str(tmp_path), "--output", "json"])
    alias = runner.invoke(app, ["integrity", "check", "--project", str(tmp_path), "--output", "json"])

    assert direct.schema_version == "harness.integrity_check_result/v1"
    assert direct.ok is True
    assert direct.summary["fail"] == 0
    assert any(check.subject_kind.value == "builtin_spec" for check in direct.checks)
    assert any(check.subject_kind.value == "adapter_descriptor" for check in direct.checks)
    assert any(check.subject_kind.value == "workflow_template" for check in direct.checks)
    assert any(check.subject_kind.value == "tui_static_asset" for check in direct.checks)
    assert evals.exit_code == 0, evals.output
    assert alias.exit_code == 0, alias.output
    assert json.loads(evals.output)["schema_version"] == "harness.integrity_check_result/v1"
    assert json.loads(alias.output)["schema_version"] == "harness.integrity_check_result/v1"
    assert not (tmp_path / ".harness").exists()


def test_integrity_check_fails_closed_on_policy_broadening(monkeypatch) -> None:
    registry = builtin_spec_registry()
    policy_id = next(iter(registry.tool_policies))
    broadened = ToolPolicy.model_construct(
        tools={},
        network=ToolPermission.ALLOWED,
        active_repo_write=ToolPermission.FORBIDDEN,
        hosted_boundary=ToolPermission.APPROVAL_REQUIRED,
    )
    registry = registry.model_copy(update={"tool_policies": {**registry.tool_policies, policy_id: broadened}})
    monkeypatch.setattr("harness.integrity.builtin_spec_registry", lambda: registry)

    checks = check_builtin_spec_integrity()
    invariant = next(check for check in checks if check.subject_id == "registry_security_invariants")

    assert invariant.status.value == "fail"
    assert invariant.subject_kind.value == "builtin_spec"


def test_workflow_template_integrity_fails_closed_on_invalid_template(monkeypatch) -> None:
    def broken_template_for_intent(intent, prompt, project_root):
        raise ValueError(f"broken template: {intent}")

    monkeypatch.setattr("harness.integrity.BUILTIN_WORKFLOW_TEMPLATE_INTENTS", ("broken",))
    monkeypatch.setattr("harness.integrity.template_for_intent", broken_template_for_intent)

    checks = check_workflow_template_integrity()

    assert len(checks) == 1
    assert checks[0].status.value == "fail"
    assert checks[0].subject_kind.value == "workflow_template"
    assert checks[0].subject_id == "broken"
    assert checks[0].metadata["error"] == "broken template: broken"


def test_security_layer_audit_passes_clean_initialized_project_with_dry_run(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    store.create_task(
        title="Audit dry run",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None
    result = execute_lease(tmp_path, leased.lease.id, owner=leased.lease.owner)
    assert result.ok is True

    audit = run_security_layer_audit(tmp_path)
    evals = runner.invoke(app, ["evals", "run", "--suite", "security-layer", "--project", str(tmp_path), "--output", "json"])
    alias = runner.invoke(app, ["security", "audit", "--project", str(tmp_path), "--output", "json"])

    assert audit.schema_version == "harness.security_layer_audit/v1"
    assert audit.ok is True
    assert audit.summary["fail"] == 0
    assert {check.id for check in audit.checks} >= {
        "registered_adapters_have_sandbox_profiles",
        "runtime_manifest_evidence",
        "security_detections_callable",
        "integrity_checks_pass",
    }
    assert evals.exit_code == 0, evals.output
    assert alias.exit_code == 0, alias.output
    assert json.loads(evals.output)["schema_version"] == "harness.security_layer_audit/v1"
    assert json.loads(alias.output)["schema_version"] == "harness.security_layer_audit/v1"


def test_security_layer_audit_verifies_run_trace_payload_metadata(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="audited trace run", task_type="phase_1a_test")
    empty_run = store.create_run(goal="empty audited trace run", task_type="phase_1a_test")
    store.append_event(
        run.id,
        "info",
        "trace_event",
        "Trace event.",
        {"api_key": "sk-event-secret-abcdefghijklmnop", "safe": "visible"},
    )

    audit = run_security_layer_audit(tmp_path)
    check = next(item for item in audit.checks if item.id == "run_trace_payload_metadata")
    run_evidence = next(item for item in check.evidence["runs"] if item["run_id"] == run.id)
    empty_run_evidence = next(item for item in check.evidence["runs"] if item["run_id"] == empty_run.id)
    trace_export = run_evidence["trace_export"]
    empty_trace_export = empty_run_evidence["trace_export"]

    assert audit.ok is True
    assert check.status == "pass"
    assert run_evidence["ok"] is True
    assert run_evidence["event_count"] == 1
    assert trace_export["ok"] is True
    assert trace_export["trace_provenance_id"]
    assert trace_export["trace_output_sha256"]
    assert trace_export["trace_producer"] == "harness.trace_export"
    assert trace_export["run_event_payload_metadata_ok"] is True
    assert trace_export["run_event_payload_metadata"]["span_count"] == 1
    assert trace_export["run_event_payload_metadata"]["missing_payload_metadata"] == []
    assert trace_export["run_event_payload_metadata"]["sensitive_key_leaks"] == []
    assert empty_run_evidence["ok"] is True
    assert empty_run_evidence["event_count"] == 0
    assert empty_trace_export["ok"] is True
    assert empty_trace_export["run_event_payload_metadata"]["required"] is False
    assert empty_trace_export["run_event_payload_metadata"]["span_count"] == 0
    serialized = json.dumps(check.evidence)
    assert "api_key" not in serialized
    assert "sk-event-secret" not in serialized


def test_security_layer_audit_fails_if_run_trace_payload_metadata_is_missing(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="broken audited trace run", task_type="phase_1a_test")
    store.append_event(run.id, "info", "trace_event", "Trace event.", {"safe": "visible"})

    def trace_without_payload_metadata(project_root, store, run_id):
        return TraceExport(
            ok=True,
            run_id=run_id,
            trace_id="1" * 32,
            spans=[
                TraceSpan(
                    trace_id="1" * 32,
                    span_id="2" * 16,
                    name="harness.run",
                    start_time=run.created_at,
                    end_time=run.updated_at,
                    attributes={
                        "trace.provenance_id": "artprov_test",
                        "trace.output_sha256": "b" * 64,
                        "trace.producer": "harness.trace_export",
                    },
                ),
                TraceSpan(
                    trace_id="1" * 32,
                    span_id="3" * 16,
                    parent_span_id="2" * 16,
                    name="harness.event.trace_event",
                    start_time=run.created_at,
                    end_time=run.created_at,
                    attributes={"event.payload": {"safe": "visible"}},
                ),
            ],
        )

    monkeypatch.setattr("harness.evals.export_run_trace", trace_without_payload_metadata)

    audit = run_security_layer_audit(tmp_path)
    check = next(item for item in audit.checks if item.id == "run_trace_payload_metadata")
    trace_export = check.evidence["runs"][0]["trace_export"]

    assert audit.ok is False
    assert check.status == "fail"
    assert trace_export["run_event_payload_metadata_ok"] is False
    assert trace_export["run_event_payload_metadata"]["span_count"] == 1
    assert trace_export["run_event_payload_metadata"]["missing_payload_metadata"][0]["missing"] == [
        "event.payload_sha256",
        "event.payload_size_bytes",
        "event.payload_keys",
    ]


def test_security_layer_audit_verifies_autonomous_objective_evidence(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    objective = store.create_objective("Audited objective")
    store.create_task(
        title="Audited autonomous dry run",
        objective_id=objective.id,
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    assert result.ok is True

    audit = run_security_layer_audit(tmp_path)
    check = next(item for item in audit.checks if item.id == "objective_evidence_verifiable")

    assert audit.ok is True
    assert check.status == "pass"
    assert check.evidence["objectives"][0]["objective_id"] == objective.id
    assert check.evidence["objectives"][0]["ok"] is True
    trace_export = check.evidence["objectives"][0]["trace_export"]
    assert trace_export["ok"] is True
    assert trace_export["objective_run_ids"]
    assert trace_export["span_count"] >= 3
    assert trace_export["objective_evidence_event_count"] >= 4
    assert trace_export["objective_evidence_hash_chain_ok"] is True
    assert trace_export["objective_evidence_head_sha256"]
    assert trace_export["trace_provenance_id"]
    assert trace_export["trace_output_sha256"]
    assert trace_export["trace_producer"] == "harness.trace_export"
    assert trace_export["objective_event_payload_metadata_ok"] is True
    assert trace_export["objective_event_payload_metadata"]["span_count"] >= 4
    assert trace_export["objective_event_payload_metadata"]["missing_payload_metadata"] == []
    assert trace_export["objective_event_payload_metadata"]["sensitive_key_leaks"] == []


def test_security_layer_audit_fails_if_objective_trace_payload_metadata_is_missing(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    objective = store.create_objective("Payload metadata objective")
    store.create_task(
        title="Payload metadata dry run",
        objective_id=objective.id,
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    assert result.ok is True

    def trace_without_payload_metadata(project_root, store, objective_id):
        return TraceExport(
            ok=True,
            objective_id=objective_id,
            objective_run_ids=["orun_missing_payload_metadata"],
            trace_id="1" * 32,
            spans=[
                TraceSpan(
                    trace_id="1" * 32,
                    span_id="2" * 16,
                    name="harness.objective",
                    start_time=objective.created_at,
                    end_time=objective.updated_at,
                    attributes={
                        "objective.evidence_event_count": 1,
                        "objective.evidence_hash_chain_ok": True,
                        "objective.evidence_head_sha256": "a" * 64,
                        "trace.provenance_id": "artprov_test",
                        "trace.output_sha256": "b" * 64,
                        "trace.producer": "harness.trace_export",
                    },
                ),
                TraceSpan(
                    trace_id="1" * 32,
                    span_id="3" * 16,
                    parent_span_id="2" * 16,
                    name="harness.objective_event.started",
                    start_time=objective.created_at,
                    end_time=objective.created_at,
                    attributes={"objective_event.payload": {"event": "started"}},
                ),
            ],
        )

    monkeypatch.setattr("harness.evals.export_objective_trace", trace_without_payload_metadata)

    audit = run_security_layer_audit(tmp_path)
    check = next(item for item in audit.checks if item.id == "objective_evidence_verifiable")
    trace_export = check.evidence["objectives"][0]["trace_export"]

    assert audit.ok is False
    assert check.status == "fail"
    assert trace_export["objective_event_payload_metadata_ok"] is False
    assert trace_export["objective_event_payload_metadata"]["span_count"] == 1
    assert trace_export["objective_event_payload_metadata"]["missing_payload_metadata"][0]["missing"] == [
        "objective_event.payload_sha256",
        "objective_event.payload_size_bytes",
        "objective_event.payload_keys",
    ]


def test_security_layer_audit_fails_if_autonomous_objective_evidence_is_tampered(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    objective = store.create_objective("Tampered objective")
    store.create_task(
        title="Tampered autonomous dry run",
        objective_id=objective.id,
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    assert result.ok is True
    lines = []
    for raw_line in result.evidence_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(raw_line)
        if event["event"] == "adapter_dispatched":
            event["artifact_ids"] = ["art_missing"]
        lines.append(json.dumps(event, sort_keys=True))
    result.evidence_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    audit = run_security_layer_audit(tmp_path)
    check = next(item for item in audit.checks if item.id == "objective_evidence_verifiable")

    assert audit.ok is False
    assert check.status == "fail"
    failed = {item["id"]: item for item in check.evidence["objectives"][0]["failed_checks"]}
    assert failed["event_hash_chain"]["evidence"]["issues"][0]["reason"] == "event_sha256_mismatch"
    assert failed["dispatch_links"]["evidence"]["issues"][0]["reason"] == "artifact_missing"
    assert check.evidence["objectives"][0]["trace_export"]["ok"] is False
    assert check.evidence["objectives"][0]["trace_export"]["objective_evidence_hash_chain_ok"] is False


def test_security_layer_audit_fails_if_objective_trace_export_cannot_parse_evidence(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    objective = store.create_objective("Malformed objective")
    store.create_task(
        title="Malformed autonomous dry run",
        objective_id=objective.id,
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    result = run_objective_autonomously(tmp_path, objective.id, autonomy_profile_id="safe-local")
    assert result.ok is True
    result.evidence_path.write_text('{"event": "started"}\nnot json\n', encoding="utf-8")

    audit = run_security_layer_audit(tmp_path)
    check = next(item for item in audit.checks if item.id == "objective_evidence_verifiable")

    assert audit.ok is False
    assert check.status == "fail"
    objective_evidence = check.evidence["objectives"][0]
    assert objective_evidence["trace_export"]["ok"] is False
    assert "ValueError: Objective evidence is malformed" in objective_evidence["trace_export"]["error"]
    assert any(item["id"] == "jsonl_parse" for item in objective_evidence["failed_checks"])


def test_security_layer_audit_uninitialized_project_is_read_only(tmp_path) -> None:
    audit = run_security_layer_audit(tmp_path)
    result = runner.invoke(app, ["evals", "run", "--suite", "security-layer", "--project", str(tmp_path), "--output", "json"])

    assert audit.ok is True
    assert audit.summary["skipped"] >= 1
    assert result.exit_code == 0, result.output
    assert not (tmp_path / ".harness").exists()


def test_security_layer_audit_fails_if_adapter_profile_missing(tmp_path, monkeypatch) -> None:
    descriptors = list_execution_adapter_descriptors()
    broken = descriptors[0].model_copy(update={"sandbox_profile_id": None})
    monkeypatch.setattr("harness.evals.list_execution_adapter_descriptors", lambda: [broken, *descriptors[1:]])

    audit = run_security_layer_audit(tmp_path)
    check = next(item for item in audit.checks if item.id == "registered_adapters_have_sandbox_profiles")

    assert audit.ok is False
    assert check.status == "fail"


def test_security_layer_audit_fails_if_registered_manifest_lacks_sandbox_or_provenance(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    store.create_task(
        title="Audit dry run",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None
    assert execute_lease(tmp_path, leased.lease.id, owner=leased.lease.owner).ok is True
    original = SQLiteStore.build_run_manifest

    def broken_manifest(self, run_id):
        manifest = original(self, run_id)
        artifacts = [artifact.model_copy(update={"provenance": None}) for artifact in manifest.artifacts]
        return manifest.model_copy(update={"sandbox_profile": None, "artifacts": artifacts})

    monkeypatch.setattr(SQLiteStore, "build_run_manifest", broken_manifest)

    audit = run_security_layer_audit(tmp_path)
    check = next(item for item in audit.checks if item.id == "runtime_manifest_evidence")

    assert audit.ok is False
    assert check.status == "fail"


def test_security_layer_audit_fails_if_registered_manifest_lacks_delegate_budget(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    store.create_task(
        title="Audit dry run budget",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None
    assert execute_lease(tmp_path, leased.lease.id, owner=leased.lease.owner).ok is True
    original = SQLiteStore.build_run_manifest

    def broken_manifest(self, run_id):
        manifest = original(self, run_id)
        return manifest.model_copy(update={"delegate_budget": None})

    monkeypatch.setattr(SQLiteStore, "build_run_manifest", broken_manifest)

    audit = run_security_layer_audit(tmp_path)
    check = next(item for item in audit.checks if item.id == "runtime_manifest_evidence")

    assert audit.ok is False
    assert check.status == "fail"
    assert "missing delegate budget evidence" in check.message


def test_security_layer_audit_fails_if_registered_trace_lacks_delegate_budget(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    store.create_task(
        title="Audit dry run trace budget",
        metadata={"execution_adapter": "dry_run", "task_type": "phase_1a_test"},
    )
    leased = store.daemon_run_once("local_daemon:test:123", pid=123)
    assert leased.lease is not None
    assert execute_lease(tmp_path, leased.lease.id, owner=leased.lease.owner).ok is True
    original = export_run_trace

    def trace_without_delegate_budget(project_root, store, run_id):
        export = original(project_root, store, run_id)
        return export.model_copy(
            update={"spans": [span for span in export.spans if span.name != "harness.delegate_budget"]}
        )

    monkeypatch.setattr("harness.evals.export_run_trace", trace_without_delegate_budget)

    audit = run_security_layer_audit(tmp_path)
    check = next(item for item in audit.checks if item.id == "run_trace_payload_metadata")

    assert audit.ok is False
    assert check.status == "fail"
    assert "trace_delegate_budget" in check.message


def test_security_check_passes_clean_project_and_cli_alias_matches(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    run = store.create_run(goal="trace run", task_type="phase_1a_test")
    paths = store.initialize_run_artifacts(run.id)
    store.register_artifact(run.id, "events", paths["events"])

    direct = run_security_check(tmp_path, store)
    evals = runner.invoke(app, ["evals", "run", "--suite", "security", "--project", str(tmp_path), "--output", "json"])
    alias = runner.invoke(app, ["security", "check", "--project", str(tmp_path), "--output", "json"])

    assert direct.schema_version == "harness.security_check/v1"
    assert direct.ok is True
    assert direct.findings == []
    assert evals.exit_code == 0, evals.output
    assert alias.exit_code == 0, alias.output
    assert json.loads(evals.output) == json.loads(alias.output)


def test_security_check_detects_synthetic_unsafe_metadata_without_echoing_secrets(tmp_path) -> None:
    store = SQLiteStore(tmp_path)
    store.initialize()
    daemon = store.ensure_daemon(owner="test")
    store.record_daemon_event(
        daemon.id,
        "execution_adapter_rejected",
        "Unknown adapter rejected.",
        {
            "adapter_id": "unknown_adapter",
            "reason_code": "unknown_adapter",
            "rejection_reasons": ["OPENAI_API_KEY=sk-secret"],
        },
    )
    store.record_daemon_event(
        daemon.id,
        "execution_adapter_rejected",
        "Breaker open.",
        {"adapter_id": "dry_run", "reason_code": "breaker_open"},
    )
    hosted = store.create_run(
        goal="hosted without approval",
        task_type="repo_planning",
        backend=default_config().backends["codex_cli"],
    )
    store.append_event(hosted.id, "info", "apply_back_applied", "Applied.", {"files": ["app.py"]})
    store.append_event(hosted.id, "info", "docker_test", "Docker test.", {"network": True})
    artifact_path = tmp_path / ".harness" / "runs" / hosted.id / "metadata.txt"
    artifact_path.write_text("safe body", encoding="utf-8")
    store.register_artifact(
        hosted.id,
        "metadata",
        artifact_path,
        metadata={"token": "Bearer abcdefghijklmnop"},
    )
    with store.connect() as conn:
        conn.execute(
            "UPDATE artifacts SET metadata_json = ? WHERE run_id = ? AND kind = ?",
            (json.dumps({"token": "Bearer abcdefghijklmnop"}), hosted.id, "metadata"),
        )

    result = run_security_check(tmp_path, store)
    serialized = json.dumps(result.model_dump(mode="json"))
    check_ids = {finding.check_id for finding in result.findings}

    assert result.ok is False
    assert {
        "unknown_adapter_dispatch_attempt",
        "breaker_open_execution_attempt",
        "hosted_boundary_without_approval",
        "apply_back_without_inspected_approval",
        "docker_network_enabled",
        "secret_like_metadata_output",
    } <= check_ids
    assert "sk-secret" not in serialized
    assert "abcdefghijklmnop" not in serialized
    assert "OPENAI_API_KEY" not in serialized
