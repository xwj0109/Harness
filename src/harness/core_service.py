from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from harness import __version__
from harness.event_broker import EventSubscription, subscribe_global_events, subscribe_store_events
from pydantic import BaseModel, Field

from harness.execution import (
    CODEX_CODE_EDIT_TASK_TYPE,
    CODEX_ISOLATED_EDIT_ADAPTER,
    REPO_PLANNING_EXECUTION_ADAPTER,
    REPO_PLANNING_TASK_TYPE,
    execute_lease,
)
from harness.config import load_config
from harness.memory.sqlite_store import DRY_RUN_EXECUTION_ADAPTER, DRY_RUN_TASK_TYPE, SQLiteStore
from harness.model_catalog import build_model_provider_suggestions, parse_model_ref, validate_model_selection
from harness.model_discovery import ModelDiscoveryError, list_cached_discovered_models, refresh_model_discovery
from harness.models import (
    EventRecord,
    EventStreamType,
    ObjectiveRecord,
    RedactionState,
    RunRecord,
    SessionMessageRole,
    SessionPermissionSource,
    SessionPermissionStatus,
    SessionPartKind,
    SessionSpec,
    SessionStatus,
    StoredEventRecord,
    TaskLease,
    run_mode_for_task_type,
)
from harness.operator_context import build_session_pane_projection, build_tui_dashboard
from harness.operator_loop import session_operator_status_projection
from harness.provider_adapters import build_default_provider_adapter
from harness.provider_auth import (
    activate_provider_auth_account,
    connect_provider_api_key,
    connect_provider_env,
    connect_provider_local_account,
    disconnect_provider_auth,
    provider_oauth_authorize,
    provider_oauth_callback,
    provider_auth_methods_projection,
)
from harness.security import sanitize_for_logging
from harness.session_cwd import session_cwd_payload
from harness.session_runtime import SessionPromptQueuePolicy, SessionPromptRequest, SessionRuntimeManager
from harness.session_tools import (
    build_session_approval_card,
    session_planning_mode_projection,
)
from harness.tui import build_tui_settings_catalog


CORE_SCHEMA_VERSION = "harness.core_run/v1"
APP_SERVICE_SCHEMA_VERSION = "harness.app_service/v1"
CORE_OWNER = "core_service"
SUPPORTED_CORE_MODES = {"dry_run", "repo_planning", "codex_isolated_edit"}


