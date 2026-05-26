from __future__ import annotations

from pathlib import Path

from harness.action_reports import write_managed_action_report
from harness.action_router import (
    ManagedActionDecision,
    ManagedActionDecisionStatus,
    ManagedActionResult,
    ManagedActionRoute,
)
from harness.memory.sqlite_store import SQLiteStore


def execute_managed_action(
    project_root: Path,
    route: ManagedActionRoute,
    decision: ManagedActionDecision,
    store: SQLiteStore,
) -> ManagedActionResult:
    if decision.status != ManagedActionDecisionStatus.AUTO_ALLOWED:
        raise ValueError(f"Managed action was not auto-allowed by policy: {decision.status.value}")
    if decision.route.model_dump(mode="json") != route.model_dump(mode="json"):
        raise ValueError("Managed action decision does not match the requested route.")
    if route.executor == "create_empty_file":
        return _execute_create_empty_file(project_root, route, decision, store)
    if route.executor == "create_file_with_content":
        return _execute_create_file_with_content(project_root, route, decision, store)
    if route.executor == "create_directory":
        return _execute_create_directory(project_root, route, decision, store)
    if route.executor == "write_file":
        return _execute_write_file(project_root, route, decision, store)
    if route.executor == "write_note_file":
        return _execute_write_note_file(project_root, route, decision, store)
    raise ValueError(f"Unknown managed action executor: {route.executor}")


def _execute_create_empty_file(
    project_root: Path,
    route: ManagedActionRoute,
    decision: ManagedActionDecision,
    store: SQLiteStore,
) -> ManagedActionResult:
    requested = str(route.normalized_arguments.get("filename") or route.normalized_arguments.get("default_filename") or "scratch.txt")
    target = _next_available_path(project_root, requested)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("", encoding="utf-8")
    run = store.create_run(goal=str(route.normalized_arguments.get("request") or route.intent), task_type=f"managed_action.{route.executor}", status="succeeded")
    store.append_event(run.id, "info", "managed_action.file_created", "Created empty file.", {"path": str(target), "intent": route.intent})
    created_artifact = store.register_artifact(
        run.id,
        "created_file",
        target,
        metadata={"created_from": "managed_action", "intent": route.intent},
        producer="managed_action",
        redaction_state="not_required",
    )
    result = ManagedActionResult(
        ok=True,
        status="succeeded",
        intent=route.intent,
        run_id=run.id,
        created_paths=[target],
        artifact_ids=[created_artifact.id],
        manifest_path=store.runs_dir / run.id / "manifest.json",
        message=f"Created `{target.name}`.",
    )
    report_path = write_managed_action_report(
        store,
        run.id,
        request=str(route.normalized_arguments.get("request") or route.intent),
        route=route,
        decision=decision,
        result=result,
    )
    report_artifact = store.register_artifact(
        run.id,
        "final_report",
        report_path,
        metadata={"created_from": "managed_action", "intent": route.intent},
        producer="managed_action",
        redaction_state="not_required",
    )
    return result.model_copy(update={"report_path": report_path, "artifact_ids": [created_artifact.id, report_artifact.id]})


def _execute_create_file_with_content(
    project_root: Path,
    route: ManagedActionRoute,
    decision: ManagedActionDecision,
    store: SQLiteStore,
) -> ManagedActionResult:
    requested = str(route.normalized_arguments.get("filename") or "scratch.txt")
    target = _next_available_path(project_root, requested)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = str(route.normalized_arguments.get("text") or "")
    target.write_text(text if text.endswith("\n") else f"{text}\n", encoding="utf-8")
    run = store.create_run(goal=str(route.normalized_arguments.get("request") or route.intent), task_type=f"managed_action.{route.executor}", status="succeeded")
    store.append_event(run.id, "info", "managed_action.file_created", "Created file with content.", {"path": str(target), "intent": route.intent})
    created_artifact = store.register_artifact(
        run.id,
        "created_file",
        target,
        metadata={"created_from": "managed_action", "intent": route.intent},
        producer="managed_action",
        redaction_state="not_required",
    )
    result = ManagedActionResult(
        ok=True,
        status="succeeded",
        intent=route.intent,
        run_id=run.id,
        created_paths=[target],
        artifact_ids=[created_artifact.id],
        manifest_path=store.runs_dir / run.id / "manifest.json",
        message=f"Created `{target.name}`.",
    )
    report_path = write_managed_action_report(
        store,
        run.id,
        request=str(route.normalized_arguments.get("request") or route.intent),
        route=route,
        decision=decision,
        result=result,
    )
    report_artifact = store.register_artifact(
        run.id,
        "final_report",
        report_path,
        metadata={"created_from": "managed_action", "intent": route.intent},
        producer="managed_action",
        redaction_state="not_required",
    )
    return result.model_copy(update={"report_path": report_path, "artifact_ids": [created_artifact.id, report_artifact.id]})


