from __future__ import annotations

from typing import Any

from harness.models import EventStreamType, RedactionState
from harness.security import sanitize_for_logging


PENDING_CHAT_ACTION_SCHEMA_VERSION = "harness.pending_chat_action/v1"
PENDING_CHAT_ACTION_METADATA_KEY = "pending_chat_action"
PENDING_CHAT_ACTION_PROJECTION_SCHEMA_VERSION = "harness.pending_chat_action_projection/v1"
PENDING_CHAT_ACTION_AUDIT_SCHEMA_VERSION = "harness.pending_chat_action_audit/v1"
PENDING_CHAT_ACTION_CLEAR_SCHEMA_VERSION = "harness.pending_chat_action_clear/v1"

_VALID_PENDING_CHAT_ACTION_KINDS = {
    "task_draft",
    "orchestration_draft",
    "action_contract",
    "execute_lease",
    "hosted_approval",
}
_PASSIVE_PENDING_ACTION_FLAGS = {
    "process_started": False,
    "filesystem_modified": False,
    "active_repo_modified": False,
    "adapter_dispatch_started": False,
    "provider_called": False,
    "model_context_sent": False,
    "network_called": False,
    "permission_granting": False,
    "authority_granting": False,
}


def pending_chat_action_projection(
    metadata: dict[str, Any] | None,
    *,
    session_id: str | None = None,
    lease_status: str | None = None,
) -> dict[str, Any] | None:
    audit = pending_chat_action_audit(metadata, session_id=session_id, lease_status=lease_status)
    return audit.get("pending_action") if audit.get("recoverable") else None


def pending_chat_action_audit(
    metadata: dict[str, Any] | None,
    *,
    session_id: str | None = None,
    lease_status: str | None = None,
) -> dict[str, Any]:
    raw = (metadata or {}).get(PENDING_CHAT_ACTION_METADATA_KEY)
    if raw is None:
        return {
            "schema_version": PENDING_CHAT_ACTION_AUDIT_SCHEMA_VERSION,
            "session_id": session_id,
            "present": False,
            "status": "missing",
            "recoverable": False,
            "pending_action": None,
            "issues": [],
            "cleanup_supported": False,
            "cleanup_command": None,
            "cleanup_route": None,
            "next_commands": [],
            "raw_metadata_exposed": False,
            **_PASSIVE_PENDING_ACTION_FLAGS,
        }
    issues = _pending_chat_action_issues(raw, lease_status=lease_status)
    if issues:
        status = "stale" if any(str(issue.get("code") or "").startswith("stale_") for issue in issues) else "invalid"
        return {
            "schema_version": PENDING_CHAT_ACTION_AUDIT_SCHEMA_VERSION,
            "session_id": session_id,
            "present": True,
            "status": status,
            "recoverable": False,
            "pending_action": None,
            "issues": issues,
            "cleanup_supported": bool(session_id),
            "cleanup_command": _cleanup_command(session_id),
            "cleanup_route": _cleanup_route(session_id),
            "next_commands": [command for command in [_cleanup_command(session_id)] if command],
            "raw_metadata_exposed": False,
            **_PASSIVE_PENDING_ACTION_FLAGS,
        }
    if not isinstance(raw, dict):
        raise AssertionError("validated pending chat action metadata must be a dict")
    projection = _recoverable_pending_chat_action_projection(raw, session_id=session_id)
    return {
        "schema_version": PENDING_CHAT_ACTION_AUDIT_SCHEMA_VERSION,
        "session_id": session_id,
        "present": True,
        "status": "recoverable",
        "recoverable": True,
        "pending_action": projection,
        "issues": [],
        "cleanup_supported": bool(session_id),
        "cleanup_command": _cleanup_command(session_id),
        "cleanup_route": _cleanup_route(session_id),
        "next_commands": projection["next_commands"],
        "raw_metadata_exposed": False,
        **_PASSIVE_PENDING_ACTION_FLAGS,
    }


