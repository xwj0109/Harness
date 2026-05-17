from __future__ import annotations

from pathlib import Path
from typing import Any

from harness import __version__
from harness.config import HARNESS_DIR, load_config
from harness.security import sanitize_for_logging


WORKSPACE_CATALOG_SCHEMA_VERSION = "harness.workspaces/v1"
WORKSPACE_ACTION_SCHEMA_VERSION = "harness.workspace_action/v1"
WORKSPACE_CLIENTS_SCHEMA_VERSION = "harness.workspace_clients/v1"


def build_workspace_catalog(project_root: Path) -> dict[str, Any]:
    harness_dir = project_root / HARNESS_DIR
    initialized = (harness_dir / "harness.sqlite").exists()
    config = load_config(project_root) if initialized else None
    workspace = {
        "id": _workspace_id(project_root),
        "path": str(project_root),
        "project_name": config.project_name if config is not None else project_root.name,
        "current": True,
        "initialized": initialized,
        "harness_dir": str(harness_dir),
        "config_exists": (harness_dir / "config.yaml").exists(),
        "database_exists": initialized,
        "route": "/",
        "server_attach_supported": False,
        "sync_supported": False,
        "steal_supported": False,
    }
    return {
        "schema_version": WORKSPACE_CATALOG_SCHEMA_VERSION,
        "ok": True,
        "harness_version": __version__,
        "current_workspace_id": workspace["id"],
        "workspaces": [workspace],
        "registry_scope": "current_project_only",
        "global_registry_supported": False,
        "workspace_routing_supported": True,
        "remote_attach_supported": False,
        "sync_supported": False,
        "steal_supported": False,
        "client_conflict_detection_supported": False,
        "network_called": False,
        "filesystem_modified": False,
        "process_started": False,
        "permission_granting": False,
    }


def build_workspace_clients_projection(project_root: Path) -> dict[str, Any]:
    return {
        "schema_version": WORKSPACE_CLIENTS_SCHEMA_VERSION,
        "ok": True,
        "workspace_id": _workspace_id(project_root),
        "workspace_path": str(project_root),
        "clients": [],
        "active_client_id": None,
        "client_registration_supported": False,
        "conflict_detection_supported": False,
        "steal_supported": False,
        "dispose_supported": False,
        "lease_ttl_seconds": None,
        "network_called": False,
        "filesystem_modified": False,
        "process_started": False,
        "permission_granting": False,
    }


def workspace_action_unsupported(action: str, workspace_id: str | None, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": WORKSPACE_ACTION_SCHEMA_VERSION,
        "ok": False,
        "action": action,
        "workspace_id": workspace_id,
        "requested": sanitize_for_logging(body or {}),
        "error": f"Workspace {action} is not implemented yet; refusing to attach, sync, steal, or route to another workspace.",
        "network_called": False,
        "filesystem_modified": False,
        "process_started": False,
        "attached": False,
        "client_registered": False,
        "client_stolen": False,
        "disposed": False,
        "sync_started": False,
        "permission_granting": False,
    }


def _workspace_id(project_root: Path) -> str:
    return "ws_" + project_root.resolve().as_posix().encode("utf-8").hex()[-16:]