def _execute_create_directory(
    project_root: Path,
    route: ManagedActionRoute,
    decision: ManagedActionDecision,
    store: SQLiteStore,
) -> ManagedActionResult:
    dirname = str(route.normalized_arguments.get("dirname") or "new-folder")
    target = project_root / dirname
    created = not target.exists()
    if target.exists() and not target.is_dir():
        raise ValueError(f"Target exists and is not a directory: {dirname}")
    target.mkdir(parents=False, exist_ok=True)
    run = store.create_run(goal=str(route.normalized_arguments.get("request") or route.intent), task_type=f"managed_action.{route.executor}", status="succeeded")
    store.append_event(run.id, "info", "managed_action.directory_created", "Created directory." if created else "Directory already existed.", {"path": str(target), "created": created})
    result = ManagedActionResult(
        ok=True,
        status="succeeded",
        intent=route.intent,
        run_id=run.id,
        created_paths=[target] if created else [],
        changed_paths=[],
        manifest_path=store.runs_dir / run.id / "manifest.json",
        message=f"{'Created' if created else 'Directory already exists'} `{target.name}`.",
    )
    report_path = write_managed_action_report(
        store,
        run.id,
        request=str(route.normalized_arguments.get("request") or route.intent),
        route=route,
        decision=decision,
        result=result,
    )
    report_artifact = store.register_artifact(run.id, "final_report", report_path, producer="managed_action", redaction_state="not_required")
    return result.model_copy(update={"report_path": report_path, "artifact_ids": [report_artifact.id]})


def _execute_write_note_file(
    project_root: Path,
    route: ManagedActionRoute,
    decision: ManagedActionDecision,
    store: SQLiteStore,
) -> ManagedActionResult:
    filename = str(route.normalized_arguments.get("filename") or "notes.md")
    target = project_root / filename
    _ensure_writable_file_target(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = str(route.normalized_arguments.get("text") or "").strip()
    existed = target.exists()
    existing = target.read_text(encoding="utf-8") if existed else ""
    separator = "" if not existing or existing.endswith("\n") else "\n"
    target.write_text(f"{existing}{separator}{text}\n", encoding="utf-8")
    run = store.create_run(goal=str(route.normalized_arguments.get("request") or route.intent), task_type=f"managed_action.{route.executor}", status="succeeded")
    store.append_event(run.id, "info", "managed_action.note_written", "Wrote local note.", {"path": str(target)})
    note_artifact = store.register_artifact(
        run.id,
        "changed_file" if existed else "created_file",
        target,
        metadata={"created_from": "managed_action", "intent": route.intent},
        producer="managed_action",
        redaction_state="not_required",
    )
    result = ManagedActionResult(
        ok=True,
        status="succeeded",
        intent=route.intent,
        run_id=run.id,
        created_paths=[] if existed else [target],
        changed_paths=[target] if existed else [],
        artifact_ids=[note_artifact.id],
        manifest_path=store.runs_dir / run.id / "manifest.json",
        message=f"{'Updated' if existed else 'Created'} `{target.name}`.",
    )
    report_path = write_managed_action_report(
        store,
        run.id,
        request=str(route.normalized_arguments.get("request") or route.intent),
        route=route,
        decision=decision,
        result=result,
    )
    report_artifact = store.register_artifact(run.id, "final_report", report_path, producer="managed_action", redaction_state="not_required")
    return result.model_copy(update={"report_path": report_path, "artifact_ids": [note_artifact.id, report_artifact.id]})


def _execute_write_file(
    project_root: Path,
    route: ManagedActionRoute,
    decision: ManagedActionDecision,
    store: SQLiteStore,
) -> ManagedActionResult:
    filename = str(route.normalized_arguments.get("filename") or "scratch.md")
    target = project_root / filename
    _ensure_writable_file_target(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = str(route.normalized_arguments.get("text") or "")
    existed = target.exists()
    existing = target.read_text(encoding="utf-8") if existed else ""
    separator = "" if not existing or existing.endswith("\n") else "\n"
    target.write_text(f"{existing}{separator}{text}\n", encoding="utf-8")
    run = store.create_run(goal=str(route.normalized_arguments.get("request") or route.intent), task_type=f"managed_action.{route.executor}", status="succeeded")
    store.append_event(run.id, "info", "managed_action.file_written", "Wrote file content.", {"path": str(target), "intent": route.intent})
    changed_artifact = store.register_artifact(
        run.id,
        "changed_file" if existed else "created_file",
        target,
        metadata={"created_from": "managed_action", "intent": route.intent},
        producer="managed_action",
        redaction_state="not_required",
    )
    result = ManagedActionResult(
        ok=True,
        status="succeeded",
        intent=route.intent,
        run_id=run.id,
        created_paths=[] if existed else [target],
        changed_paths=[target] if existed else [],
        artifact_ids=[changed_artifact.id],
        manifest_path=store.runs_dir / run.id / "manifest.json",
        message=f"{'Updated' if existed else 'Created'} `{target.name}`.",
    )
    report_path = write_managed_action_report(
        store,
        run.id,
        request=str(route.normalized_arguments.get("request") or route.intent),
        route=route,
        decision=decision,
        result=result,
    )
    report_artifact = store.register_artifact(run.id, "final_report", report_path, producer="managed_action", redaction_state="not_required")
    return result.model_copy(update={"report_path": report_path, "artifact_ids": [changed_artifact.id, report_artifact.id]})


def _next_available_path(project_root: Path, filename: str) -> Path:
    path = Path(filename)
    base = path.stem or "scratch"
    suffix = path.suffix
    parent = path.parent if str(path.parent) != "." else Path()
    candidate = project_root / parent / f"{base}{suffix}"
    index = 2
    while candidate.exists():
        candidate = project_root / parent / f"{base}-{index}{suffix}"
        index += 1
    return candidate


def _ensure_writable_file_target(target: Path) -> None:
    if target.exists() and target.is_dir():
        raise ValueError(f"Target exists and is a directory: {target.name}")