def clear_pending_chat_action_metadata(store: Any, session_id: str, *, actor: str = "harness") -> dict[str, Any]:
    session = store.get_session(session_id)
    before = pending_chat_action_audit(session.metadata, session_id=session.id)
    metadata = dict(session.metadata or {})
    cleared = PENDING_CHAT_ACTION_METADATA_KEY in metadata
    if cleared:
        metadata.pop(PENDING_CHAT_ACTION_METADATA_KEY, None)
        session = store.update_session(session.id, metadata=metadata)
    event = None
    if cleared and hasattr(store, "append_store_event"):
        event = store.append_store_event(
            EventStreamType.SESSION,
            session.id,
            "pending_chat_action.cleared",
            sanitize_for_logging(
                {
                    "actor": actor,
                    "status_before": before.get("status"),
                    "kind_before": (before.get("pending_action") or {}).get("kind"),
                    "issue_codes": [issue.get("code") for issue in before.get("issues") or []],
                    "metadata_key_removed": True,
                    "process_started": False,
                    "filesystem_modified": False,
                    "active_repo_modified": False,
                    "adapter_dispatch_started": False,
                    "provider_called": False,
                    "model_context_sent": False,
                    "network_called": False,
                    "permission_granting": False,
                    "authority_granting": False,
                }
            ),
            session_id=session.id,
            redaction_state=RedactionState.NOT_REQUIRED,
        )
    after = pending_chat_action_audit(session.metadata, session_id=session.id)
    return {
        "schema_version": PENDING_CHAT_ACTION_CLEAR_SCHEMA_VERSION,
        "ok": True,
        "session_id": session.id,
        "cleared": cleared,
        "had_pending_action_metadata": before["present"],
        "audit_before": before,
        "audit_after": after,
        "event": event.model_dump(mode="json") if event is not None else None,
        "mutation_scope": "session_metadata_only",
        "session_metadata_mutated": cleared,
        "events_appended": 1 if event is not None else 0,
        "objectives_mutated": False,
        "tasks_mutated": False,
        "leases_mutated": False,
        "runs_mutated": False,
        "approvals_mutated": False,
        "artifacts_mutated": False,
        "messages_mutated": False,
        "events_deleted": False,
        **_PASSIVE_PENDING_ACTION_FLAGS,
    }