class HarnessHTTPServiceError(RuntimeError):
    """Raised when an attached local server rejects or cannot satisfy a service call."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        error_code: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.payload = payload or {}


class HarnessHTTPAppService:
    """HTTP implementation of the app-facing service facade for attached TUI mode."""

    def __init__(self, server_url: str, token: str, *, timeout: float = 5.0) -> None:
        clean_url = str(server_url or "").strip().rstrip("/")
        clean_token = str(token or "").strip()
        if not clean_url:
            raise ValueError("Missing server URL.")
        if not clean_token:
            raise ValueError("Missing server token.")
        self.server_url = clean_url
        self.token = clean_token
        self.timeout = timeout
        self.project_root = Path(".").resolve()

    @classmethod
    def from_attach(cls, server_url: str, token: str | None, *, timeout: float = 5.0) -> "HarnessHTTPAppService":
        if not token:
            raise ValueError("Missing server token. Pass --token or set HARNESS_SERVER_TOKEN.")
        service = cls(server_url, token, timeout=timeout)
        health = service.health()
        if not health.get("ok"):
            raise HarnessHTTPServiceError("Attached server health probe failed.", payload=health)
        return service

    def health(self) -> dict[str, Any]:
        return self._get("/global/health")

    def dashboard(self, *, selected_session_id: str | None = None) -> dict[str, Any]:
        query = {"selected_session_id": selected_session_id} if selected_session_id else None
        return self._get("/tui/dashboard", query=query)

    def session_pane(
        self,
        *,
        selected_session_id: str | None,
        status_filter: str,
        query: str,
    ) -> dict[str, Any]:
        params = {
            "status_filter": status_filter,
            "query": query,
        }
        if selected_session_id:
            params["selected_session_id"] = selected_session_id
        return self._get("/tui/session-pane", query=params)

    def list_sessions(self) -> dict[str, Any]:
        return self._get("/sessions")

    def create_session(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._post("/sessions", body)

    def archive_session(self, session_id: str) -> dict[str, Any]:
        return self._delete(f"/sessions/{session_id}")

    def restore_session(self, session_id: str) -> dict[str, Any]:
        return self._post(f"/sessions/{session_id}/restore", {})

    def abort_session(self, session_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._post(f"/sessions/{session_id}/abort", body or {})

    def fork_session(self, session_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._post(f"/sessions/{session_id}/fork", body or {})

    def hard_delete_session(self, session_id: str) -> dict[str, Any]:
        return self._post(f"/sessions/{session_id}/hard-delete", {})

    def update_session_title(self, session_id: str, title: str | None) -> dict[str, Any]:
        return self._patch(f"/sessions/{session_id}", {"title": title})

    def update_session_agent(self, session_id: str, agent_id: str | None, *, source: str = "http_app_service") -> dict[str, Any]:
        return self._patch(f"/sessions/{session_id}", {"agent_id": agent_id, "source": source})

    def update_session_model_selection(
        self,
        session_id: str,
        raw_model_ref: str,
        *,
        source: str = "http_app_service_model_picker",
    ) -> dict[str, Any]:
        return self._patch(f"/sessions/{session_id}", {"raw_model_ref": raw_model_ref, "source": source})

    def append_message(self, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._post(f"/sessions/{session_id}/messages", body)

    def prompt_async(self, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._post(f"/sessions/{session_id}/prompt_async", body)

    def submit_prompt(self, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._post(f"/sessions/{session_id}/prompt", body)

    def sessions_status(self) -> dict[str, Any]:
        return self._get("/sessions/status")

    def session_status(self, session_id: str) -> dict[str, Any]:
        return self._get(f"/sessions/{session_id}/status")

    def list_messages(self, session_id: str, *, limit: int | None = None) -> dict[str, Any]:
        return self._get(f"/sessions/{session_id}/messages", query={"limit": limit} if limit is not None else None)

    def message_detail(self, session_id: str, message_id: str) -> dict[str, Any]:
        return self._get(f"/sessions/{session_id}/messages/{message_id}")

    def list_events(
        self,
        session_id: str,
        *,
        after_seq: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {}
        if after_seq is not None:
            query["after_seq"] = after_seq
        if limit is not None:
            query["limit"] = limit
        return self._get(f"/sessions/{session_id}/events", query=query or None)

    def runtime_status(self, session_id: str) -> dict[str, Any]:
        status = self.session_status(session_id)
        return {
            "schema_version": "harness.session_runtime_status/v1",
            "ok": bool(status.get("ok", True)),
            "session_id": session_id,
            "runtime": status.get("runtime") or {},
            "execution_started": False,
            "permission_granting": False,
        }

    def list_permissions(self, session_id: str | None = None) -> dict[str, Any]:
        if session_id is None:
            return self._get("/permission")
        return self._get(f"/sessions/{session_id}/permissions")

    def reply_permission(self, session_id: str, permission_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._post(f"/sessions/{session_id}/permissions/{permission_id}/reply", body)

    def list_questions(self, session_id: str | None = None) -> dict[str, Any]:
        if session_id is None:
            return self._get("/question")
        return self._get(f"/sessions/{session_id}/questions")

    def settings_tui(self, session_id: str | None = None) -> dict[str, Any]:
        return self._get("/settings/tui")

    def provider_auth_methods(self) -> dict[str, Any]:
        return self._get("/provider/auth")

    def list_providers(self) -> dict[str, Any]:
        return self._get("/providers")

    def provider_detail(self, provider_id: str) -> dict[str, Any]:
        return self._get(f"/providers/{provider_id}")

    def list_models(self) -> dict[str, Any]:
        return self._get("/models")

    def model_detail(self, provider_id: str, model_id: str) -> dict[str, Any]:
        return self._get(f"/models/{provider_id}/{model_id}")

    def validate_model(self, raw_model_ref: str) -> dict[str, Any]:
        return self._get("/models/validate", query={"model": raw_model_ref})

    def model_preferences(self) -> dict[str, Any]:
        return self._get("/models/preferences")

    def connect_provider_api_key(
        self,
        provider_id: str,
        api_key: str,
        *,
        description: str = "default",
        active: bool = True,
    ) -> dict[str, Any]:
        return self._post(
            f"/provider/{provider_id}/auth/api-key",
            {"api_key": api_key, "description": description, "active": active},
        )

    def connect_provider_env(
        self,
        provider_id: str,
        env_var: str,
        *,
        description: str = "default",
        active: bool = True,
    ) -> dict[str, Any]:
        return self._post(
            f"/provider/{provider_id}/auth/env",
            {"env_var": env_var, "description": description, "active": active},
        )

    def connect_provider_local_account(
        self,
        provider_id: str,
        credential_kind: str,
        *,
        description: str = "default",
        active: bool = True,
        env_var: str | None = None,
    ) -> dict[str, Any]:
        return self._post(
            f"/provider/{provider_id}/auth/local",
            {
                "credential_kind": credential_kind,
                "description": description,
                "active": active,
                "env_var": env_var,
            },
        )

    def activate_provider_account(self, provider_id: str, account_id: str) -> dict[str, Any]:
        return self._post(f"/provider/{provider_id}/auth/activate", {"account_id": account_id})

    def disconnect_provider(self, provider_id: str) -> dict[str, Any]:
        return self._delete(f"/provider/{provider_id}/auth")

    def set_model_favorite(self, raw_model_ref: str, favorite: bool) -> dict[str, Any]:
        return self._post("/models/preferences/favorite", {"raw_model_ref": raw_model_ref, "favorite": favorite})

    def set_default_model_preference(self, raw_model_ref: str) -> dict[str, Any]:
        return self._post("/models/preferences/default", {"raw_model_ref": raw_model_ref})

    def inspect_model(self, raw_model_ref: str) -> dict[str, Any]:
        return self._post("/models/inspect", {"raw_model_ref": raw_model_ref})

    def refresh_provider_models(
        self,
        provider_id: str,
        *,
        approve_hosted: bool = False,
        with_credentials: bool = False,
    ) -> dict[str, Any]:
        return self._post(
            "/models/refresh",
            {
                "provider_id": provider_id,
                "approve_hosted": approve_hosted,
                "with_credentials": with_credentials,
            },
        )

    def provider_oauth_authorize(self, provider_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._post(f"/provider/{provider_id}/oauth/authorize", body or {})

    def provider_oauth_callback(self, provider_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._post(f"/provider/{provider_id}/oauth/callback", body)

    def subscribe_global_events(self) -> "_HTTPEventSubscription":
        return _HTTPEventSubscription(self._url("/global/event", query={"live": 1}), self.token, timeout=self.timeout)

    def subscribe_session_events(self, session_id: str, *, after_seq: int | None = None) -> "_HTTPEventSubscription":
        query: dict[str, Any] = {"live": 1}
        if after_seq is not None:
            query["after_seq"] = after_seq
        return _HTTPEventSubscription(self._url(f"/sessions/{session_id}/events/stream", query=query), self.token, timeout=self.timeout)

    def _get(self, path: str, *, query: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request_json("GET", path, query=query)

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", path, body=body)

    def _patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("PATCH", path, body=body)

    def _delete(self, path: str) -> dict[str, Any]:
        return self._request_json("DELETE", path)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self._url(path, query=query), data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except HTTPError as exc:
            payload = _http_error_payload(exc)
            error_code = payload.get("error_code") if isinstance(payload, dict) else None
            message = payload.get("error") if isinstance(payload, dict) else str(exc)
            raise HarnessHTTPServiceError(
                str(message),
                status=exc.code,
                error_code=str(error_code) if error_code else None,
                payload=payload if isinstance(payload, dict) else {},
            ) from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise HarnessHTTPServiceError(f"Attached server request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise HarnessHTTPServiceError("Attached server returned a non-object JSON payload.")
        return payload

    def _url(self, path: str, *, query: dict[str, Any] | None = None) -> str:
        if not path.startswith("/"):
            path = f"/{path}"
        url = f"{self.server_url}{path}"
        if query:
            clean_query = {key: value for key, value in query.items() if value is not None}
            if clean_query:
                url = f"{url}?{urlencode(clean_query)}"
        return url


class _HTTPEventSubscription:
    def __init__(self, url: str, token: str, *, timeout: float = 5.0) -> None:
        self._url = url
        self._token = token
        self._timeout = timeout
        self._subscription = EventSubscription()
        self._response = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def closed(self) -> bool:
        return self._subscription.closed

    def next(self, timeout: float | None = None) -> StoredEventRecord | None:
        return self._subscription.next(timeout=timeout)

    def drain(self, *, limit: int | None = None) -> list[StoredEventRecord]:
        return self._subscription.drain(limit=limit)

    def close(self) -> None:
        self._subscription.close()
        response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    def _run(self) -> None:
        request = Request(self._url, headers={"Authorization": f"Bearer {self._token}", "Accept": "text/event-stream"})
        try:
            with urlopen(request, timeout=self._timeout) as response:
                self._response = response
                self._read_sse(response)
        except Exception:
            pass
        finally:
            self._subscription.close()
            self._response = None

    def _read_sse(self, response: Any) -> None:
        event_name: str | None = None
        data_lines: list[str] = []
        for raw_line in response:
            if self.closed:
                return
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                self._dispatch_sse_event(event_name, data_lines)
                event_name = None
                data_lines = []
                continue
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
                continue
            if line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())

    def _dispatch_sse_event(self, event_name: str | None, data_lines: list[str]) -> None:
        if not data_lines or event_name in {"harness.ready", "server.connected", "harness.heartbeat"}:
            return
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            return
        if (
            isinstance(payload, dict)
            and "kind" not in payload
            and isinstance(payload.get("payload"), dict)
            and "kind" in payload["payload"]
        ):
            payload = payload["payload"]
        if not isinstance(payload, dict) or "kind" not in payload or "seq" not in payload:
            return
        try:
            self._subscription.enqueue(StoredEventRecord.model_validate(payload))
        except Exception:
            return


def _http_error_payload(exc: HTTPError) -> dict[str, Any]:
    try:
        raw = exc.read().decode("utf-8")
        payload = json.loads(raw or "{}")
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


class HarnessAppService:
    """Shared app-facing backend facade for TUI and local-server projections."""

    def __init__(
        self,
        project_root: Path,
        *,
        store: SQLiteStore | None = None,
        runtime: SessionRuntimeManager | None = None,
        execution_enabled: bool = True,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.store = store or SQLiteStore(self.project_root)
        self.runtime = runtime
        self.execution_enabled = execution_enabled

    def health(self) -> dict[str, Any]:
        return {
            "schema_version": APP_SERVICE_SCHEMA_VERSION,
            "ok": True,
            "healthy": True,
            "version": __version__,
            "project_root": str(self.project_root),
            "initialized": self.initialized,
            "execution_started": False,
            "permission_granting": False,
        }

    @property
    def initialized(self) -> bool:
        return self.store.db_path.exists()

    def dashboard(self, *, selected_session_id: str | None = None) -> dict[str, Any]:
        return build_tui_dashboard(self.project_root, selected_session_id=selected_session_id)

    def session_pane(
        self,
        *,
        selected_session_id: str | None,
        status_filter: str,
        query: str,
    ) -> dict[str, Any]:
        return build_session_pane_projection(
            self.project_root,
            selected_session_id=selected_session_id,
            status_filter=status_filter,
            query=query,
        )

    def list_sessions(self) -> dict[str, Any]:
        if not self.initialized:
            return self._uninitialized_payload("harness.sessions/v1", sessions=[])
        sessions = self.store.list_sessions()
        return {
            "schema_version": "harness.sessions/v1",
            "ok": True,
            "sessions": [session.model_dump(mode="json") for session in sessions],
            "execution_started": False,
            "permission_granting": False,
        }

    def create_session(self, body: dict[str, Any]) -> dict[str, Any]:
        self._require_initialized()
        session = self.store.create_session(
            title=_optional_text(body, "title") or "New session",
            intent=_optional_text(body, "intent"),
            metadata=dict(body.get("metadata") or {}),
            agent_id=_optional_text(body, "agent_id"),
            raw_model_ref=_optional_text(body, "raw_model_ref"),
        )
        return {
            "schema_version": "harness.session_create/v1",
            "ok": True,
            "session": session.model_dump(mode="json"),
            "session_id": session.id,
            "session_created": True,
            "messages_mutated": False,
            "parts_mutated": False,
            "execution_started": False,
            "process_started": False,
            "filesystem_modified": True,
            "active_repo_modified": False,
            "permission_granting": False,
            "authority_granting": False,
        }

    def archive_session(self, session_id: str) -> dict[str, Any]:
        self._require_initialized()
        session = self.store.archive_session(session_id)
        return {
            "schema_version": "harness.session_archive/v1",
            "ok": True,
            "session": session.model_dump(mode="json"),
            "archived": True,
            "hard_deleted": False,
            "messages_deleted": False,
            "parts_deleted": False,
            "events_deleted": False,
            "execution_started": False,
            "process_started": False,
            "permission_granting": False,
            "authority_granting": False,
        }

    def restore_session(self, session_id: str) -> dict[str, Any]:
        self._require_initialized()
        session = self.store.restore_session(session_id)
        return {
            "schema_version": "harness.session_restore/v1",
            "ok": True,
            "session": session.model_dump(mode="json"),
            "restored": True,
            "execution_started": False,
            "process_started": False,
            "permission_granting": False,
            "authority_granting": False,
        }

    def abort_session(self, session_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_initialized()
        reason = _optional_text(body or {}, "reason") or "tui_session_pane"
        session = self.store.cancel_session(session_id, reason=reason)
        return {
            "schema_version": "harness.session_abort/v1",
            "ok": True,
            "session": session.model_dump(mode="json"),
            "aborted": True,
            "cancelled": True,
            "process_stopped": False,
            "runtime_abort_requested": False,
            "execution_started": False,
            "process_started": False,
            "permission_granting": False,
            "authority_granting": False,
        }

    def fork_session(self, session_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_initialized()
        body = body or {}
        child = self.store.fork_session(
            session_id,
            message_id=_optional_text(body, "message_id"),
            title=_optional_text(body, "title"),
            metadata=dict(body.get("metadata") or {}),
        )
        return {
            "schema_version": "harness.session_fork/v1",
            "ok": True,
            "session_id": session_id,
            "child_session_id": child.id,
            "child": child.model_dump(mode="json"),
            "session": child.model_dump(mode="json"),
            "execution_started": False,
            "process_started": False,
            "permission_granting": False,
            "authority_granting": False,
        }

    def hard_delete_session(self, session_id: str) -> dict[str, Any]:
        self._require_initialized()
        counts = self.store.hard_delete_session(session_id)
        return {
            "schema_version": "harness.session_hard_delete/v1",
            "ok": True,
            "session_id": session_id,
            "counts": counts,
            "deletion_counts": counts,
            "hard_deleted": True,
            "active_repo_modified": False,
            "process_started": False,
            "filesystem_modified": True,
            "permission_granting": False,
            "authority_granting": False,
        }

    def update_session_title(self, session_id: str, title: str | None) -> dict[str, Any]:
        self._require_initialized()
        session = self.store.update_session_title(session_id, title)
        return {
            "schema_version": "harness.session_update/v1",
            "ok": True,
            "session": session.model_dump(mode="json"),
            "title_updated": True,
            "model_updated": False,
            "agent_updated": False,
            "messages_mutated": False,
            "parts_mutated": False,
            "execution_started": False,
            "process_started": False,
            "permission_granting": False,
            "authority_granting": False,
            "no_hidden_fallback": True,
        }

    def update_session_agent(self, session_id: str, agent_id: str | None, *, source: str = "app_service") -> dict[str, Any]:
        self._require_initialized()
        session = self.store.update_session(session_id, agent_id=agent_id)
        event = self.store.append_store_event(
            EventStreamType.SESSION,
            session.id,
            "agent.selected",
            {
                "agent_id": agent_id,
                "source": source,
                "process_started": False,
                "permission_granting": False,
            },
            session_id=session.id,
            redaction_state=RedactionState.NOT_REQUIRED,
        )
        return {
            "schema_version": "harness.session_update/v1",
            "ok": True,
            "session": session.model_dump(mode="json"),
            "event": event.model_dump(mode="json"),
            "title_updated": False,
            "model_updated": False,
            "agent_updated": True,
            "messages_mutated": False,
            "parts_mutated": False,
            "execution_started": False,
            "process_started": False,
            "permission_granting": False,
            "authority_granting": False,
            "no_hidden_fallback": True,
        }

    def update_session_model_selection(
        self,
        session_id: str,
        raw_model_ref: str,
        *,
        source: str = "app_service_model_picker",
    ) -> dict[str, Any]:
        self._require_initialized()
        clean_ref = str(raw_model_ref or "").strip()
        if not clean_ref:
            raise ValueError("Missing model ref.")
        cfg = load_config(self.project_root)
        validation = validate_model_selection(
            cfg,
            clean_ref,
            provider_accounts=self.store.list_provider_accounts(),
        )
        validation_payload = validation.model_dump(mode="json")
        if not validation.executable:
            suggestions = (
                build_model_provider_suggestions(
                    cfg,
                    clean_ref,
                    provider_accounts=self.store.list_provider_accounts(),
                    model_overlays=list_cached_discovered_models(cfg, self.store),
                )
                if any(
                    reason in {"provider_unknown", "provider_not_specified", "model_unknown", "variant_unknown"}
                    for reason in validation.blocked_reasons
                )
                else None
            )
            if suggestions is not None:
                validation_payload["suggestions"] = suggestions["model_suggestions"]
                validation_payload["model_suggestions"] = suggestions["model_suggestions"]
                validation_payload["provider_suggestions"] = suggestions["provider_suggestions"]
            event = self._append_model_validation_event(
                session_id,
                validation_payload,
                source=source,
            )
            return {
                "schema_version": "harness.session_update/v1",
                "ok": False,
                "session": self.store.get_session(session_id).model_dump(mode="json"),
                "raw_model_ref": clean_ref,
                "model_validation": validation_payload,
                "blocked_reasons": validation.blocked_reasons,
                "suggestions": suggestions["model_suggestions"] if suggestions is not None else [],
                "model_suggestions": suggestions["model_suggestions"] if suggestions is not None else [],
                "provider_suggestions": suggestions["provider_suggestions"] if suggestions is not None else [],
                "suggestion_only": suggestions is not None,
                "session_model_selected": False,
                "session_event_persisted": True,
                "event": event.model_dump(mode="json"),
                "title_updated": False,
                "model_updated": False,
                "messages_mutated": False,
                "parts_mutated": False,
                "execution_started": False,
                "process_started": False,
                "provider_execution_started": False,
                "model_execution_started": False,
                "network_accessed": False,
                "hidden_provider_fallback": False,
                "hidden_model_fallback": False,
                "no_hidden_fallback": True,
                "permission_granting": False,
                "authority_granting": False,
            }
        session = self.store.update_session_model(
            session_id,
            raw_model_ref=clean_ref,
            provider_id=validation.provider_id,
            model_id=validation.model_id,
            model_variant=validation.variant,
        )
        self.store.record_model_selection(
            raw_model_ref=clean_ref,
            provider_id=validation.provider_id,
            model_id=validation.model_id,
            model_variant=validation.variant,
            last_reasoning_effort=validation.resolved_model_selection.resolved_reasoning_effort
            if validation.resolved_model_selection is not None
            else None,
            source=source,
            metadata={"session_id": session.id},
        )
        event = self._append_model_validation_event(
            session.id,
            validation_payload,
            source=source,
        )
        return {
            "schema_version": "harness.session_update/v1",
            "ok": True,
            "session": session.model_dump(mode="json"),
            "raw_model_ref": clean_ref,
            "model_validation": validation_payload,
            "blocked_reasons": validation.blocked_reasons,
            "session_model_selected": True,
            "session_event_persisted": True,
            "event": event.model_dump(mode="json"),
            "title_updated": False,
            "model_updated": True,
            "messages_mutated": False,
            "parts_mutated": False,
            "execution_started": False,
            "process_started": False,
            "provider_execution_started": False,
            "model_execution_started": False,
            "network_accessed": False,
            "hidden_provider_fallback": False,
            "hidden_model_fallback": False,
            "no_hidden_fallback": True,
            "permission_granting": False,
            "authority_granting": False,
        }

    def append_message(self, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_initialized()
        self.store.get_session(session_id)
        content = _message_content_from_body(body)
        if not content:
            raise ValueError("Missing required message content.")
        role = _optional_text(body, "role") or SessionMessageRole.USER.value
        agent_id = _optional_text(body, "agent_id") or _optional_text(body, "agent")
        message = self.store.append_session_message(
            session_id,
            SessionMessageRole(role),
            content,
            agent_id=agent_id,
        )
        part = self.store.append_session_part(
            session_id,
            message.id,
            SessionPartKind.TEXT,
            text=content,
            metadata={
                "source": _optional_text(body, "source") or "app_service",
                **(dict(body.get("metadata") or {}) if isinstance(body.get("metadata"), dict) else {}),
            },
            redaction_state=RedactionState.REDACTED,
        )
        return {
            "schema_version": "harness.session_message_append/v1",
            "ok": True,
            "session_id": session_id,
            "message": message.model_dump(mode="json"),
            "part": part.model_dump(mode="json"),
            "messages_mutated": True,
            "parts_mutated": True,
            "execution_started": False,
            "process_started": False,
            "provider_execution_started": False,
            "model_execution_started": False,
            "permission_granting": False,
            "authority_granting": False,
            "no_hidden_fallback": True,
        }

    def prompt_async(self, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._submit_prompt(session_id, body, mode="async")

    def submit_prompt(self, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._submit_prompt(session_id, body, mode="sync")

    def sessions_status(self) -> dict[str, Any]:
        if not self.initialized:
            return self._uninitialized_payload(
                "harness.sessions_status/v1",
                status_by_session={},
                sessions=[],
                active_session_ids=[],
                session_count=0,
            )
        sessions = self.store.list_sessions()
        runtime_manager = self._runtime_manager()
        active_session_ids = [
            session.id
            for session in sessions
            if session.status
            not in {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED, SessionStatus.ARCHIVED}
        ]
        return {
            "schema_version": "harness.sessions_status/v1",
            "ok": True,
            "status_by_session": {session.id: session.status.value for session in sessions},
            "sessions": [
                {
                    "session_id": session.id,
                    "status": session.status.value,
                    "active_run_id": session.active_run_id,
                    "active_task_id": session.active_task_id,
                    "runtime": runtime_manager.status(session.id).model_dump(mode="json"),
                    "updated_at": session.updated_at.isoformat(),
                }
                for session in sessions
            ],
            "active_session_ids": active_session_ids,
            "session_count": len(sessions),
            "execution_started": False,
            "permission_granting": False,
        }

    def session_status(self, session_id: str) -> dict[str, Any]:
        self._require_initialized()
        session = self.store.get_session(session_id)
        events = self.store.list_session_store_events(session.id)
        messages = self.store.list_session_messages(session.id)
        children = self.store.list_child_sessions(session.id)
        runtime = self._runtime_manager().status(session.id)
        try:
            cwd = session_cwd_payload(
                self.project_root,
                session.metadata,
                load_config(self.project_root).context_excludes,
            )
        except Exception:
            cwd = {"cwd": session.metadata.get("cwd", "."), "resolved_abs_path": None}
        return {
            "schema_version": "harness.session_status/v1",
            "ok": True,
            "session_id": session.id,
            "status": session.status.value,
            "title": session.title,
            "active_run_id": session.active_run_id,
            "active_task_id": session.active_task_id,
            "objective_id": session.objective_id,
            "summary": session.summary,
            "token_input": session.token_input,
            "token_output": session.token_output,
            "token_reasoning": session.token_reasoning,
            "token_cache_read": session.token_cache_read,
            "token_cache_write": session.token_cache_write,
            "estimated_cost_usd": str(session.estimated_cost_usd) if session.estimated_cost_usd is not None else None,
            "message_count": len(messages),
            "event_count": len(events),
            "cwd": cwd,
            "planning_mode": session_planning_mode_projection(session.metadata),
            "operator": session_operator_status_projection(
                self.store,
                session.id,
                project_root=self.project_root,
                cwd=str(cwd.get("cwd") or "."),
                active_tools=self._operator_active_tools(),
            ),
            "runtime": runtime.model_dump(mode="json"),
            "child_session_ids": [child.id for child in children],
            "latest_ui_activation": self.latest_session_ui_activation(session.id),
            "terminal": session.status
            in {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED, SessionStatus.ARCHIVED},
            "process_running": runtime.process_running,
            "permission_granting": False,
        }

    def list_messages(self, session_id: str, *, limit: int | None = None) -> dict[str, Any]:
        self._require_initialized()
        self.store.get_session(session_id)
        messages = self.store.list_session_messages(session_id)
        if limit is not None:
            messages = messages[-limit:] if limit else []
        parts_by_message = {
            message.id: self.store.list_session_parts(session_id, message.id)
            for message in messages
        }
        return {
            "schema_version": "harness.session_messages/v1",
            "ok": True,
            "session_id": session_id,
            "limit": limit,
            "messages": [message.model_dump(mode="json") for message in messages],
            "parts": {
                message_id: [part.model_dump(mode="json") for part in message_parts]
                for message_id, message_parts in parts_by_message.items()
            },
            "execution_started": False,
            "permission_granting": False,
        }

    def message_detail(self, session_id: str, message_id: str) -> dict[str, Any]:
        self._require_initialized()
        self.store.get_session(session_id)
        message = next(
            (candidate for candidate in self.store.list_session_messages(session_id) if candidate.id == message_id),
            None,
        )
        if message is None:
            raise KeyError(f"Session message not found: {message_id}")
        parts = self.store.list_session_parts(session_id, message_id)
        return {
            "schema_version": "harness.session_message/v1",
            "ok": True,
            "session_id": session_id,
            "message_id": message_id,
            "message": message.model_dump(mode="json"),
            "parts": [part.model_dump(mode="json") for part in parts],
            "execution_started": False,
            "permission_granting": False,
        }

    def list_events(
        self,
        session_id: str,
        *,
        after_seq: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        self._require_initialized()
        self.store.get_session(session_id)
        events = self.store.list_store_events(
            EventStreamType.SESSION,
            session_id,
            after_seq=after_seq,
            limit=limit,
        )
        return {
            "schema_version": "harness.session_events/v1",
            "ok": True,
            "session_id": session_id,
            "after_seq": after_seq,
            "limit": limit,
            "events": [event.model_dump(mode="json") for event in events],
            "execution_started": False,
            "permission_granting": False,
        }

    def runtime_status(self, session_id: str) -> dict[str, Any]:
        self._require_initialized()
        runtime = self._runtime_manager().status(session_id)
        return {
            "schema_version": "harness.session_runtime_status/v1",
            "ok": True,
            "session_id": session_id,
            "runtime": runtime.model_dump(mode="json"),
            "execution_started": False,
            "permission_granting": False,
        }

    def list_permissions(self, session_id: str | None = None) -> dict[str, Any]:
        self._require_initialized()
        if session_id is None:
            permissions: list[Any] = []
            for session in self.store.list_sessions():
                permissions.extend(
                    self.store.list_session_permissions(session.id, status=SessionPermissionStatus.PENDING)
                )
            return {
                "schema_version": "harness.global_permissions/v1",
                "ok": True,
                "permissions": self._session_permission_payloads(permissions),
                "approval_cards": self._approval_cards_for_permissions(permissions),
                "pending_count": len(permissions),
                "execution_started": False,
                "permission_granting": False,
            }
        self.store.get_session(session_id)
        permissions = self.store.list_session_permissions(session_id)
        return {
            "schema_version": "harness.session_permissions/v1",
            "ok": True,
            "session_id": session_id,
            "permissions": self._session_permission_payloads(permissions),
            "snapshot": self._session_permission_snapshot_payload(session_id, permissions),
            "permission_granting": False,
        }

    def reply_permission(self, session_id: str, permission_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_initialized()
        self.store.get_session(session_id)
        existing = self.store.get_session_permission(permission_id)
        if existing.session_id != session_id:
            raise ValueError(f"Permission {permission_id} does not belong to session {session_id}.")
        approval_card = build_session_approval_card(self.store, session_id, permission_id)
        reply = _optional_text(body, "reply") or _optional_text(body, "response")
        decision = _optional_text(body, "decision") or _optional_text(body, "status")
        status = _permission_reply_status(reply, decision)
        reason = _optional_text(body, "reason") or _optional_text(body, "message")
        permission = self.store.resolve_session_permission(
            permission_id,
            status,
            source=SessionPermissionSource.USER,
            reason=reason,
        )
        runtime_resolution = self._runtime_manager().permission_resolved(
            session_id,
            permission_id,
            decision=status.value,
            resumed=False,
        )
        permissions = self.store.list_session_permissions(session_id)
        snapshot = self._session_permission_snapshot_payload(session_id, permissions)
        return {
            "schema_version": "harness.session_permission_reply/v1",
            "ok": True,
            "session_id": session_id,
            "permission_id": permission_id,
            "reply": reply,
            "decision": status.value,
            "permission": permission.model_dump(mode="json"),
            "approval_card": approval_card,
            "runtime": runtime_resolution.model_dump(mode="json"),
            "snapshot": snapshot,
            "execution_started": False,
            "tool_execution_started": False,
            "scope_broadened": False,
            "permission_granting": status == SessionPermissionStatus.ALLOWED,
            "authority_granting": False,
        }

    def list_questions(self, session_id: str | None = None) -> dict[str, Any]:
        self._require_initialized()
        if session_id is None:
            questions: list[Any] = []
            for session in self.store.list_sessions():
                questions.extend(
                    part
                    for part in self.store.list_session_parts(session.id)
                    if part.kind == SessionPartKind.QUESTION
                )
            return {
                "schema_version": "harness.global_questions/v1",
                "ok": True,
                "questions": [part.model_dump(mode="json") for part in questions],
                "pending_count": len(questions),
                "execution_started": False,
                "permission_granting": False,
            }
        self.store.get_session(session_id)
        questions = [
            part
            for part in self.store.list_session_parts(session_id)
            if part.kind == SessionPartKind.QUESTION
        ]
        return {
            "schema_version": "harness.session_questions/v1",
            "ok": True,
            "session_id": session_id,
            "questions": [part.model_dump(mode="json") for part in questions],
            "execution_started": False,
            "permission_granting": False,
        }

    def settings_tui(self, session_id: str | None = None) -> dict[str, Any]:
        preferences: dict[str, Any] | None = None
        source = "defaults"
        if session_id is not None and self.initialized:
            try:
                session = self.store.get_session(session_id)
                preferences = session.ui_preferences
                source = "active_session"
            except KeyError:
                preferences = None
        catalog = build_tui_settings_catalog(preferences, source=source, session_id=session_id)
        return {
            **catalog,
            "execution_started": False,
            "permission_granting": False,
        }

    def provider_auth_methods(self) -> dict[str, Any]:
        cfg = load_config(self.project_root)
        return provider_auth_methods_projection(cfg, self.store)

    def list_providers(self) -> dict[str, Any]:
        from harness.local_server import _provider_catalog_projection

        cfg = load_config(self.project_root)
        return _provider_catalog_projection(self.store, cfg)

    def provider_detail(self, provider_id: str) -> dict[str, Any]:
        from harness.local_server import _provider_catalog_projection

        cfg = load_config(self.project_root)
        return _provider_catalog_projection(self.store, cfg, provider_id=provider_id)

    def list_models(self) -> dict[str, Any]:
        from harness.local_server import _model_catalog_projection

        cfg = load_config(self.project_root)
        return _model_catalog_projection(self.store, cfg)

    def model_detail(self, provider_id: str, model_id: str) -> dict[str, Any]:
        from harness.local_server import _model_detail_projection

        cfg = load_config(self.project_root)
        return _model_detail_projection(self.store, cfg, provider_id=provider_id, model_id=model_id)

    def validate_model(self, raw_model_ref: str) -> dict[str, Any]:
        from harness.local_server import _model_selection_validation_projection

        cfg = load_config(self.project_root)
        return _model_selection_validation_projection(self.store, cfg, raw_model_ref)

    def model_preferences(self) -> dict[str, Any]:
        from harness.local_server import _model_preferences_projection

        return _model_preferences_projection(self.store)

    def connect_provider_api_key(
        self,
        provider_id: str,
        api_key: str,
        *,
        description: str = "default",
        active: bool = True,
    ) -> dict[str, Any]:
        self._require_initialized()
        cfg = load_config(self.project_root)
        return connect_provider_api_key(
            self.project_root,
            self.store,
            cfg,
            provider_id,
            api_key,
            description=description,
            active=active,
        )

    def connect_provider_env(
        self,
        provider_id: str,
        env_var: str,
        *,
        description: str = "default",
        active: bool = True,
    ) -> dict[str, Any]:
        self._require_initialized()
        cfg = load_config(self.project_root)
        return connect_provider_env(
            self.store,
            cfg,
            provider_id,
            env_var,
            description=description,
            active=active,
        )

    def connect_provider_local_account(
        self,
        provider_id: str,
        credential_kind: str,
        *,
        description: str = "default",
        active: bool = True,
        env_var: str | None = None,
    ) -> dict[str, Any]:
        self._require_initialized()
        cfg = load_config(self.project_root)
        return connect_provider_local_account(
            self.store,
            cfg,
            provider_id,
            credential_kind,
            description=description,
            active=active,
            env_var=env_var,
        )

    def activate_provider_account(self, provider_id: str, account_id: str) -> dict[str, Any]:
        self._require_initialized()
        cfg = load_config(self.project_root)
        return activate_provider_auth_account(self.store, cfg, provider_id, account_id)

    def disconnect_provider(self, provider_id: str) -> dict[str, Any]:
        self._require_initialized()
        cfg = load_config(self.project_root)
        return disconnect_provider_auth(self.store, cfg, provider_id)

    def set_model_favorite(self, raw_model_ref: str, favorite: bool) -> dict[str, Any]:
        self._require_initialized()
        validation = self._validate_model_preference_ref(raw_model_ref)
        preference = self.store.set_model_favorite(
            raw_model_ref,
            favorite,
            provider_id=validation.provider_id,
            model_id=validation.model_id,
            model_variant=validation.variant,
            source="tui_models_favorite_action" if favorite else "tui_models_unfavorite_action",
        )
        return self._model_preference_update_payload("favorite" if favorite else "unfavorite", preference, validation.model_dump(mode="json"))

    def set_default_model_preference(self, raw_model_ref: str) -> dict[str, Any]:
        self._require_initialized()
        validation = self._validate_model_preference_ref(raw_model_ref)
        preference = self.store.set_default_model_preference(
            raw_model_ref,
            provider_id=validation.provider_id,
            model_id=validation.model_id,
            model_variant=validation.variant,
            source="tui_models_default_action",
        )
        return self._model_preference_update_payload("default", preference, validation.model_dump(mode="json"))

    def inspect_model(self, raw_model_ref: str) -> dict[str, Any]:
        self._require_initialized()
        cfg = load_config(self.project_root)
        validation = validate_model_selection(
            cfg,
            raw_model_ref,
            model_overlays=list_cached_discovered_models(cfg, self.store),
            provider_accounts=self.store.list_provider_accounts(),
        )
        return {
            "schema_version": "harness.model_inspection/v1",
            "ok": True,
            "raw_model_ref": raw_model_ref,
            "validation": validation.model_dump(mode="json"),
            "model": validation.matched_model.model_dump(mode="json") if validation.matched_model is not None else None,
            "provider_execution_started": False,
            "model_execution_started": False,
            "network_accessed": False,
            "credentials_included": False,
            "hidden_provider_fallback": False,
            "hidden_model_fallback": False,
            "no_hidden_fallback": True,
            "permission_granting": False,
            "authority_granting": False,
        }

    def refresh_provider_models(
        self,
        provider_id: str,
        *,
        approve_hosted: bool = False,
        with_credentials: bool = False,
    ) -> dict[str, Any]:
        self._require_initialized()
        cfg = load_config(self.project_root)
        try:
            result = refresh_model_discovery(
                cfg,
                provider_id,
                store=self.store,
                approve_hosted=approve_hosted,
                with_credentials=with_credentials,
            )
            return result.model_dump(mode="json")
        except ModelDiscoveryError as exc:
            return {
                "schema_version": "harness.model_discovery_result/v1",
                "ok": False,
                "provider_id": exc.provider_id,
                "error": str(exc),
                "blocked_reasons": exc.blocked_reasons,
                "source": "discovered",
                "network_accessed": False,
                "credentials_included": False,
                "credential_written": False,
                "provider_execution_started": False,
                "model_execution_started": False,
                "hidden_provider_fallback": False,
                "hidden_model_fallback": False,
                "no_hidden_fallback": True,
                "permission_granting": False,
                "authority_granting": False,
            }

    def provider_oauth_authorize(self, provider_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_initialized()
        cfg = load_config(self.project_root)
        return provider_oauth_authorize(cfg, provider_id, body or {})

    def provider_oauth_callback(self, provider_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._require_initialized()
        cfg = load_config(self.project_root)
        return provider_oauth_callback(self.project_root, self.store, cfg, provider_id, body)

    def _validate_model_preference_ref(self, raw_model_ref: str):
        clean_ref = str(raw_model_ref or "").strip()
        if not clean_ref:
            raise ValueError("Missing model ref.")
        cfg = load_config(self.project_root)
        validation = validate_model_selection(
            cfg,
            clean_ref,
            model_overlays=list_cached_discovered_models(cfg, self.store),
            provider_accounts=self.store.list_provider_accounts(),
        )
        if validation.matched_model is None:
            raise ValueError(f"Model ref is not present in the local catalog: {clean_ref}")
        return validation

    @staticmethod
    def _model_preference_update_payload(action: str, preference: dict[str, Any], validation: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "harness.model_preference_update/v1",
            "ok": True,
            "action": action,
            "preference": preference,
            "validation": validation,
            "provider_execution_started": False,
            "model_execution_started": False,
            "network_accessed": False,
            "credentials_included": False,
            "credential_written": False,
            "hidden_provider_fallback": False,
            "hidden_model_fallback": False,
            "no_hidden_fallback": True,
            "permission_granting": False,
            "authority_granting": False,
        }

    def subscribe_session_events(
        self,
        session_id: str,
        *,
        after_seq: int | None = None,
    ) -> EventSubscription:
        self._require_initialized()
        self.store.get_session(session_id)
        return subscribe_store_events(self.store, EventStreamType.SESSION, session_id, after_seq=after_seq)

    def subscribe_global_events(self) -> EventSubscription:
        return subscribe_global_events(self.project_root)

    def latest_session_ui_activation(self, session_id: str) -> dict[str, Any] | None:
        events = self.store.list_session_store_events(session_id)
        event = next((item for item in reversed(events) if item.kind == "tui.ui_activation.applied"), None)
        if event is None:
            return None
        payload = event.payload or {}
        action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
        return {
            "seq": event.seq,
            "event_id": event.id,
            "entry_id": payload.get("entry_id"),
            "source": payload.get("source"),
            "activation_kind": payload.get("activation_kind"),
            "action_type": action.get("type"),
            "evidence_status": payload.get("evidence_status") or "ui_only_persisted",
            "policy_boundary": payload.get("policy_boundary")
            or {
                "kind": "safe_ui_activation",
                "ui_state_only": True,
                "command_execution_allowed": False,
                "process_start_allowed": False,
                "filesystem_mutation_allowed": False,
                "permission_grant_allowed": False,
                "authority_grant_allowed": False,
            },
            "blocked_reasons": payload.get("blocked_reasons") or [],
            "ui_action_applied": bool(payload.get("ui_action_applied")),
            "command_started": bool(payload.get("command_started")),
            "process_started": bool(payload.get("process_started")),
            "filesystem_modified": bool(payload.get("filesystem_modified")),
            "provider_id": payload.get("provider_id"),
            "method": payload.get("method"),
            "account_id": payload.get("account_id"),
            "account_created": bool(payload.get("account_created")),
            "account_activated": bool(payload.get("account_activated")),
            "credential_source": payload.get("credential_source"),
            "credential_value_included": bool(payload.get("credential_value_included")),
            "credentials_included": bool(payload.get("credentials_included")),
            "provider_execution_started": bool(payload.get("provider_execution_started")),
            "model_execution_started": bool(payload.get("model_execution_started")),
            "network_accessed": bool(payload.get("network_accessed")),
            "active_model_changed": bool(payload.get("active_model_changed")),
            "permission_granting": bool(payload.get("permission_granting")),
            "authority_granting": bool(payload.get("authority_granting")),
        }

    def _runtime_manager(self) -> SessionRuntimeManager:
        if self.runtime is not None:
            return self.runtime
        provider_adapter = build_default_provider_adapter(self.project_root) if self.execution_enabled else None
        self.runtime = SessionRuntimeManager.for_store(
            self.store,
            provider_adapter=provider_adapter,
            execution_enabled=self.execution_enabled,
        )
        return self.runtime

    def _submit_prompt(self, session_id: str, body: dict[str, Any], *, mode: str) -> dict[str, Any]:
        self._require_initialized()
        session = self.store.get_session(session_id)
        terminal_statuses = {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED, SessionStatus.ARCHIVED}
        if session.status in terminal_statuses:
            runtime = self._runtime_manager().status(session.id)
            return {
                "schema_version": f"harness.session_prompt_{mode}/v1",
                "ok": False,
                "accepted": False,
                "session_id": session.id,
                "session": session.model_dump(mode="json"),
                "error_code": "session_terminal",
                "error": f"Session is terminal: {session.status.value}.",
                "guidance": "Fork this session or create a new session before submitting another prompt.",
                "message": None,
                "part": None,
                "runtime": {
                    "schema_version": "harness.session_prompt_accepted/v1",
                    "ok": False,
                    "accepted": False,
                    "session_id": session.id,
                    "prompt_id": None,
                    "queued": False,
                    "queue_policy": SessionPromptQueuePolicy.FOLLOW_UP.value,
                    "phase": runtime.phase.value,
                    "reason": f"Session is terminal: {session.status.value}.",
                    "execution_started": False,
                    "worker_started": False,
                    "runtime": runtime.model_dump(mode="json"),
                },
                "prompt_id": None,
                "async_accepted": False,
                "waited_for_response": False,
                "assistant_response_started": False,
                "messages_mutated": False,
                "parts_mutated": False,
                "execution_started": False,
                "process_started": False,
                "provider_execution_started": False,
                "model_execution_started": False,
                "permission_granting": False,
                "authority_granting": False,
                "no_hidden_fallback": True,
            }
        content = _message_content_from_body(body)
        if not content:
            raise ValueError("Missing required prompt content.")
        agent_id = _optional_text(body, "agent_id") or _optional_text(body, "agent") or session.agent_id
        model_ref = _body_model_ref(body)
        message_payload = self.append_message(
            session.id,
            {
                "content": content,
                "role": SessionMessageRole.USER.value,
                "agent_id": agent_id,
                "source": _optional_text(body, "source") or f"app_service_prompt_{mode}",
                "metadata": {
                    "prompt_mode": mode,
                    "model_ref": model_ref,
                    "session_model_ref": session.raw_model_ref,
                    **(dict(body.get("metadata") or {}) if isinstance(body.get("metadata"), dict) else {}),
                },
            },
        )
        queue_policy = _queue_policy_from_body(body)
        runtime_acceptance = self._runtime_manager().submit_prompt(
            SessionPromptRequest(
                session_id=session.id,
                content=content,
                mode=mode,  # type: ignore[arg-type]
                queue_policy=queue_policy,
                agent_id=agent_id,
                model_ref=model_ref,
                message_id=message_payload["message"]["id"],
                part_id=message_payload["part"]["id"],
                metadata={
                    "source": _optional_text(body, "source") or f"app_service_prompt_{mode}",
                    "tui_submit": bool(body.get("tui_submit")),
                    **(dict(body.get("runtime_metadata") or {}) if isinstance(body.get("runtime_metadata"), dict) else {}),
                },
            )
        )
        return {
            **message_payload,
            "schema_version": f"harness.session_prompt_{mode}/v1",
            "ok": runtime_acceptance.ok,
            "accepted": runtime_acceptance.accepted,
            "session_id": session.id,
            "runtime": runtime_acceptance.model_dump(mode="json"),
            "prompt_id": runtime_acceptance.prompt_id,
            "async_accepted": mode == "async" and runtime_acceptance.accepted,
            "waited_for_response": False,
            "assistant_response_started": False,
            "messages_mutated": True,
            "parts_mutated": True,
            "execution_started": runtime_acceptance.execution_started,
            "process_started": runtime_acceptance.worker_started,
            "provider_execution_started": runtime_acceptance.execution_started,
            "model_execution_started": runtime_acceptance.execution_started,
            "permission_granting": False,
            "authority_granting": False,
            "no_hidden_fallback": True,
        }

    def _require_initialized(self) -> None:
        if not self.initialized:
            raise FileNotFoundError(f"Harness project is not initialized: {self.project_root}")

    def _uninitialized_payload(self, schema_version: str, **fields: Any) -> dict[str, Any]:
        return {
            "schema_version": schema_version,
            "ok": False,
            "project_root": str(self.project_root),
            "error_code": "project_uninitialized",
            "error": "Harness project is not initialized.",
            "execution_started": False,
            "permission_granting": False,
            **fields,
        }

    def _session_permission_payloads(self, permissions: list[Any]) -> list[dict[str, Any]]:
        payloads = []
        for permission in permissions:
            payload = permission.model_dump(mode="json")
            try:
                payload["approval_card"] = build_session_approval_card(self.store, permission.session_id, permission.id)
            except Exception:
                payload["approval_card"] = None
            payloads.append(payload)
        return payloads

    def _approval_cards_for_permissions(self, permissions: list[Any]) -> list[dict[str, Any]]:
        cards = []
        for permission in permissions:
            try:
                cards.append(build_session_approval_card(self.store, permission.session_id, permission.id))
            except Exception:
                continue
        return cards

    def _session_permission_snapshot_payload(self, session_id: str, permissions: list[Any]) -> dict[str, Any]:
        counts = {status.value: 0 for status in SessionPermissionStatus}
        pending_ids: list[str] = []
        approval_cards: list[dict[str, Any]] = []
        for permission in permissions:
            status = permission.status.value
            counts[status] = counts.get(status, 0) + 1
            if permission.status == SessionPermissionStatus.PENDING:
                pending_ids.append(permission.id)
                try:
                    approval_cards.append(build_session_approval_card(self.store, session_id, permission.id))
                except Exception:
                    pass
        return {
            "session_id": session_id,
            "counts": counts,
            "pending_permission_ids": pending_ids,
            "approval_cards": approval_cards,
            "pending_approval_cards": approval_cards,
            "pending_count": len(pending_ids),
            "blocked_on_permission": bool(pending_ids),
            "reply_route": "/sessions/{session_id}/permissions/{permission_id}/reply",
            "approval_route": "/sessions/{session_id}/approval/{approval_id}",
            "execution_started": False,
        }

    def _append_model_validation_event(
        self,
        session_id: str,
        validation_payload: dict[str, Any],
        *,
        source: str,
    ):
        return self.store.append_store_event(
            EventStreamType.SESSION,
            session_id,
            "session.model_validation",
            {
                **validation_payload,
                "source": source,
                "summary": "Model selection validated."
                if validation_payload.get("executable")
                else "Model selection blocked before execution.",
                "provider_execution_started": False,
                "model_execution_started": False,
                "network_accessed": False,
                "hidden_provider_fallback": False,
                "hidden_model_fallback": False,
                "no_hidden_fallback": True,
                "permission_granting": False,
                "authority_granting": False,
            },
            session_id=session_id,
            redaction_state=RedactionState.NOT_REQUIRED,
        )

    def _operator_active_tools(self) -> list[str]:
        from harness.session_tools import default_session_tool_descriptors

        return sorted(descriptor.id for descriptor in default_session_tool_descriptors() if descriptor.enabled)


def _optional_text(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _message_content_from_body(body: dict[str, Any]) -> str | None:
    direct = _optional_text(body, "content") or _optional_text(body, "text")
    if direct:
        return direct
    prompt = body.get("prompt")
    if isinstance(prompt, str):
        text = prompt.strip()
        return text or None
    if isinstance(prompt, dict):
        nested = _message_content_from_body(prompt)
        if nested:
            return nested
    parts = body.get("parts")
    if isinstance(parts, list):
        lines: list[str] = []
        for part in parts:
            if isinstance(part, str):
                text = part.strip()
            elif isinstance(part, dict):
                text = str(part.get("text") or part.get("content") or "").strip()
            else:
                text = ""
            if text:
                lines.append(text)
        if lines:
            return "\n".join(lines)
    return None


def _body_model_ref(body: dict[str, Any]) -> str | None:
    raw_model_ref = _optional_text(body, "raw_model_ref") or _optional_text(body, "model")
    if raw_model_ref:
        return raw_model_ref
    model_id = _optional_text(body, "model_id") or _optional_text(body, "modelID")
    provider_id = _optional_text(body, "provider_id") or _optional_text(body, "providerID")
    if model_id and provider_id:
        return f"{provider_id}/{model_id}"
    return model_id


def _queue_policy_from_body(body: dict[str, Any]) -> SessionPromptQueuePolicy:
    raw = _optional_text(body, "queue_policy") or _optional_text(body, "queuePolicy") or SessionPromptQueuePolicy.FOLLOW_UP.value
    try:
        return SessionPromptQueuePolicy(raw)
    except ValueError as exc:
        raise ValueError(f"Unsupported prompt queue policy: {raw}") from exc


def _permission_reply_status(reply: str | None, decision: str | None) -> SessionPermissionStatus:
    if reply:
        normalized = reply.strip().lower()
        if normalized in {"once", "always", "allow", "allowed"}:
            return SessionPermissionStatus.ALLOWED
        if normalized in {"reject", "deny", "denied"}:
            return SessionPermissionStatus.DENIED
        if normalized in {"cancel", "cancelled", "canceled"}:
            return SessionPermissionStatus.CANCELLED
        raise ValueError("Unsupported permission reply. Use once, always, reject, or cancel.")
    if not decision:
        raise ValueError("Missing permission reply or decision.")
    normalized = decision.strip().lower()
    if normalized == "allow":
        normalized = SessionPermissionStatus.ALLOWED.value
    if normalized == "deny":
        normalized = SessionPermissionStatus.DENIED.value
    if normalized == "cancel":
        normalized = SessionPermissionStatus.CANCELLED.value
    return SessionPermissionStatus(normalized)


class CoreEventSummary(BaseModel):
    schema_version: str = "harness.core_event_summary/v1"
    event_id: str
    run_id: str
    task_id: str | None = None
    seq: int | None = None
    event_type: str
    level: str
    message: str
    visibility: str
    redaction_state: str
    created_at: datetime


class CoreRunSummary(BaseModel):
    schema_version: str = "harness.core_summary/v1"
    ok: bool
    mode: str
    decision: str
    status: str
    task_id: str | None = None
    lease_id: str | None = None
    run_id: str | None = None
    adapter_id: str | None = None
    manifest_path: Path | None = None
    event_count: int = 0
    artifact_kinds: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    summary_text: str


class CoreSessionStartResult(BaseModel):
    schema_version: str = "harness.core_session_start/v1"
    ok: bool
    mode: str
    project_root: Path
    goal: str
    session_id: str | None = None
    objective_id: str | None = None
    errors: list[str] = Field(default_factory=list)


class CoreTaskCreationResult(BaseModel):
    schema_version: str = "harness.core_task/v1"
    ok: bool
    mode: str
    project_root: Path
    task_id: str | None = None
    session_id: str | None = None
    objective_id: str | None = None
    adapter_id: str | None = None
    task_type: str | None = None
    status: str | None = None
    errors: list[str] = Field(default_factory=list)


class CoreRunExecutionResult(BaseModel):
    schema_version: str = CORE_SCHEMA_VERSION
    ok: bool
    mode: str
    decision: str
    project_root: Path
    session_id: str | None = None
    objective_id: str | None = None
    task_id: str | None = None
    lease_id: str | None = None
    run_id: str | None = None
    adapter_id: str | None = None
    manifest: Path | None = None
    errors: list[str] = Field(default_factory=list)
    next_commands: list[str] = Field(default_factory=list)
    summary: CoreRunSummary | None = None
    task: CoreTaskCreationResult | None = None


class HarnessCoreService:
    """Headless backend entrypoint for one governed task execution slice."""

    def start_goal(
        self,
        goal: str,
        mode: str,
        project_root: Path,
        output_format: str = "json",
    ) -> CoreRunExecutionResult:
        normalized_mode = self._normalize_mode(mode)
        root = Path(project_root).resolve()
        if normalized_mode not in SUPPORTED_CORE_MODES:
            reason = (
                f"Unsupported core mode: {mode}. "
                f"Supported modes are: {', '.join(sorted(SUPPORTED_CORE_MODES))}."
            )
            return self._closed_result(
                mode=normalized_mode or mode,
                project_root=root,
                decision="unsupported_mode",
                errors=[reason],
            )

        store = SQLiteStore(root)
        store.initialize()
        try:
            session_start = self._start_session_for_goal(store, goal, normalized_mode)
            task_result = self.create_task_for_goal(
                goal=goal,
                mode=normalized_mode,
                project_root=root,
                session_id=session_start.session_id,
                objective_id=session_start.objective_id,
                output_format=output_format,
            )
            if not task_result.ok or task_result.task_id is None:
                return self._closed_result(
                    mode=normalized_mode,
                    project_root=root,
                    decision="task_creation_failed",
                    session_id=session_start.session_id,
                    objective_id=session_start.objective_id,
                    task=task_result,
                    errors=task_result.errors or ["Task creation failed."],
                )
            return self.run_task(
                task_id=task_result.task_id,
                mode=normalized_mode,
                project_root=root,
                session_id=session_start.session_id,
                objective_id=session_start.objective_id,
                task=task_result,
            )
        except Exception as exc:
            reason = str(sanitize_for_logging(str(exc)))
            return self._closed_result(
                mode=normalized_mode,
                project_root=root,
                decision="core_service_failed",
                errors=[reason],
            )

    def create_task_for_goal(
        self,
        *,
        goal: str,
        mode: str,
        project_root: Path,
        session_id: str | None = None,
        objective_id: str | None = None,
        output_format: str = "json",
    ) -> CoreTaskCreationResult:
        root = Path(project_root).resolve()
        normalized_mode = self._normalize_mode(mode)
        if normalized_mode not in SUPPORTED_CORE_MODES:
            return CoreTaskCreationResult(
                ok=False,
                mode=normalized_mode or mode,
                project_root=root,
                errors=[f"Unsupported core mode: {mode}."],
            )
        store = SQLiteStore(root)
        store.initialize()
        adapter_id, task_type = self._adapter_metadata(normalized_mode)
        task = store.create_task(
            title=str(sanitize_for_logging(goal)),
            description="",
            priority=0,
            objective_id=objective_id,
            session_id=session_id,
            metadata={
                "execution_adapter": adapter_id,
                "task_type": task_type,
                "core_service_mode": normalized_mode,
                "core_output_format": output_format,
            },
        )
        return CoreTaskCreationResult(
            ok=True,
            mode=normalized_mode,
            project_root=root,
            task_id=task.id,
            session_id=session_id,
            objective_id=objective_id,
            adapter_id=adapter_id,
            task_type=task_type,
            status=task.status.value,
        )

    def run_task(
        self,
        *,
        task_id: str,
        mode: str,
        project_root: Path,
        session_id: str | None = None,
        objective_id: str | None = None,
        task: CoreTaskCreationResult | None = None,
    ) -> CoreRunExecutionResult:
        root = Path(project_root).resolve()
        normalized_mode = self._normalize_mode(mode)
        store = SQLiteStore(root)
        store.initialize()
        task_record = store.get_task(task_id)
        selection, pause_reasons = store.select_guarded_task_for_lease(
            task_id,
            owner=CORE_OWNER,
            objective_id=task_record.objective_id,
        )
        if pause_reasons:
            eligibility = pause_reasons[0]
            decision = "approval_required" if eligibility["decision"] == "waiting_approval" else eligibility["decision"]
            return self._closed_result(
                mode=normalized_mode,
                project_root=root,
                decision=decision,
                session_id=session_id or task_record.session_id,
                objective_id=objective_id or task_record.objective_id,
                task_id=task_id,
                adapter_id=str(task_record.metadata.get("execution_adapter") or ""),
                task=task,
                errors=[self._task_eligibility_error(eligibility)],
            )
        if selection is None:
            return self._closed_result(
                mode=normalized_mode,
                project_root=root,
                decision="lease_unavailable",
                session_id=session_id or task_record.session_id,
                objective_id=objective_id or task_record.objective_id,
                task_id=task_id,
                adapter_id=str(task_record.metadata.get("execution_adapter") or ""),
                task=task,
                errors=[f"Unable to acquire a lease for task {task_id}."],
            )

        lease = selection["lease"]
        dispatch = execute_lease(root, lease.id, owner=CORE_OWNER)
        run_id = dispatch.run.id if dispatch.run is not None else None
        if session_id is not None and run_id is not None:
            store.attach_session_to_run(session_id, run_id)
            dispatch.run = store.get_run(run_id)
            dispatch.manifest = store.build_run_manifest(run_id)
        manifest_path = self._manifest_path(root, run_id) if run_id is not None else None
        errors = list(dispatch.errors or dispatch.rejection_reasons)
        summary = self._build_summary(
            store=store,
            mode=normalized_mode,
            ok=dispatch.ok,
            decision=dispatch.decision,
            task_id=dispatch.task.id if dispatch.task is not None else task_id,
            lease_id=dispatch.lease.id if dispatch.lease is not None else lease.id,
            run_id=run_id,
            adapter_id=dispatch.adapter_id,
            manifest_path=manifest_path,
            errors=errors,
        )
        return CoreRunExecutionResult(
            ok=dispatch.ok,
            mode=normalized_mode,
            decision=dispatch.decision,
            project_root=root,
            session_id=session_id or (dispatch.run.session_id if dispatch.run is not None else task_record.session_id),
            objective_id=objective_id or task_record.objective_id,
            task_id=dispatch.task.id if dispatch.task is not None else task_id,
            lease_id=dispatch.lease.id if dispatch.lease is not None else lease.id,
            run_id=run_id,
            adapter_id=dispatch.adapter_id,
            manifest=manifest_path,
            errors=errors,
            next_commands=self._next_commands(root, run_id, task_id, dispatch.lease.id if dispatch.lease else lease.id),
            summary=summary,
            task=task,
        )

    def get_run_summary(self, run_id: str, project_root: Path) -> CoreRunSummary:
        root = Path(project_root).resolve()
        store = SQLiteStore(root)
        store.initialize()
        run = store.get_run(run_id)
        task = store.get_task(run.task_id) if run.task_id is not None else None
        lease = self._latest_task_lease(store, task.id) if task is not None else None
        adapter_id = str(task.metadata.get("execution_adapter")) if task is not None else None
        mode = self._mode_from_adapter(adapter_id, run.task_type)
        decision = self._decision_from_run(run)
        errors = [] if run.status in {"completed", "completed_applied", "completed_denied", "completed_no_changes"} else [run.status]
        return self._build_summary(
            store=store,
            mode=mode,
            ok=not errors,
            decision=decision,
            task_id=run.task_id,
            lease_id=lease.id if lease is not None else None,
            run_id=run.id,
            adapter_id=adapter_id,
            manifest_path=self._manifest_path(root, run.id),
            errors=errors,
        )

    def list_run_events(self, run_id: str, project_root: Path) -> list[CoreEventSummary]:
        root = Path(project_root).resolve()
        store = SQLiteStore(root)
        store.initialize()
        return [self._event_summary(event) for event in store.list_events(run_id)]

    def _start_session_for_goal(self, store: SQLiteStore, goal: str, mode: str) -> CoreSessionStartResult:
        adapter_id, task_type = self._adapter_metadata(mode)
        session: SessionSpec | None = store.create_session(
            title=str(sanitize_for_logging(goal))[:120],
            mode=run_mode_for_task_type(task_type).value,
            intent="core_service_goal",
            metadata={
                "core_service": True,
                "core_mode": mode,
                "execution_adapter": adapter_id,
                "task_type": task_type,
            },
        )
        objective: ObjectiveRecord = store.create_objective(
            title=str(sanitize_for_logging(goal)),
            description="Headless core service objective for a single goal.",
            session_id=session.id if session is not None else None,
            metadata={"core_service": True, "core_mode": mode},
        )
        if session is not None:
            store.attach_session_to_objective(session.id, objective.id)
        return CoreSessionStartResult(
            ok=True,
            mode=mode,
            project_root=store.project_root,
            goal=str(sanitize_for_logging(goal)),
            session_id=session.id if session is not None else None,
            objective_id=objective.id,
        )

    def _build_summary(
        self,
        *,
        store: SQLiteStore,
        mode: str,
        ok: bool,
        decision: str,
        task_id: str | None,
        lease_id: str | None,
        run_id: str | None,
        adapter_id: str | None,
        manifest_path: Path | None,
        errors: list[str],
    ) -> CoreRunSummary:
        status = "blocked"
        event_count = 0
        artifact_kinds: list[str] = []
        if run_id is not None:
            try:
                run = store.get_run(run_id)
                status = run.status
                event_count = len(store.list_events(run_id))
                artifact_kinds = sorted({artifact.kind for artifact in store.list_artifacts(run_id)})
            except KeyError:
                status = "missing_run"
        elif ok:
            status = "completed"
        text = (
            f"Core run decision={decision}; status={status}; run_id={run_id or 'none'}; "
            f"task_id={task_id or 'none'}; lease_id={lease_id or 'none'}; "
            f"adapter_id={adapter_id or 'none'}; manifest={manifest_path or 'none'}; "
            f"errors={'; '.join(errors) if errors else 'none'}."
        )
        return CoreRunSummary(
            ok=ok,
            mode=mode,
            decision=decision,
            status=status,
            task_id=task_id,
            lease_id=lease_id,
            run_id=run_id,
            adapter_id=adapter_id,
            manifest_path=manifest_path,
            event_count=event_count,
            artifact_kinds=artifact_kinds,
            errors=errors,
            summary_text=text,
        )

    def _closed_result(
        self,
        *,
        mode: str,
        project_root: Path,
        decision: str,
        errors: list[str],
        session_id: str | None = None,
        objective_id: str | None = None,
        task_id: str | None = None,
        adapter_id: str | None = None,
        task: CoreTaskCreationResult | None = None,
    ) -> CoreRunExecutionResult:
        summary = CoreRunSummary(
            ok=False,
            mode=mode,
            decision=decision,
            status="blocked",
            task_id=task_id,
            adapter_id=adapter_id,
            errors=errors,
            summary_text=(
                f"Core run decision={decision}; status=blocked; run_id=none; "
                f"task_id={task_id or 'none'}; lease_id=none; adapter_id={adapter_id or 'none'}; "
                f"manifest=none; errors={'; '.join(errors) if errors else 'none'}."
            ),
        )
        return CoreRunExecutionResult(
            ok=False,
            mode=mode,
            decision=decision,
            project_root=Path(project_root).resolve(),
            session_id=session_id,
            objective_id=objective_id,
            task_id=task_id,
            adapter_id=adapter_id,
            errors=errors,
            next_commands=self._next_commands(Path(project_root).resolve(), None, task_id, None),
            summary=summary,
            task=task,
        )

    def _task_eligibility_error(self, eligibility: dict[str, Any]) -> str:
        reason = str(sanitize_for_logging(str(eligibility.get("reason") or "Task is not eligible for core execution.")))
        details: list[str] = []
        decision = eligibility.get("decision")
        if decision in {"breaker_open", "control_disabled", "waiting_approval"}:
            details.append(f"decision={decision}")
        adapter_id = eligibility.get("adapter_id")
        if isinstance(adapter_id, str) and adapter_id:
            details.append(f"adapter={adapter_id}")
        task_type = eligibility.get("task_type")
        if isinstance(task_type, str) and task_type:
            details.append(f"task_type={task_type}")
        for key in (
            "missing_approvals",
            "required_approvals",
            "forbidden_policy_keys",
            "blocked_dependency_ids",
        ):
            values = eligibility.get(key)
            if isinstance(values, list) and values:
                details.append(f"{key}={','.join(str(sanitize_for_logging(str(item))) for item in values)}")
        target_kind = eligibility.get("target_kind")
        target_id = eligibility.get("target_id")
        if isinstance(target_kind, str) and isinstance(target_id, str):
            details.append(f"control={target_kind}:{target_id}")
        failure_count = eligibility.get("failure_count")
        threshold = eligibility.get("threshold")
        if isinstance(failure_count, int) and isinstance(threshold, int):
            details.append(f"breaker_failures={failure_count}/{threshold}")
        if details:
            return f"{reason} ({'; '.join(details)})"
        return reason

    def _next_commands(self, project_root: Path, run_id: str | None, task_id: str | None, lease_id: str | None) -> list[str]:
        project = str(project_root)
        commands: list[str] = []
        if run_id is not None:
            commands.extend(
                [
                    f"harness show {run_id} --project {project} --output json",
                    f"harness core inspect-evidence --run {run_id} --project {project} --output json",
                    f"harness core inspect-events {run_id} --project {project} --output json",
                    f"harness events {run_id} --project {project} --jsonl",
                    f"harness artifacts list {run_id} --project {project} --output json",
                ]
            )
        if task_id is not None:
            commands.append(f"harness core inspect-evidence --task {task_id} --project {project} --output json")
            commands.append(f"harness core inspect-task {task_id} --project {project} --output json")
            commands.append(f"harness tasks inspect {task_id} --project {project} --output json")
        if lease_id is not None:
            commands.append(f"harness daemon inspect-lease {lease_id} --project {project} --output json")
        return commands

    def _adapter_metadata(self, mode: str) -> tuple[str, str]:
        if mode == "dry_run":
            return DRY_RUN_EXECUTION_ADAPTER, DRY_RUN_TASK_TYPE
        if mode == "repo_planning":
            return REPO_PLANNING_EXECUTION_ADAPTER, REPO_PLANNING_TASK_TYPE
        if mode == "codex_isolated_edit":
            return CODEX_ISOLATED_EDIT_ADAPTER, CODEX_CODE_EDIT_TASK_TYPE
        raise ValueError(f"Unsupported core mode: {mode}")

    def _mode_from_adapter(self, adapter_id: str | None, task_type: str | None) -> str:
        if adapter_id in SUPPORTED_CORE_MODES:
            return adapter_id
        if task_type == DRY_RUN_TASK_TYPE:
            return "dry_run"
        if task_type == REPO_PLANNING_TASK_TYPE:
            return "repo_planning"
        if task_type == CODEX_CODE_EDIT_TASK_TYPE:
            return "codex_isolated_edit"
        return "unknown"

    def _decision_from_run(self, run: RunRecord) -> str:
        if run.status in {"completed", "completed_applied", "completed_denied", "completed_no_changes"}:
            return f"{run.task_type or 'run'}_completed"
        return f"{run.task_type or 'run'}_{run.status}"

    def _latest_task_lease(self, store: SQLiteStore, task_id: str) -> TaskLease | None:
        leases = store.list_task_leases(task_id)
        return leases[-1] if leases else None

    def _event_summary(self, event: EventRecord) -> CoreEventSummary:
        return CoreEventSummary(
            event_id=event.id,
            run_id=event.run_id,
            task_id=event.task_id,
            seq=event.seq,
            event_type=event.event_type,
            level=event.level,
            message=event.message,
            visibility=event.visibility.value,
            redaction_state=event.redaction_state.value,
            created_at=event.created_at,
        )

    def _manifest_path(self, project_root: Path, run_id: str | None) -> Path | None:
        if run_id is None:
            return None
        path = Path(project_root).resolve() / ".harness" / "runs" / run_id / "manifest.json"
        return path if path.exists() else None

    def _normalize_mode(self, mode: str) -> str:
        return str(mode or "").strip().lower().replace("-", "_")