def _recoverable_pending_chat_action_projection(raw: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    kind = str(raw.get("kind") or "unknown")
    subject = _subject_for_pending_action(raw)
    return {
        "schema_version": PENDING_CHAT_ACTION_PROJECTION_SCHEMA_VERSION,
        "session_id": session_id,
        "recoverable": True,
        "kind": kind,
        "label": _label_for_kind(kind),
        "summary": sanitize_for_logging(subject),
        "requires_confirmation": True,
        "next_commands": ["/confirm", "/decline"],
        "resume_instruction": (
            f"Resume session {session_id} and type /confirm or /decline."
            if session_id
            else "Resume this session and type /confirm or /decline."
        ),
        **_PASSIVE_PENDING_ACTION_FLAGS,
    }


def pending_chat_action_search_text(projection: dict[str, Any] | None) -> str:
    if not projection:
        return ""
    issues = projection.get("issues") if isinstance(projection.get("issues"), list) else []
    issue_text = " ".join(str(issue.get("code") or "") for issue in issues if isinstance(issue, dict))
    searchable = " ".join(
        str(projection.get(key) or "")
        for key in ("kind", "label", "summary", "resume_instruction", "status", "cleanup_command")
    )
    return f"{searchable} {issue_text}".strip() if issue_text else searchable


def _pending_chat_action_issues(raw: Any, *, lease_status: str | None) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return [
            {
                "code": "invalid_metadata_type",
                "message": f"Pending chat action metadata must be an object, got {type(raw).__name__}.",
            }
        ]
    issues: list[dict[str, Any]] = []
    if raw.get("schema_version") != PENDING_CHAT_ACTION_SCHEMA_VERSION:
        issues.append(
            {
                "code": "unsupported_schema",
                "message": "Pending chat action metadata uses an unsupported schema version.",
            }
        )
    kind = str(raw.get("kind") or "").strip()
    if kind not in _VALID_PENDING_CHAT_ACTION_KINDS:
        issues.append(
            {
                "code": "unknown_kind",
                "message": f"Pending chat action kind is not supported: {sanitize_for_logging(kind or 'missing')}.",
            }
        )
        return issues
    if kind == "action_contract":
        contract = raw.get("contract")
        if not isinstance(contract, dict):
            issues.append({"code": "missing_contract", "message": "Action-contract pending action is missing a contract object."})
        else:
            for key in ("id", "tool", "risk"):
                if not _non_empty_string(contract.get(key)):
                    issues.append({"code": f"missing_contract_{key}", "message": f"Action contract is missing {key}."})
            if "normalized_arguments" in contract and not isinstance(contract.get("normalized_arguments"), dict):
                issues.append(
                    {
                        "code": "invalid_contract_arguments",
                        "message": "Action contract normalized_arguments must be an object.",
                    }
                )
    elif kind == "orchestration_draft":
        draft = raw.get("draft")
        if not isinstance(draft, dict):
            issues.append({"code": "missing_orchestration_draft", "message": "Orchestration pending action is missing a draft object."})
        else:
            if "tasks" in draft and not isinstance(draft.get("tasks"), list):
                issues.append({"code": "invalid_orchestration_tasks", "message": "Orchestration draft tasks must be a list."})
            if "checkpoints" in draft and not isinstance(draft.get("checkpoints"), list):
                issues.append(
                    {
                        "code": "invalid_orchestration_checkpoints",
                        "message": "Orchestration draft checkpoints must be a list.",
                    }
                )
    elif kind == "task_draft":
        if not isinstance(raw.get("draft"), dict):
            issues.append({"code": "missing_task_draft", "message": "Task pending action is missing a draft object."})
    elif kind == "execute_lease":
        if not _non_empty_string(raw.get("lease_id")):
            issues.append({"code": "missing_lease_id", "message": "Adapter-dispatch pending action is missing a lease id."})
        elif lease_status is not None and lease_status != "active":
            issues.append(
                {
                    "code": "stale_lease",
                    "message": f"Adapter-dispatch pending action references a lease that is not active: {lease_status}.",
                }
            )
    return issues


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _cleanup_command(session_id: str | None) -> str | None:
    return f"harness sessions clear-pending-action {session_id}" if session_id else None


def _cleanup_route(session_id: str | None) -> str | None:
    return f"DELETE /sessions/{session_id}/pending-action" if session_id else None


def _label_for_kind(kind: str) -> str:
    labels = {
        "task_draft": "Pending task draft",
        "orchestration_draft": "Pending orchestration draft",
        "action_contract": "Pending action contract",
        "execute_lease": "Pending adapter dispatch",
        "hosted_approval": "Pending hosted-boundary approval",
    }
    return labels.get(kind, "Pending chat action")


def _subject_for_pending_action(raw: dict[str, Any]) -> str:
    kind = str(raw.get("kind") or "")
    if kind == "action_contract":
        contract = raw.get("contract") if isinstance(raw.get("contract"), dict) else {}
        return str(contract.get("summary") or contract.get("tool") or "action contract")
    if kind == "orchestration_draft":
        draft = raw.get("draft") if isinstance(raw.get("draft"), dict) else {}
        return str(draft.get("objective_title") or draft.get("proposed_action") or "orchestration draft")
    if kind == "task_draft":
        draft = raw.get("draft") if isinstance(raw.get("draft"), dict) else {}
        return str(draft.get("title") or draft.get("proposed_action") or "task draft")
    if kind == "execute_lease":
        return f"lease {raw.get('lease_id') or 'unknown'}"
    if kind == "hosted_approval":
        return "hosted-boundary approval"
    return "pending chat action"
