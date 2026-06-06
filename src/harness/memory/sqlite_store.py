from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from harness.events import append_jsonl
from harness.models import (
    ArtifactRecord,
    BackendCapabilities,
    BackendConfig,
    BackendDescriptor,
    BackendKind,
    BackendMetadata,
    AdapterBreakerState,
    BreakerStatus,
    ContextProvenanceRecord,
    ContextSourceKind,
    ContextTrustLevel,
    DaemonDryRunResult,
    DaemonEvent,
    DaemonLeaseInspection,
    DaemonRecord,
    DaemonRecoveryResult,
    DaemonStatus,
    DaemonStatusResult,
    DaemonTickResult,
    EventStreamType,
    EventRecord,
    EventVisibility,
    ManifestArtifact,
    MemoryRecord,
    MemoryRedactionState,
    MemoryScopeType,
    MemorySourceKind,
    ObjectiveRecord,
    ObjectiveStatus,
    KillSwitchRecord,
    KillSwitchTargetKind,
    ProjectAgentRecord,
    RunBaselineRecord,
    RunCompareResult,
    RunManifest,
    RunRecord,
    SessionMessageRecord,
    SessionMessageRole,
    SessionMutationReversibility,
    SessionPartKind,
    SessionPartRecord,
    SessionPermissionBoundaryKind,
    SessionPermissionRequest,
    SessionPermissionScope,
    SessionPermissionSource,
    SessionPermissionStatus,
    SessionSpec,
    SessionStatus,
    SessionTodoRecord,
    StoredEventRecord,
    TaskAttempt,
    TaskDependency,
    TaskDependencyType,
    TaskLease,
    TaskLeaseStatus,
    TaskRecord,
    TaskStatus,
    TaskTransitionRecord,
    ToolReplayPolicy,
    PolicyLevel,
    RedactionState,
    RunEventType,
    TokenUsageSnapshot,
    run_mode_for_task_type,
)
from harness.agent_authoring import AgentBundleError, LoadedAgentBundle, agent_bundle_content_sha256, load_agent_bundle
from harness.registry import SpecRegistry, builtin_spec_registry
from harness.spec_loader import preview_agent_effective_policy
from harness.specs import AgentProfileSpec, AgentSpec
from harness.policy import (
    backend_descriptor_sha256,
    effective_policy_sha256,
    resolve_run_effective_policy,
    resolve_task_effective_policy,
)
from harness.sandbox_profiles import sandbox_profile_dict
from harness.security import SecretBlockedError, is_secret_path, redact_secret_text, sanitize_for_logging, scan_text_for_secrets


logger = logging.getLogger(__name__)
from harness.security_explanations import explanations_from_eligibility, explanations_from_security_decision

MUTABLE_RUN_ARTIFACT_KINDS = {"events", "transcript", "procedure", "final_report", "token_usage", "manifest"}

LEGACY_TASK_STATUS_VALUES = {
    "queued": TaskStatus.READY,
    "completed": TaskStatus.SUCCEEDED,
    "canceled": TaskStatus.CANCELLED,
}

SESSION_TODO_STATUSES = {"pending", "in_progress", "completed", "cancelled"}
DAEMON_TASK_PAUSE_DECISIONS = {
    "active_lease",
    "blocked_dependency",
    "breaker_open",
    "control_disabled",
    "policy_forbidden",
    "waiting_approval",
}
SCHEMA_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    (
        "20260516_001_sessions",
        "20260516_001_sessions.sql",
        "Session spine tables and append-only event store.",
    ),
    (
        "20260518_002_context_chunks",
        "20260518_002_context_chunks.sql",
        "Local context chunk cache tables.",
    ),
    (
        "20260518_003_context_vectors",
        "20260518_003_context_vectors.sql",
        "Local derived context vector tables.",
    ),
)
SESSION_SCHEMA_REPAIR_MESSAGE = "The Harness session database is missing required tables. Run: harness doctor --repair"
REQUIRED_SESSION_SCHEMA_TABLES: tuple[str, ...] = (
    "runs",
    "events",
    "artifacts",
    "sessions",
    "session_messages",
    "session_parts",
    "event_store",
    "session_permissions",
)


def is_missing_session_schema_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return isinstance(exc, sqlite3.Error) and "no such table" in message


def _normalize_session_todo_status(status: str) -> str:
    normalized = str(status).strip().lower().replace("-", "_")
    if normalized == "done":
        normalized = "completed"
    if normalized not in SESSION_TODO_STATUSES:
        raise ValueError(f"Unsupported session todo status: {status}")
    return normalized


def _session_local_tool_evidence(tool_id: str) -> dict[str, Any]:
    return {
        "policy_boundary": {
            "kind": "session_local_state",
            "boundary_kind": SessionPermissionBoundaryKind.LOCAL_ONLY.value,
            "source": f"session_{tool_id}",
        },
        "tool_id": tool_id,
        "session_local": True,
        "repository_files_modified": False,
        "filesystem_modified": False,
        "active_repo_modified": False,
        "git_mutation_started": False,
        "process_started": False,
        "network_accessed": False,
        "permission_granting": False,
        "authority_granting": False,
        "blocked_reasons": [],
    }


def _permission_expiry_iso(expires_at: datetime | str | None, scope: SessionPermissionScope) -> str:
    if isinstance(expires_at, datetime):
        value = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(expires_at, str) and expires_at:
        return expires_at
    now = datetime.now(timezone.utc)
    if scope == SessionPermissionScope.SESSION:
        return (now + timedelta(hours=24)).isoformat()
    return (now + timedelta(minutes=15)).isoformat()


def _model_dump_jsonable(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    raise TypeError(f"Catalog cache values must be Pydantic models or dicts, got {type(value).__name__}.")


def _redact_provider_account_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    secret_words = ("secret", "token", "password", "credential", "api_key", "apikey", "authorization")
    for key, value in metadata.items():
        key_text = str(key)
        if any(word in key_text.lower() for word in secret_words):
            redacted[key_text] = "[redacted]"
        else:
            redacted[key_text] = value
    return redacted


TASK_STATUS_QUERY_ALIASES = {
    TaskStatus.READY: ("ready", "queued"),
    TaskStatus.SUCCEEDED: ("succeeded", "completed"),
    TaskStatus.CANCELLED: ("cancelled", "canceled"),
}

DEFAULT_TASK_LEASE_MINUTES = 30
DEFAULT_TASK_LEASE_OWNER = "manual_cli"
DEFAULT_DAEMON_STALE_AFTER_SECONDS = 120
DRY_RUN_EXECUTION_ADAPTER = "dry_run"
DRY_RUN_TASK_TYPE = "phase_1a_test"
READ_ONLY_EXECUTION_ADAPTER = "read_only_summary"
READ_ONLY_TASK_TYPE = "read_only_repo_summary"
ADAPTER_BREAKER_THRESHOLD = 3
ADAPTER_BREAKER_WINDOW_SECONDS = 15 * 60
DAEMON_POLICY_FORBIDDEN_METADATA_KEYS = {
    "daemon_policy_forbidden",
    "requires_active_repo_write",
    "requires_external_network",
    "requires_docker",
    "requires_paid_provider",
    "requires_hosted_boundary",
}
DRY_RUN_FORBIDDEN_METADATA_KEYS = DAEMON_POLICY_FORBIDDEN_METADATA_KEYS | {
    "requires_generic_shell",
    "requires_mcp",
    "requires_a2a",
    "requires_browser",
    "requires_email",
    "requires_calendar",
}
READ_ONLY_FORBIDDEN_METADATA_KEYS = DRY_RUN_FORBIDDEN_METADATA_KEYS

TASK_REPLAY_RECEIPT_SCHEMA_VERSION = "harness.task_replay_receipt/v1"

ALLOWED_TASK_TRANSITIONS = {
    TaskStatus.CREATED: {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.WAITING_APPROVAL},
    TaskStatus.READY: {
        TaskStatus.BLOCKED,
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.LEASED,
        TaskStatus.RUNNING,
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.SKIPPED,
    },
    TaskStatus.BLOCKED: {TaskStatus.READY, TaskStatus.CANCELLED, TaskStatus.SKIPPED},
    TaskStatus.WAITING_APPROVAL: {
        TaskStatus.READY,
        TaskStatus.RUNNING,
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.SKIPPED,
    },
    TaskStatus.LEASED: {
        TaskStatus.RUNNING,
        TaskStatus.READY,
        TaskStatus.BLOCKED,
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.RUNNING: {
        TaskStatus.READY,
        TaskStatus.BLOCKED,
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.CANCELLED,
    },
    TaskStatus.FAILED: {
        TaskStatus.READY,
        TaskStatus.BLOCKED,
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.CANCELLED,
    },
    TaskStatus.SUCCEEDED: set(),
    TaskStatus.CANCELLED: set(),
    TaskStatus.SKIPPED: set(),
}

ALLOWED_OBJECTIVE_TRANSITIONS = {
    ObjectiveStatus.CREATED: {ObjectiveStatus.ACTIVE, ObjectiveStatus.CANCELLED},
    ObjectiveStatus.ACTIVE: {
        ObjectiveStatus.WAITING_APPROVAL,
        ObjectiveStatus.SUSPENDED,
        ObjectiveStatus.RETRYING,
        ObjectiveStatus.COMPLETED,
        ObjectiveStatus.CANCELLED,
        ObjectiveStatus.TIMED_OUT,
    },
    ObjectiveStatus.WAITING_APPROVAL: {
        ObjectiveStatus.ACTIVE,
        ObjectiveStatus.SUSPENDED,
        ObjectiveStatus.CANCELLED,
        ObjectiveStatus.TIMED_OUT,
    },
    ObjectiveStatus.SUSPENDED: {ObjectiveStatus.ACTIVE, ObjectiveStatus.CANCELLED, ObjectiveStatus.TIMED_OUT},
    ObjectiveStatus.RETRYING: {ObjectiveStatus.ACTIVE, ObjectiveStatus.CANCELLED, ObjectiveStatus.TIMED_OUT},
    ObjectiveStatus.COMPLETED: set(),
    ObjectiveStatus.CANCELLED: set(),
    ObjectiveStatus.TIMED_OUT: {ObjectiveStatus.RETRYING},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _catalog_cache_ttl_metadata(refreshed_at: str, ttl_value: Any) -> dict[str, Any]:
    try:
        ttl_seconds = int(ttl_value)
    except (TypeError, ValueError):
        ttl_seconds = 24 * 60 * 60
    if ttl_seconds <= 0:
        ttl_seconds = 24 * 60 * 60
    expires_at = parse_dt(refreshed_at) + timedelta(seconds=ttl_seconds)
    return {
        "cache_refreshed_at": refreshed_at,
        "cache_ttl_seconds": ttl_seconds,
        "cache_expires_at": expires_at.isoformat(),
        "cache_status": "fresh",
    }


def normalize_task_status(status: str | TaskStatus) -> TaskStatus:
    return TaskStatus(status.value if isinstance(status, TaskStatus) else status)


def normalize_objective_status(status: str | ObjectiveStatus) -> ObjectiveStatus:
    return ObjectiveStatus(status.value if isinstance(status, ObjectiveStatus) else status)


def _sort_json(value):
    if isinstance(value, dict):
        return {key: _sort_json(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sort_json(item) for item in value]
    return value


def validate_task_transition(from_status: str | TaskStatus, to_status: str | TaskStatus) -> None:
    current = normalize_task_status(from_status)
    next_status = normalize_task_status(to_status)
    if current == next_status:
        return
    if next_status not in ALLOWED_TASK_TRANSITIONS[current]:
        raise ValueError(f"Invalid task transition: {current.value} -> {next_status.value}")


def validate_objective_transition(from_status: str | ObjectiveStatus, to_status: str | ObjectiveStatus) -> None:
    current = normalize_objective_status(from_status)
    next_status = normalize_objective_status(to_status)
    if current == next_status:
        return
    if next_status not in ALLOWED_OBJECTIVE_TRANSITIONS[current]:
        raise ValueError(f"Invalid objective transition: {current.value} -> {next_status.value}")


def _execution_control_id(target_kind: KillSwitchTargetKind, target_id: str) -> str:
    digest = hashlib.sha256(f"{target_kind.value}:{target_id}".encode("utf-8")).hexdigest()[:16]
    return f"control_{digest}"


def _event_counts_for_adapter_breaker(event: DaemonEvent, adapter_id: str) -> bool:
    metadata = event.metadata
    if metadata.get("adapter_id") != adapter_id:
        return False
    if event.event_type == "execution_adapter_rejected":
        return metadata.get("reason_code") == "adapter_execution_failed"
    decision = str(metadata.get("decision") or "")
    return decision.endswith("_failed")


def _authority_claim_codes(text: str) -> list[str]:
    lowered = text.lower()
    checks = {
        "approval_claim": ("approve", "approval", "authorized", "permission"),
        "hosted_boundary_claim": ("hosted", "codex", "provider"),
        "active_repo_write_claim": ("active repo", "apply-back", "apply back", "write access"),
        "docker_network_shell_claim": ("docker", "network", "shell", "tool"),
        "policy_override_claim": ("override policy", "weaken policy", "ignore policy", "bypass"),
    }
    return [
        code
        for code, needles in checks.items()
        if any(needle in lowered for needle in needles)
    ]


def _provenance_id(kind: str, value: str) -> str:
    digest = hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()[:16]
    return f"ctx_{digest}"


def _context_warnings(records: list[ContextProvenanceRecord]) -> list[str]:
    warnings: list[str] = []
    for record in records:
        for warning in record.warnings:
            if warning not in warnings:
                warnings.append(warning)
    return warnings


def _artifact_context_classification(kind: str) -> tuple[ContextSourceKind, ContextTrustLevel, list[str]]:
    normalized = kind.lower()
    if normalized in {"codex_final_message", "final_report"}:
        return ContextSourceKind.GENERATED_PLAN, ContextTrustLevel.GENERATED, ["generated_text_not_authority"]
    if "isolated" in normalized or "diff" in normalized or "baseline_manifest" in normalized:
        return ContextSourceKind.REPO_FILE, ContextTrustLevel.UNTRUSTED_REPO, ["untrusted_repo_context"]
    if "event" in normalized or "transcript" in normalized or "test" in normalized or "pytest" in normalized:
        return ContextSourceKind.TOOL_OUTPUT, ContextTrustLevel.UNTRUSTED_TOOL_OUTPUT, ["artifact_content_not_authority"]
    return ContextSourceKind.ARTIFACT, ContextTrustLevel.ARTIFACT, ["artifact_content_not_authority"]


class SQLiteStore:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.harness_dir = self.project_root / ".harness"
        self.db_path = self.harness_dir / "harness.sqlite"
        self.runs_dir = self.harness_dir / "runs"

    @classmethod
    def open_initialized(cls, project_root: Path) -> "SQLiteStore":
        store = cls(project_root)
        store.initialize()
        return store

    def initialize(self) -> None:
        self.harness_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        (self.harness_dir / "tmp").mkdir(parents=True, exist_ok=True)
        approvals = self.harness_dir / "approvals.yaml"
        if not approvals.exists():
            approvals.write_text("approvals: []\n", encoding="utf-8")
        with self.connect() as conn:
            self._ensure_schema_migrations_table(conn)
            self._apply_schema_migrations(conn)
            # After migration integrity is validated, replay the additive schema as a repair pass for
            # older or partially initialized .harness databases with missing IF NOT EXISTS tables.
            conn.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
            self._ensure_column(conn, "runs", "approval_id", "TEXT")
            self._ensure_column(conn, "runs", "task_id", "TEXT")
            self._ensure_column(conn, "runs", "objective_id", "TEXT")
            self._ensure_column(conn, "runs", "session_id", "TEXT")
            self._ensure_column(conn, "events", "session_id", "TEXT")
            self._ensure_column(conn, "events", "schema_version", "TEXT")
            self._ensure_column(conn, "events", "seq", "INTEGER")
            self._ensure_column(conn, "events", "task_id", "TEXT")
            self._ensure_column(conn, "events", "trace_id", "TEXT")
            self._ensure_column(conn, "events", "visibility", "TEXT")
            self._ensure_column(conn, "events", "redaction_state", "TEXT")
            self._ensure_column(conn, "artifacts", "schema_version", "TEXT")
            self._ensure_column(conn, "artifacts", "sha256", "TEXT")
            self._ensure_column(conn, "artifacts", "size_bytes", "INTEGER")
            self._ensure_column(conn, "artifacts", "producer", "TEXT")
            self._ensure_column(conn, "artifacts", "redaction_state", "TEXT")
            self._ensure_column(conn, "artifacts", "evidence_status", "TEXT")
            self._ensure_column(conn, "artifacts", "session_id", "TEXT")
            self._ensure_column(conn, "tasks", "objective_id", "TEXT")
            self._ensure_column(conn, "tasks", "idempotency_key", "TEXT")
            self._ensure_column(conn, "tasks", "required_approvals_json", "TEXT")
            self._ensure_column(conn, "tasks", "approval_state", "TEXT")
            self._ensure_column(conn, "tasks", "session_id", "TEXT")
            self._ensure_column(conn, "objectives", "session_id", "TEXT")
            self._ensure_column(conn, "sessions", "title", "TEXT")
            self._ensure_column(conn, "sessions", "parent_session_id", "TEXT")
            self._ensure_column(conn, "sessions", "forked_from_message_id", "TEXT")
            self._ensure_column(conn, "sessions", "provider_id", "TEXT")
            self._ensure_column(conn, "sessions", "model_id", "TEXT")
            self._ensure_column(conn, "sessions", "model_variant", "TEXT")
            self._ensure_column(conn, "sessions", "raw_model_ref", "TEXT")
            self._ensure_column(conn, "sessions", "summary", "TEXT")
            self._ensure_column(conn, "sessions", "token_input", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "sessions", "token_output", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "sessions", "token_reasoning", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "sessions", "token_cache_read", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "sessions", "token_cache_write", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "sessions", "estimated_cost_usd", "TEXT")
            self._ensure_column(conn, "sessions", "ui_preferences_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "sessions", "archived_at", "TEXT")
            self._ensure_provider_accounts_table(conn)
            self._ensure_model_preferences_table(conn)
            self._migrate_task_rows(conn)
            self._migrate_artifact_rows(conn)

    def _ensure_schema_migrations_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              id TEXT PRIMARY KEY,
              checksum TEXT NOT NULL,
              applied_at TEXT NOT NULL,
              metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )

    def _apply_schema_migrations(self, conn: sqlite3.Connection) -> None:
        migrations_dir = Path(__file__).with_name("migrations")
        known: dict[str, tuple[Path, str]] = {
            migration_id: (migrations_dir / filename, description)
            for migration_id, filename, description in SCHEMA_MIGRATIONS
        }
        applied_rows = conn.execute("SELECT id, checksum FROM schema_migrations ORDER BY id ASC").fetchall()
        applied = {row["id"]: row["checksum"] for row in applied_rows}
        unknown = sorted(migration_id for migration_id in applied if migration_id not in known)
        if unknown:
            raise RuntimeError(
                "Unknown future schema migration(s) present; refusing to mutate state: " + ", ".join(unknown)
            )
        for migration_id, filename, description in SCHEMA_MIGRATIONS:
            migration_path = migrations_dir / filename
            migration_sql = migration_path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(migration_sql.encode("utf-8")).hexdigest()
            if migration_id in applied:
                if applied[migration_id] != checksum:
                    raise RuntimeError(
                        f"Schema migration checksum mismatch for {migration_id}; refusing to mutate state."
                    )
                continue
            conn.executescript(migration_sql)
            conn.execute(
                """
                INSERT INTO schema_migrations (id, checksum, applied_at, metadata_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    migration_id,
                    checksum,
                    now_iso(),
                    json.dumps({"description": description, "path": filename}, sort_keys=True, default=str),
                ),
            )

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _ensure_provider_accounts_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_accounts (
              account_id TEXT PRIMARY KEY,
              provider_id TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT 'default',
              credential_kind TEXT NOT NULL,
              status TEXT NOT NULL,
              active INTEGER NOT NULL DEFAULT 1,
              expires_at TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_provider_accounts_provider_active
            ON provider_accounts(provider_id, active)
            """
        )

    def _ensure_model_preferences_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_preferences (
              raw_model_ref TEXT PRIMARY KEY,
              provider_id TEXT,
              model_id TEXT,
              model_variant TEXT,
              favorite INTEGER NOT NULL DEFAULT 0,
              is_default INTEGER NOT NULL DEFAULT 0,
              selection_count INTEGER NOT NULL DEFAULT 0,
              last_selected_at TEXT,
              last_reasoning_effort TEXT,
              source TEXT NOT NULL DEFAULT 'unknown',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_model_preferences_favorite
            ON model_preferences(favorite, last_selected_at DESC)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_model_preferences_default
            ON model_preferences(is_default, updated_at DESC)
            """
        )

    def _record_schema_migration(
        self, conn: sqlite3.Connection, migration_id: str, schema_text: str, description: str
    ) -> None:
        checksum = hashlib.sha256(schema_text.encode("utf-8")).hexdigest()
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (id, checksum, applied_at, metadata_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                migration_id,
                checksum,
                now_iso(),
                json.dumps({"description": description}, sort_keys=True, default=str),
            ),
        )

    def list_schema_migrations(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM schema_migrations ORDER BY applied_at ASC, id ASC").fetchall()
        return [dict(row) for row in rows]

    def _migrate_task_rows(self, conn: sqlite3.Connection) -> None:
        timestamp = now_iso()
        for legacy, canonical in LEGACY_TASK_STATUS_VALUES.items():
            conn.execute("UPDATE tasks SET status = ? WHERE status = ?", (canonical.value, legacy))
        conn.execute(
            """
            UPDATE tasks
            SET idempotency_key = 'task_idem_' || lower(hex(randomblob(8)))
            WHERE idempotency_key IS NULL OR idempotency_key = ''
            """
        )
        conn.execute(
            """
            UPDATE tasks
            SET required_approvals_json = '[]'
            WHERE required_approvals_json IS NULL OR required_approvals_json = ''
            """
        )
        conn.execute(
            """
            UPDATE tasks
            SET updated_at = ?
            WHERE updated_at IS NULL OR updated_at = ''
            """,
            (timestamp,),
        )

    def _migrate_artifact_rows(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            UPDATE artifacts
            SET schema_version = 'harness.artifact/v1'
            WHERE schema_version IS NULL OR schema_version = ''
            """
        )
        conn.execute(
            """
            UPDATE artifacts
            SET redaction_state = 'unknown'
            WHERE redaction_state IS NULL OR redaction_state = ''
            """
        )
        conn.execute(
            """
            UPDATE artifacts
            SET evidence_status = 'unknown'
            WHERE evidence_status IS NULL OR evidence_status = ''
            """
        )

    def create_session(
        self,
        *,
        title: str | None = None,
        parent_session_id: str | None = None,
        forked_from_message_id: str | None = None,
        objective_id: str | None = None,
        active_task_id: str | None = None,
        active_run_id: str | None = None,
        workbench_id: str | None = None,
        agent_id: str | None = None,
        provider_id: str | None = None,
        model_id: str | None = None,
        model_variant: str | None = None,
        raw_model_ref: str | None = None,
        mode: str | None = None,
        intent: str | None = None,
        status: SessionStatus = SessionStatus.ACTIVE,
        summary: str | None = None,
        ui_preferences: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionSpec:
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        timestamp = now_iso()
        metadata = sanitize_for_logging(metadata or {})
        ui_preferences = sanitize_for_logging(ui_preferences or {})
        with self.connect() as conn:
            if parent_session_id is not None:
                self._require_session(conn, parent_session_id)
            if objective_id is not None:
                self._require_objective(conn, objective_id)
            if active_task_id is not None:
                self._require_task(conn, active_task_id)
            if active_run_id is not None:
                self._require_run(conn, active_run_id)
            conn.execute(
                """
                INSERT INTO sessions (
                  id, project_path, title, parent_session_id, forked_from_message_id,
                  objective_id, active_task_id, active_run_id, workbench_id, agent_id,
                  provider_id, model_id, model_variant, raw_model_ref, mode, intent, status,
                  summary, ui_preferences_json, created_at, updated_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    str(self.project_root),
                    title,
                    parent_session_id,
                    forked_from_message_id,
                    objective_id,
                    active_task_id,
                    active_run_id,
                    workbench_id,
                    agent_id,
                    provider_id,
                    model_id,
                    model_variant,
                    raw_model_ref,
                    mode,
                    intent,
                    status.value,
                    summary,
                    json.dumps(ui_preferences, sort_keys=True, default=str),
                    timestamp,
                    timestamp,
                    json.dumps(metadata, sort_keys=True, default=str),
                ),
            )
        (self.harness_dir / "sessions" / session_id).mkdir(parents=True, exist_ok=True)
        (self.harness_dir / "sessions" / session_id / "transcript.jsonl").touch(exist_ok=True)
        self.append_store_event(
            EventStreamType.SESSION,
            session_id,
            "session.created",
            {
                "title": title,
                "intent": intent,
                "objective_id": objective_id,
                "active_task_id": active_task_id,
                "active_run_id": active_run_id,
                "raw_model_ref": raw_model_ref,
                "provider_id": provider_id,
                "model_id": model_id,
                "model_variant": model_variant,
                "model_selection_source": "session_create",
                "model_override_persisted": raw_model_ref is not None,
                "provider_execution_started": False,
                "model_execution_started": False,
                "hidden_provider_fallback": False,
                "hidden_model_fallback": False,
                "no_hidden_fallback": True,
                "permission_granting": False,
                "authority_granting": False,
            },
            session_id=session_id,
            redaction_state=RedactionState.NOT_REQUIRED,
        )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> SessionSpec:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(f"Session not found: {session_id}")
        return self._row_to_session(row)

    def archive_session(self, session_id: str) -> SessionSpec:
        self.get_session(session_id)
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET status = ?, archived_at = COALESCE(archived_at, ?), updated_at = ?
                WHERE id = ?
                """,
                (SessionStatus.ARCHIVED.value, timestamp, timestamp, session_id),
            )
        self.append_store_event(
            EventStreamType.SESSION,
            session_id,
            "session.archived",
            {"archived_at": timestamp},
            session_id=session_id,
            redaction_state=RedactionState.NOT_REQUIRED,
        )
        return self.get_session(session_id)

    def delete_session(self, session_id: str) -> SessionSpec:
        return self.archive_session(session_id)

    def restore_session(self, session_id: str) -> SessionSpec:
        self.get_session(session_id)
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET status = ?, archived_at = NULL, updated_at = ?
                WHERE id = ?
                """,
                (SessionStatus.ACTIVE.value, timestamp, session_id),
            )
        self.append_store_event(
            EventStreamType.SESSION,
            session_id,
            "session.restored",
            {"restored_at": timestamp, "permission_granting": False},
            session_id=session_id,
            redaction_state=RedactionState.NOT_REQUIRED,
        )
        return self.get_session(session_id)

    def hard_delete_session(self, session_id: str) -> dict[str, Any]:
        self.get_session(session_id)
        session_dir = self.harness_dir / "sessions" / session_id
        counts: dict[str, Any] = {
            "session_id": session_id,
            "session_parts": 0,
            "session_messages": 0,
            "session_todos": 0,
            "session_permissions": 0,
            "session_run_links": 0,
            "session_events": 0,
            "session_rows": 0,
            "child_sessions_unlinked": 0,
            "runs_unlinked": 0,
            "tasks_unlinked": 0,
            "objectives_unlinked": 0,
            "artifacts_unlinked": 0,
            "events_unlinked": 0,
            "event_store_unlinked": 0,
            "session_directory_removed": False,
            "runs_deleted": 0,
            "tasks_deleted": 0,
            "artifacts_deleted": 0,
            "active_repo_modified": False,
            "process_started": False,
            "permission_granting": False,
        }
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            child_result = conn.execute(
                """
                UPDATE sessions
                SET parent_session_id = CASE WHEN parent_session_id = ? THEN NULL ELSE parent_session_id END,
                    forked_from_message_id = CASE
                      WHEN forked_from_message_id IN (SELECT id FROM session_messages WHERE session_id = ?) THEN NULL
                      ELSE forked_from_message_id
                    END
                WHERE parent_session_id = ?
                   OR forked_from_message_id IN (SELECT id FROM session_messages WHERE session_id = ?)
                """,
                (session_id, session_id, session_id, session_id),
            )
            counts["child_sessions_unlinked"] = child_result.rowcount
            for table, key in (
                ("session_parts", "session_parts"),
                ("session_todos", "session_todos"),
                ("session_permissions", "session_permissions"),
                ("session_run_links", "session_run_links"),
            ):
                result = conn.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
                counts[key] = result.rowcount
            result = conn.execute("DELETE FROM session_messages WHERE session_id = ?", (session_id,))
            counts["session_messages"] = result.rowcount
            result = conn.execute(
                "DELETE FROM event_store WHERE stream_type = ? AND stream_id = ?",
                (EventStreamType.SESSION.value, session_id),
            )
            counts["session_events"] = result.rowcount
            for table, key in (
                ("runs", "runs_unlinked"),
                ("tasks", "tasks_unlinked"),
                ("objectives", "objectives_unlinked"),
                ("artifacts", "artifacts_unlinked"),
                ("events", "events_unlinked"),
            ):
                result = conn.execute(f"UPDATE {table} SET session_id = NULL WHERE session_id = ?", (session_id,))
                counts[key] = result.rowcount
            result = conn.execute("UPDATE event_store SET session_id = NULL WHERE session_id = ?", (session_id,))
            counts["event_store_unlinked"] = result.rowcount
            result = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            counts["session_rows"] = result.rowcount
            if counts["session_rows"] != 1:
                raise KeyError(f"Session not found: {session_id}")
        if session_dir.exists():
            shutil.rmtree(session_dir)
            counts["session_directory_removed"] = True
        return counts

    def cancel_session(self, session_id: str, *, reason: str | None = None) -> SessionSpec:
        self.get_session(session_id)
        timestamp = now_iso()
        sanitized_reason = sanitize_for_logging(reason) if reason is not None else None
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (SessionStatus.CANCELLED.value, timestamp, session_id),
            )
        self.append_store_event(
            EventStreamType.SESSION,
            session_id,
            "session.cancelled",
            {
                "cancelled_at": timestamp,
                "reason": sanitized_reason,
                "process_stopped": False,
                "run_cancelled": False,
                "task_cancelled": False,
                "permission_granting": False,
            },
            session_id=session_id,
            redaction_state=RedactionState.REDACTED if sanitized_reason else RedactionState.NOT_REQUIRED,
        )
        return self.get_session(session_id)

    def update_session_model(
        self,
        session_id: str,
        *,
        raw_model_ref: str | None,
        provider_id: str | None = None,
        model_id: str | None = None,
        model_variant: str | None = None,
    ) -> SessionSpec:
        self.get_session(session_id)
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET raw_model_ref = ?, provider_id = ?, model_id = ?, model_variant = ?, updated_at = ?
                WHERE id = ?
                """,
                (raw_model_ref, provider_id, model_id, model_variant, timestamp, session_id),
            )
        self.append_store_event(
            EventStreamType.SESSION,
            session_id,
            "session.model_selected",
            {
                "raw_model_ref": raw_model_ref,
                "provider_id": provider_id,
                "model_id": model_id,
                "model_variant": model_variant,
                "model_selection_source": "session_update",
                "model_override_persisted": raw_model_ref is not None,
                "provider_execution_started": False,
                "model_execution_started": False,
                "hidden_provider_fallback": False,
                "hidden_model_fallback": False,
                "no_hidden_fallback": True,
                "permission_granting": False,
                "authority_granting": False,
            },
            session_id=session_id,
            redaction_state=RedactionState.NOT_REQUIRED,
        )
        return self.get_session(session_id)

    def update_session_title(self, session_id: str, title: str | None) -> SessionSpec:
        self.get_session(session_id)
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute("UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?", (title, timestamp, session_id))
        self.append_store_event(
            EventStreamType.SESSION,
            session_id,
            "session.title_updated",
            {"title": title},
            session_id=session_id,
            redaction_state=RedactionState.REDACTED,
        )
        return self.get_session(session_id)

    def update_session_summary(
        self,
        session_id: str,
        *,
        summary: str | None = None,
        token_input: int | None = None,
        token_output: int | None = None,
        token_reasoning: int | None = None,
        token_cache_read: int | None = None,
        token_cache_write: int | None = None,
        estimated_cost_usd: Decimal | str | None = None,
    ) -> SessionSpec:
        current = self.get_session(session_id)
        sanitized_summary = sanitize_for_logging(summary) if summary is not None else current.summary
        cost_value = (
            str(Decimal(str(estimated_cost_usd)))
            if estimated_cost_usd is not None
            else (str(current.estimated_cost_usd) if current.estimated_cost_usd is not None else None)
        )
        values = {
            "summary": sanitized_summary,
            "token_input": current.token_input if token_input is None else int(token_input),
            "token_output": current.token_output if token_output is None else int(token_output),
            "token_reasoning": current.token_reasoning if token_reasoning is None else int(token_reasoning),
            "token_cache_read": current.token_cache_read if token_cache_read is None else int(token_cache_read),
            "token_cache_write": current.token_cache_write if token_cache_write is None else int(token_cache_write),
            "estimated_cost_usd": cost_value,
        }
        if any(value < 0 for key, value in values.items() if key.startswith("token_")):
            raise ValueError("Session token counters must be non-negative.")
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET summary = ?, token_input = ?, token_output = ?, token_reasoning = ?,
                    token_cache_read = ?, token_cache_write = ?, estimated_cost_usd = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    values["summary"],
                    values["token_input"],
                    values["token_output"],
                    values["token_reasoning"],
                    values["token_cache_read"],
                    values["token_cache_write"],
                    values["estimated_cost_usd"],
                    timestamp,
                    session_id,
                ),
            )
        self.append_store_event(
            EventStreamType.SESSION,
            session_id,
            "session.summary_updated",
            {
                "summary": values["summary"],
                "token_input": values["token_input"],
                "token_output": values["token_output"],
                "token_reasoning": values["token_reasoning"],
                "token_cache_read": values["token_cache_read"],
                "token_cache_write": values["token_cache_write"],
                "estimated_cost_usd": values["estimated_cost_usd"],
                "mutable_projection": True,
                "permission_granting": False,
            },
            session_id=session_id,
            redaction_state=RedactionState.REDACTED,
        )
        return self.get_session(session_id)

    def replace_provider_model_catalog_cache(self, providers: list[Any], models: list[Any]) -> dict[str, Any]:
        timestamp = now_iso()
        provider_rows = []
        model_rows = []
        for provider in providers:
            payload = sanitize_for_logging(_model_dump_jsonable(provider))
            provider_rows.append(
                (
                    f"catalog_provider_{payload['provider_id']}",
                    "provider",
                    payload["provider_id"],
                    payload["backend_id"],
                    None,
                    None,
                    None,
                    json.dumps(payload, sort_keys=True, default=str),
                    timestamp,
                )
            )
        for index, model in enumerate(models):
            payload = sanitize_for_logging(_model_dump_jsonable(model))
            row_id = f"catalog_model_{payload['provider_id']}_{payload.get('model_profile_id') or 'backend'}_{index}"
            model_rows.append(
                (
                    row_id,
                    "model",
                    payload["provider_id"],
                    payload["backend_id"],
                    payload["model_id"],
                    payload.get("model_profile_id"),
                    payload["raw_model_ref"],
                    json.dumps(payload, sort_keys=True, default=str),
                    timestamp,
                )
            )
        with self.connect() as conn:
            conn.execute("DELETE FROM provider_model_catalog_cache WHERE id NOT LIKE 'catalog_discovered_%'")
            conn.executemany(
                """
                INSERT INTO provider_model_catalog_cache (
                  id, catalog_kind, provider_id, backend_id, model_id, model_profile_id,
                  raw_model_ref, payload_json, refreshed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                provider_rows + model_rows,
            )
        return {
            "schema_version": "harness.provider_model_catalog_cache/v1",
            "refreshed_at": timestamp,
            "provider_count": len(provider_rows),
            "model_count": len(model_rows),
            "source": "local_config_and_builtin_specs",
            "permission_granting": False,
            "no_hidden_fallback": True,
        }

    def replace_discovered_model_catalog_cache(
        self,
        provider_id: str,
        models: list[Any],
        *,
        discovery_metadata: dict[str, Any],
    ) -> dict[str, Any]:
        timestamp = now_iso()
        cache_metadata = _catalog_cache_ttl_metadata(timestamp, discovery_metadata.get("cache_ttl_seconds"))
        sanitized_discovery_metadata = sanitize_for_logging({**dict(discovery_metadata), **cache_metadata})
        rows = []
        for index, model in enumerate(models):
            payload = sanitize_for_logging(_model_dump_jsonable(model))
            payload["source"] = "discovered"
            model_discovery_metadata = payload.get("discovery_metadata") if isinstance(payload.get("discovery_metadata"), dict) else {}
            payload["discovery_metadata"] = sanitize_for_logging({**model_discovery_metadata, **sanitized_discovery_metadata})
            payload.setdefault("discovered_at", sanitized_discovery_metadata.get("discovered_at"))
            payload.setdefault("endpoint", sanitized_discovery_metadata.get("endpoint"))
            payload["cache_refreshed_at"] = cache_metadata["cache_refreshed_at"]
            payload["cache_ttl_seconds"] = cache_metadata["cache_ttl_seconds"]
            payload["cache_expires_at"] = cache_metadata["cache_expires_at"]
            payload["cache_status"] = cache_metadata["cache_status"]
            rows.append(
                (
                    f"catalog_discovered_{provider_id}_{index}",
                    "model",
                    payload["provider_id"],
                    payload["backend_id"],
                    payload["model_id"],
                    payload.get("model_profile_id"),
                    payload["raw_model_ref"],
                    json.dumps(payload, sort_keys=True, default=str),
                    timestamp,
                )
            )
        with self.connect() as conn:
            conn.execute("DELETE FROM provider_model_catalog_cache WHERE id LIKE 'catalog_discovered_%' AND provider_id = ?", (provider_id,))
            conn.executemany(
                """
                INSERT INTO provider_model_catalog_cache (
                  id, catalog_kind, provider_id, backend_id, model_id, model_profile_id,
                  raw_model_ref, payload_json, refreshed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return {
            "schema_version": "harness.provider_model_catalog_discovery_cache/v1",
            "refreshed_at": timestamp,
            "provider_id": provider_id,
            "model_count": len(rows),
            "source": "discovered",
            "discovery_metadata": sanitized_discovery_metadata,
            **cache_metadata,
            "permission_granting": False,
            "no_hidden_fallback": True,
        }

    def clear_discovered_model_catalog_cache(self, provider_id: str | None = None) -> dict[str, Any]:
        timestamp = now_iso()
        with self.connect() as conn:
            if provider_id is None:
                cursor = conn.execute("DELETE FROM provider_model_catalog_cache WHERE id LIKE 'catalog_discovered_%'")
            else:
                cursor = conn.execute(
                    "DELETE FROM provider_model_catalog_cache WHERE id LIKE 'catalog_discovered_%' AND provider_id = ?",
                    (provider_id,),
                )
        return {
            "schema_version": "harness.provider_model_catalog_discovery_cache_clear/v1",
            "cleared_at": timestamp,
            "provider_id": provider_id,
            "removed_count": cursor.rowcount,
            "source": "discovered",
            "permission_granting": False,
            "no_hidden_fallback": True,
        }

    def list_provider_model_catalog_cache(self, catalog_kind: str | None = None) -> list[dict[str, Any]]:
        params: tuple[Any, ...] = ()
        query = "SELECT * FROM provider_model_catalog_cache"
        if catalog_kind is not None:
            query += " WHERE catalog_kind = ?"
            params = (catalog_kind,)
        query += " ORDER BY catalog_kind, provider_id, model_profile_id, model_id, id"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "id": row["id"],
                "catalog_kind": row["catalog_kind"],
                "provider_id": row["provider_id"],
                "backend_id": row["backend_id"],
                "model_id": row["model_id"],
                "model_profile_id": row["model_profile_id"],
                "raw_model_ref": row["raw_model_ref"],
                "payload": json.loads(row["payload_json"]),
                "refreshed_at": row["refreshed_at"],
            }
            for row in rows
        ]

    def create_provider_account(
        self,
        *,
        provider_id: str,
        credential_kind: str,
        status: str = "configured",
        description: str = "default",
        active: bool = True,
        expires_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        provider_id = str(provider_id or "").strip()
        credential_kind = str(credential_kind or "").strip()
        status = str(status or "").strip()
        if not provider_id:
            raise ValueError("Provider account requires provider_id.")
        if not credential_kind:
            raise ValueError("Provider account requires credential_kind.")
        if status not in {"configured", "missing", "expired", "refresh_required", "unknown", "not_required"}:
            raise ValueError(f"Unsupported provider account status: {status}")
        account_id = f"acct_{uuid.uuid4().hex[:12]}"
        timestamp = now_iso()
        safe_metadata = _redact_provider_account_metadata(sanitize_for_logging(metadata or {}))
        with self.connect() as conn:
            self._ensure_provider_accounts_table(conn)
            if active:
                conn.execute("UPDATE provider_accounts SET active = 0 WHERE provider_id = ?", (provider_id,))
            conn.execute(
                """
                INSERT INTO provider_accounts (
                  account_id, provider_id, description, credential_kind, status, active,
                  expires_at, created_at, updated_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    provider_id,
                    description or "default",
                    credential_kind,
                    status,
                    1 if active else 0,
                    expires_at,
                    timestamp,
                    timestamp,
                    json.dumps(safe_metadata, sort_keys=True, default=str),
                ),
            )
        account = self.get_provider_account(account_id)
        self._record_provider_account_event("provider.account_created", account)
        return account

    def list_provider_accounts(self, provider_id: str | None = None) -> list[dict[str, Any]]:
        params: tuple[Any, ...] = ()
        query = "SELECT * FROM provider_accounts"
        if provider_id is not None:
            query += " WHERE provider_id = ?"
            params = (provider_id,)
        query += " ORDER BY provider_id ASC, active DESC, updated_at DESC, account_id ASC"
        with self.connect() as conn:
            self._ensure_provider_accounts_table(conn)
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_provider_account(row) for row in rows]

    def get_provider_account(self, account_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            self._ensure_provider_accounts_table(conn)
            row = conn.execute("SELECT * FROM provider_accounts WHERE account_id = ?", (account_id,)).fetchone()
        if row is None:
            raise KeyError(f"Provider account not found: {account_id}")
        return self._row_to_provider_account(row)

    def active_provider_account(self, provider_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            self._ensure_provider_accounts_table(conn)
            row = conn.execute(
                """
                SELECT * FROM provider_accounts
                WHERE provider_id = ? AND active = 1
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (provider_id,),
            ).fetchone()
        return self._row_to_provider_account(row) if row is not None else None

    def activate_provider_account(self, provider_id: str, account_id: str) -> dict[str, Any]:
        timestamp = now_iso()
        with self.connect() as conn:
            self._ensure_provider_accounts_table(conn)
            row = conn.execute(
                "SELECT * FROM provider_accounts WHERE provider_id = ? AND account_id = ?",
                (provider_id, account_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"Provider account not found: {account_id}")
            conn.execute("UPDATE provider_accounts SET active = 0 WHERE provider_id = ?", (provider_id,))
            conn.execute(
                "UPDATE provider_accounts SET active = 1, updated_at = ? WHERE account_id = ?",
                (timestamp, account_id),
            )
        account = self.get_provider_account(account_id)
        self._record_provider_account_event("provider.account_activated", account)
        return account

    def remove_provider_account(self, account_id: str) -> dict[str, Any]:
        account = self.get_provider_account(account_id)
        secret_removed = False
        try:
            from harness.provider_auth import delete_provider_account_secret

            secret_removed = delete_provider_account_secret(self.project_root, account_id)
        except Exception:
            secret_removed = False
        with self.connect() as conn:
            self._ensure_provider_accounts_table(conn)
            result = conn.execute("DELETE FROM provider_accounts WHERE account_id = ?", (account_id,))
            if result.rowcount != 1:
                raise KeyError(f"Provider account not found: {account_id}")
        account = {**account, "credential_removed": secret_removed}
        self._record_provider_account_event("provider.account_deleted", account)
        return account

    def _row_to_provider_account(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema_version": "harness.provider_account/v1",
            "account_id": row["account_id"],
            "provider_id": row["provider_id"],
            "description": sanitize_for_logging(row["description"]),
            "credential_kind": row["credential_kind"],
            "status": row["status"],
            "active": bool(row["active"]),
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "metadata": _redact_provider_account_metadata(sanitize_for_logging(json.loads(row["metadata_json"] or "{}"))),
            "credential_value_included": False,
            "credentials_included": False,
            "credential_written": False,
            "provider_execution_started": False,
            "model_execution_started": False,
            "network_accessed": False,
            "permission_granting": False,
            "authority_granting": False,
            "no_hidden_fallback": True,
        }

    def _record_provider_account_event(self, kind: str, account: dict[str, Any]) -> None:
        payload = {
            "schema_version": "harness.provider_account_event/v1",
            "provider_id": account.get("provider_id"),
            "account_id": account.get("account_id"),
            "credential_kind": account.get("credential_kind"),
            "status": account.get("status"),
            "active": account.get("active"),
            "credential_value_included": False,
            "credentials_included": False,
            "credential_written": False,
            "credential_removed": bool(account.get("credential_removed", False)),
            "network_accessed": False,
            "provider_execution_started": False,
            "model_execution_started": False,
            "permission_granting": False,
            "authority_granting": False,
            "no_hidden_fallback": True,
        }
        try:
            self.append_store_event(
                EventStreamType.ORCHESTRATION,
                "provider_accounts",
                kind,
                payload,
                redaction_state=RedactionState.NOT_REQUIRED,
            )
        except sqlite3.Error:
            return

    def record_model_selection(
        self,
        *,
        raw_model_ref: str,
        provider_id: str | None = None,
        model_id: str | None = None,
        model_variant: str | None = None,
        last_reasoning_effort: str | None = None,
        source: str = "session_model_selection",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raw_model_ref = str(raw_model_ref or "").strip()
        if not raw_model_ref:
            raise ValueError("Model preference requires raw_model_ref.")
        timestamp = now_iso()
        payload_metadata = sanitize_for_logging(metadata or {})
        with self.connect() as conn:
            self._ensure_model_preferences_table(conn)
            conn.execute(
                """
                INSERT INTO model_preferences (
                  raw_model_ref, provider_id, model_id, model_variant, favorite, is_default,
                  selection_count, last_selected_at, last_reasoning_effort, source,
                  created_at, updated_at, metadata_json
                ) VALUES (?, ?, ?, ?, 0, 0, 1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(raw_model_ref) DO UPDATE SET
                  provider_id = excluded.provider_id,
                  model_id = excluded.model_id,
                  model_variant = excluded.model_variant,
                  selection_count = model_preferences.selection_count + 1,
                  last_selected_at = excluded.last_selected_at,
                  last_reasoning_effort = excluded.last_reasoning_effort,
                  source = excluded.source,
                  updated_at = excluded.updated_at,
                  metadata_json = excluded.metadata_json
                """,
                (
                    raw_model_ref,
                    provider_id,
                    model_id,
                    model_variant,
                    timestamp,
                    last_reasoning_effort,
                    source,
                    timestamp,
                    timestamp,
                    json.dumps(payload_metadata, sort_keys=True, default=str),
                ),
            )
        return self.get_model_preference(raw_model_ref)

    def set_model_favorite(
        self,
        raw_model_ref: str,
        favorite: bool,
        *,
        provider_id: str | None = None,
        model_id: str | None = None,
        model_variant: str | None = None,
        source: str = "model_favorite_command",
    ) -> dict[str, Any]:
        return self._upsert_model_preference_flag(
            raw_model_ref,
            provider_id=provider_id,
            model_id=model_id,
            model_variant=model_variant,
            favorite=favorite,
            is_default=None,
            source=source,
        )

    def set_default_model_preference(
        self,
        raw_model_ref: str,
        *,
        provider_id: str | None = None,
        model_id: str | None = None,
        model_variant: str | None = None,
        source: str = "model_default_command",
    ) -> dict[str, Any]:
        raw_model_ref = str(raw_model_ref or "").strip()
        if not raw_model_ref:
            raise ValueError("Model preference requires raw_model_ref.")
        with self.connect() as conn:
            self._ensure_model_preferences_table(conn)
            conn.execute("UPDATE model_preferences SET is_default = 0")
        return self._upsert_model_preference_flag(
            raw_model_ref,
            provider_id=provider_id,
            model_id=model_id,
            model_variant=model_variant,
            favorite=None,
            is_default=True,
            source=source,
        )

    def get_default_model_preference(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            self._ensure_model_preferences_table(conn)
            row = conn.execute(
                """
                SELECT * FROM model_preferences
                WHERE is_default = 1
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
        return self._row_to_model_preference(row) if row is not None else None

    def get_model_preference(self, raw_model_ref: str) -> dict[str, Any]:
        with self.connect() as conn:
            self._ensure_model_preferences_table(conn)
            row = conn.execute("SELECT * FROM model_preferences WHERE raw_model_ref = ?", (raw_model_ref,)).fetchone()
        if row is None:
            raise KeyError(f"Model preference not found: {raw_model_ref}")
        return self._row_to_model_preference(row)

    def list_model_preferences(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            self._ensure_model_preferences_table(conn)
            rows = conn.execute(
                """
                SELECT * FROM model_preferences
                ORDER BY favorite DESC, is_default DESC, last_selected_at IS NULL ASC, last_selected_at DESC, updated_at DESC, raw_model_ref ASC
                """
            ).fetchall()
        return [self._row_to_model_preference(row) for row in rows]

    def _upsert_model_preference_flag(
        self,
        raw_model_ref: str,
        *,
        provider_id: str | None,
        model_id: str | None,
        model_variant: str | None,
        favorite: bool | None,
        is_default: bool | None,
        source: str,
    ) -> dict[str, Any]:
        raw_model_ref = str(raw_model_ref or "").strip()
        if not raw_model_ref:
            raise ValueError("Model preference requires raw_model_ref.")
        timestamp = now_iso()
        with self.connect() as conn:
            self._ensure_model_preferences_table(conn)
            existing = conn.execute("SELECT * FROM model_preferences WHERE raw_model_ref = ?", (raw_model_ref,)).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO model_preferences (
                      raw_model_ref, provider_id, model_id, model_variant, favorite, is_default,
                      selection_count, last_selected_at, last_reasoning_effort, source,
                      created_at, updated_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?, ?, '{}')
                    """,
                    (
                        raw_model_ref,
                        provider_id,
                        model_id,
                        model_variant,
                        1 if favorite else 0,
                        1 if is_default else 0,
                        source,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE model_preferences
                    SET provider_id = COALESCE(?, provider_id),
                        model_id = COALESCE(?, model_id),
                        model_variant = COALESCE(?, model_variant),
                        favorite = COALESCE(?, favorite),
                        is_default = COALESCE(?, is_default),
                        source = ?,
                        updated_at = ?
                    WHERE raw_model_ref = ?
                    """,
                    (
                        provider_id,
                        model_id,
                        model_variant,
                        None if favorite is None else (1 if favorite else 0),
                        None if is_default is None else (1 if is_default else 0),
                        source,
                        timestamp,
                        raw_model_ref,
                    ),
                )
        return self.get_model_preference(raw_model_ref)

    def _row_to_model_preference(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema_version": "harness.model_preference/v1",
            "raw_model_ref": row["raw_model_ref"],
            "provider_id": row["provider_id"],
            "model_id": row["model_id"],
            "model_variant": row["model_variant"],
            "favorite": bool(row["favorite"]),
            "is_default": bool(row["is_default"]),
            "selection_count": int(row["selection_count"] or 0),
            "last_selected_at": row["last_selected_at"],
            "last_reasoning_effort": row["last_reasoning_effort"],
            "source": row["source"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "metadata": json.loads(row["metadata_json"] or "{}"),
            "provider_execution_started": False,
            "model_execution_started": False,
            "network_accessed": False,
            "credentials_included": False,
            "permission_granting": False,
            "authority_granting": False,
            "no_hidden_fallback": True,
        }

    def list_sessions(self, status: str | None = None) -> list[SessionSpec]:
        with self.connect() as conn:
            if status is None:
                rows = conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sessions WHERE status = ? ORDER BY updated_at DESC",
                    (SessionStatus(status).value,),
                ).fetchall()
        return [self._row_to_session(row) for row in rows]

    def latest_session(self, *, include_archived: bool = False) -> SessionSpec | None:
        with self.connect() as conn:
            if include_archived:
                row = conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT 1").fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT * FROM sessions
                    WHERE status != ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (SessionStatus.ARCHIVED.value,),
                ).fetchone()
        return self._row_to_session(row) if row is not None else None

    def list_child_sessions(self, session_id: str) -> list[SessionSpec]:
        self.get_session(session_id)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE parent_session_id = ? ORDER BY updated_at DESC, id ASC",
                (session_id,),
            ).fetchall()
        return [self._row_to_session(row) for row in rows]

    def fork_session(
        self,
        session_id: str,
        *,
        message_id: str | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionSpec:
        parent = self.get_session(session_id)
        if message_id is not None:
            with self.connect() as conn:
                row = conn.execute(
                    "SELECT id FROM session_messages WHERE id = ? AND session_id = ?",
                    (message_id, session_id),
                ).fetchone()
            if row is None:
                raise KeyError(f"Session message not found: {message_id}")
        child_metadata = dict(parent.metadata)
        child_metadata.update(sanitize_for_logging(metadata or {}))
        child = self.create_session(
            title=title or (f"Fork of {parent.title}" if parent.title else None),
            parent_session_id=parent.id,
            forked_from_message_id=message_id,
            workbench_id=parent.workbench_id,
            agent_id=parent.agent_id,
            provider_id=parent.provider_id,
            model_id=parent.model_id,
            model_variant=parent.model_variant,
            raw_model_ref=parent.raw_model_ref,
            mode=parent.mode.value if parent.mode is not None else None,
            intent=parent.intent,
            ui_preferences=parent.ui_preferences,
            metadata=child_metadata,
        )
        self.append_store_event(
            EventStreamType.SESSION,
            child.id,
            "session.forked",
            {"parent_session_id": parent.id, "message_id": message_id},
            session_id=child.id,
            message_id=message_id,
            redaction_state=RedactionState.NOT_REQUIRED,
        )
        return child

    def update_session(
        self,
        session_id: str,
        *,
        objective_id: str | None = None,
        active_task_id: str | None = None,
        active_run_id: str | None = None,
        workbench_id: str | None = None,
        agent_id: str | None = None,
        mode: str | None = None,
        intent: str | None = None,
        status: SessionStatus | str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SessionSpec:
        current = self.get_session(session_id)
        values = {
            "objective_id": current.objective_id if objective_id is None else objective_id,
            "active_task_id": current.active_task_id if active_task_id is None else active_task_id,
            "active_run_id": current.active_run_id if active_run_id is None else active_run_id,
            "workbench_id": current.workbench_id if workbench_id is None else workbench_id,
            "agent_id": current.agent_id if agent_id is None else agent_id,
            "mode": current.mode.value if current.mode is not None else None,
            "intent": current.intent if intent is None else intent,
            "status": current.status.value if status is None else SessionStatus(status).value,
            "metadata": current.metadata if metadata is None else sanitize_for_logging(metadata),
        }
        if mode is not None:
            values["mode"] = mode
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET objective_id = ?, active_task_id = ?, active_run_id = ?, workbench_id = ?,
                    agent_id = ?, mode = ?, intent = ?, status = ?, updated_at = ?, metadata_json = ?
                WHERE id = ?
                """,
                (
                    values["objective_id"],
                    values["active_task_id"],
                    values["active_run_id"],
                    values["workbench_id"],
                    values["agent_id"],
                    values["mode"],
                    values["intent"],
                    values["status"],
                    timestamp,
                    json.dumps(values["metadata"], sort_keys=True, default=str),
                    session_id,
                ),
            )
        return self.get_session(session_id)

    def update_session_cwd(
        self,
        session_id: str,
        *,
        project_root: str,
        old_cwd: str,
        new_cwd: str,
        requested_path: str,
        resolved_abs_path: str,
        actor: str,
        tool_call_id: str | None = None,
        run_id: str | None = None,
    ) -> StoredEventRecord:
        timestamp = now_iso()
        event_id = f"evt2_{uuid.uuid4().hex[:12]}"
        sanitized_actor = sanitize_for_logging({"kind": actor})
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT metadata_json FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                raise KeyError(f"Session not found: {session_id}")
            metadata = sanitize_for_logging(json.loads(row["metadata_json"] or "{}"))
            metadata["cwd"] = new_cwd
            conn.execute(
                """
                UPDATE sessions
                SET metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(metadata, sort_keys=True, default=str), timestamp, session_id),
            )
            payload = sanitize_for_logging(
                {
                    "type": "session.cwd_changed",
                    "session_id": session_id,
                    "project_root": project_root,
                    "old_cwd": old_cwd,
                    "new_cwd": new_cwd,
                    "requested_path": requested_path,
                    "resolved_abs_path": resolved_abs_path,
                    "actor": actor,
                    "tool_call_id": tool_call_id,
                    "permission_granting": False,
                    "process_started": False,
                    "shell_execution_started": False,
                    "filesystem_modified": False,
                    "active_repo_modified": False,
                    "git_mutation_started": False,
                    "summary": f"cwd {old_cwd} -> {new_cwd}",
                }
            )
            seq = self._next_store_event_seq(conn, EventStreamType.SESSION.value, session_id)
            conn.execute(
                """
                INSERT INTO event_store (
                  id, stream_type, stream_id, seq, kind, visibility, redaction_state,
                  session_id, message_id, run_id, task_id, artifact_id, actor_json,
                  correlation_id, causation_id, payload_json, artifact_refs_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    EventStreamType.SESSION.value,
                    session_id,
                    seq,
                    "session.cwd_changed",
                    EventVisibility.USER_VISIBLE.value,
                    RedactionState.REDACTED.value,
                    session_id,
                    None,
                    run_id,
                    None,
                    None,
                    json.dumps(sanitized_actor, sort_keys=True, default=str),
                    tool_call_id,
                    None,
                    json.dumps(payload, sort_keys=True, default=str),
                    json.dumps([], sort_keys=True, default=str),
                    timestamp,
                ),
            )
        return StoredEventRecord(
            id=event_id,
            stream_type=EventStreamType.SESSION,
            stream_id=session_id,
            seq=seq,
            kind="session.cwd_changed",
            visibility=EventVisibility.USER_VISIBLE,
            redaction_state=RedactionState.REDACTED,
            session_id=session_id,
            message_id=None,
            run_id=run_id,
            task_id=None,
            artifact_id=None,
            actor=sanitized_actor,
            correlation_id=tool_call_id,
            causation_id=None,
            payload=payload,
            artifact_refs=[],
            created_at=parse_dt(timestamp),
        )

    def update_session_ui_preferences(self, session_id: str, preferences: dict[str, Any]) -> SessionSpec:
        current = self.get_session(session_id)
        updated = dict(current.ui_preferences)
        updated.update(sanitize_for_logging(preferences))
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET ui_preferences_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(updated, sort_keys=True, default=str), timestamp, session_id),
            )
        self.append_store_event(
            EventStreamType.SESSION,
            session_id,
            "session.ui_preferences.updated",
            {
                "keys": sorted(preferences),
                "mutable_projection": True,
                "permission_granting": False,
            },
            session_id=session_id,
            redaction_state=RedactionState.REDACTED,
        )
        return self.get_session(session_id)

    def append_session_message(
        self,
        session_id: str,
        role: SessionMessageRole | str,
        content_preview: str,
        *,
        parent_message_id: str | None = None,
        agent_id: str | None = None,
        run_id: str | None = None,
        objective_id: str | None = None,
        mutation_reversibility: SessionMutationReversibility | str = SessionMutationReversibility.NONE,
    ) -> SessionMessageRecord:
        message_id = f"msg_{uuid.uuid4().hex[:12]}"
        timestamp = now_iso()
        role_value = SessionMessageRole(role.value if isinstance(role, SessionMessageRole) else role)
        reversibility_value = SessionMutationReversibility(
            mutation_reversibility.value
            if isinstance(mutation_reversibility, SessionMutationReversibility)
            else mutation_reversibility
        )
        preview = str(sanitize_for_logging(content_preview))[:16 * 1024]
        with self.connect() as conn:
            self._require_session(conn, session_id)
            if run_id is not None:
                self._require_run(conn, run_id)
            conn.execute(
                """
                INSERT INTO session_messages (
                  id, session_id, parent_message_id, role, agent_id, run_id, objective_id,
                  mutation_reversibility, content_preview, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    session_id,
                    parent_message_id,
                    role_value.value,
                    agent_id,
                    run_id,
                    objective_id,
                    reversibility_value.value,
                    preview,
                    timestamp,
                ),
            )
            if run_id is not None:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO session_run_links (session_id, run_id, message_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (session_id, run_id, message_id, timestamp),
                )
        self.append_store_event(
            EventStreamType.SESSION,
            session_id,
            "session.message.appended",
            {"role": role_value.value, "content_preview": preview, "run_id": run_id},
            session_id=session_id,
            message_id=message_id,
            run_id=run_id,
            redaction_state=RedactionState.REDACTED,
            created_at=timestamp,
        )
        return SessionMessageRecord(
            id=message_id,
            session_id=session_id,
            parent_message_id=parent_message_id,
            role=role_value,
            agent_id=agent_id,
            run_id=run_id,
            objective_id=objective_id,
            mutation_reversibility=reversibility_value,
            content_preview=preview,
            created_at=parse_dt(timestamp),
        )

    def append_session_part(
        self,
        session_id: str,
        message_id: str,
        kind: SessionPartKind | str,
        *,
        text: str | None = None,
        artifact_id: str | None = None,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        redaction_state: RedactionState | str = RedactionState.NOT_REQUIRED,
    ) -> SessionPartRecord:
        part_id = f"part_{uuid.uuid4().hex[:12]}"
        timestamp = now_iso()
        kind_value = SessionPartKind(kind.value if isinstance(kind, SessionPartKind) else kind)
        redaction_value = RedactionState(redaction_state.value if isinstance(redaction_state, RedactionState) else redaction_state)
        metadata = sanitize_for_logging(metadata or {})
        sanitized_text = str(sanitize_for_logging(text)) if text is not None else None
        with self.connect() as conn:
            self._require_session(conn, session_id)
            message = conn.execute(
                "SELECT id FROM session_messages WHERE id = ? AND session_id = ?", (message_id, session_id)
            ).fetchone()
            if message is None:
                raise KeyError(f"Session message not found: {message_id}")
            row = conn.execute(
                """
                SELECT COALESCE(MAX(ordinal), 0) AS max_ordinal
                FROM session_parts
                WHERE session_id = ? AND message_id = ?
                """,
                (session_id, message_id),
            ).fetchone()
            ordinal = int(row["max_ordinal"] or 0) + 1
            conn.execute(
                """
                INSERT INTO session_parts (
                  id, session_id, message_id, kind, ordinal, text, artifact_id, run_id,
                  metadata_json, redaction_state, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    part_id,
                    session_id,
                    message_id,
                    kind_value.value,
                    ordinal,
                    sanitized_text,
                    artifact_id,
                    run_id,
                    json.dumps(metadata, sort_keys=True, default=str),
                    redaction_value.value,
                    timestamp,
                ),
            )
        self.append_store_event(
            EventStreamType.SESSION,
            session_id,
            "session.part.appended",
            {"kind": kind_value.value, "ordinal": ordinal, "artifact_id": artifact_id, "run_id": run_id},
            session_id=session_id,
            message_id=message_id,
            run_id=run_id,
            artifact_id=artifact_id,
            redaction_state=redaction_value,
            created_at=timestamp,
        )
        return SessionPartRecord(
            id=part_id,
            session_id=session_id,
            message_id=message_id,
            kind=kind_value,
            ordinal=ordinal,
            text=sanitized_text,
            artifact_id=artifact_id,
            run_id=run_id,
            metadata=metadata,
            redaction_state=redaction_value,
            created_at=parse_dt(timestamp),
        )

    def append_session_snapshot_ref(
        self,
        session_id: str,
        message_id: str,
        snapshot_id: str,
        *,
        snapshot_kind: str,
        artifact_id: str | None = None,
        run_id: str | None = None,
        reversible: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> SessionPartRecord:
        snapshot_metadata = {
            "snapshot_id": snapshot_id,
            "snapshot_kind": snapshot_kind,
            "reversible": reversible,
            "revert_supported": False,
            **(metadata or {}),
        }
        if artifact_id:
            snapshot_metadata["artifact_id"] = artifact_id
        part = self.append_session_part(
            session_id,
            message_id,
            SessionPartKind.SNAPSHOT_REF,
            artifact_id=artifact_id,
            run_id=run_id,
            metadata=snapshot_metadata,
            redaction_state=RedactionState.NOT_REQUIRED,
        )
        self.append_store_event(
            EventStreamType.SESSION,
            session_id,
            "session.snapshot.recorded",
            {
                "snapshot_id": snapshot_id,
                "snapshot_kind": snapshot_kind,
                "message_id": message_id,
                "artifact_id": artifact_id,
                "run_id": run_id,
                "reversible": reversible,
                "revert_supported": False,
                "permission_granting": False,
            },
            session_id=session_id,
            message_id=message_id,
            run_id=run_id,
            artifact_id=artifact_id,
            artifact_refs=[artifact_id] if artifact_id else [],
            redaction_state=RedactionState.NOT_REQUIRED,
        )
        return part

    def list_session_messages(self, session_id: str) -> list[SessionMessageRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM session_messages WHERE session_id = ? ORDER BY created_at ASC, id ASC",
                (session_id,),
            ).fetchall()
        return [
            SessionMessageRecord(
                id=row["id"],
                session_id=row["session_id"],
                parent_message_id=row["parent_message_id"],
                role=SessionMessageRole(row["role"]),
                agent_id=row["agent_id"],
                run_id=row["run_id"],
                objective_id=row["objective_id"],
                mutation_reversibility=SessionMutationReversibility(row["mutation_reversibility"]),
                content_preview=row["content_preview"],
                created_at=parse_dt(row["created_at"]),
            )
            for row in rows
        ]

    def list_session_parts(self, session_id: str, message_id: str | None = None) -> list[SessionPartRecord]:
        params: list[Any] = [session_id]
        where = "session_id = ?"
        if message_id is not None:
            where += " AND message_id = ?"
            params.append(message_id)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM session_parts WHERE {where} ORDER BY message_id ASC, ordinal ASC", params
            ).fetchall()
        return [
            SessionPartRecord(
                id=row["id"],
                session_id=row["session_id"],
                message_id=row["message_id"],
                kind=SessionPartKind(row["kind"]),
                ordinal=row["ordinal"],
                text=row["text"],
                artifact_id=row["artifact_id"],
                run_id=row["run_id"],
                metadata=json.loads(row["metadata_json"] or "{}"),
                redaction_state=RedactionState(row["redaction_state"]),
                created_at=parse_dt(row["created_at"]),
            )
            for row in rows
        ]

    def record_session_message_retraction(
        self,
        session_id: str,
        message_id: str,
        *,
        reason: str | None = None,
    ) -> StoredEventRecord:
        self.get_session(session_id)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM session_messages WHERE id = ? AND session_id = ?",
                (message_id, session_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Session message not found: {message_id}")
        sanitized_reason = sanitize_for_logging(reason) if reason is not None else None
        return self.append_store_event(
            EventStreamType.SESSION,
            session_id,
            "session.message.retracted",
            {
                "message_id": message_id,
                "reason": sanitized_reason,
                "message_mutated": False,
                "parts_mutated": False,
                "permission_granting": False,
            },
            session_id=session_id,
            message_id=message_id,
            redaction_state=RedactionState.REDACTED if sanitized_reason else RedactionState.NOT_REQUIRED,
        )

    def record_session_part_correction(
        self,
        session_id: str,
        part_id: str,
        *,
        corrected_text: str,
        reason: str | None = None,
    ) -> StoredEventRecord:
        self.get_session(session_id)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, message_id FROM session_parts WHERE id = ? AND session_id = ?",
                (part_id, session_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Session part not found: {part_id}")
        sanitized_text = str(sanitize_for_logging(corrected_text))[:16 * 1024]
        sanitized_reason = sanitize_for_logging(reason) if reason is not None else None
        return self.append_store_event(
            EventStreamType.SESSION,
            session_id,
            "session.part.corrected",
            {
                "part_id": part_id,
                "message_id": row["message_id"],
                "corrected_text": sanitized_text,
                "reason": sanitized_reason,
                "part_mutated": False,
                "message_mutated": False,
                "permission_granting": False,
            },
            session_id=session_id,
            message_id=row["message_id"],
            redaction_state=RedactionState.REDACTED,
        )

    def record_session_part_retraction(
        self,
        session_id: str,
        part_id: str,
        *,
        reason: str | None = None,
    ) -> StoredEventRecord:
        self.get_session(session_id)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id, message_id FROM session_parts WHERE id = ? AND session_id = ?",
                (part_id, session_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Session part not found: {part_id}")
        sanitized_reason = sanitize_for_logging(reason) if reason is not None else None
        return self.append_store_event(
            EventStreamType.SESSION,
            session_id,
            "session.part.retracted",
            {
                "part_id": part_id,
                "message_id": row["message_id"],
                "reason": sanitized_reason,
                "part_deleted": False,
                "part_mutated": False,
                "message_mutated": False,
                "permission_granting": False,
            },
            session_id=session_id,
            message_id=row["message_id"],
            redaction_state=RedactionState.REDACTED if sanitized_reason else RedactionState.NOT_REQUIRED,
        )

    def append_session_todo(
        self,
        session_id: str,
        content: str,
        *,
        status: str = "pending",
        priority: int = 0,
        source_message_id: str | None = None,
    ) -> SessionTodoRecord:
        todo_id = f"todo_{uuid.uuid4().hex[:12]}"
        timestamp = now_iso()
        status_value = _normalize_session_todo_status(status)
        sanitized_content = str(sanitize_for_logging(content))[:16 * 1024]
        safety_evidence = _session_local_tool_evidence("todo")
        with self.connect() as conn:
            self._require_session(conn, session_id)
            if source_message_id is not None:
                row = conn.execute(
                    "SELECT id FROM session_messages WHERE id = ? AND session_id = ?",
                    (source_message_id, session_id),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Session message not found: {source_message_id}")
            conn.execute(
                """
                INSERT INTO session_todos (
                  id, session_id, content, status, priority, source_message_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (todo_id, session_id, sanitized_content, status_value, priority, source_message_id, timestamp, timestamp),
            )
        message = self.append_session_message(
            session_id,
            SessionMessageRole.TOOL,
            f"Todo {status_value}: {sanitized_content}",
            parent_message_id=source_message_id,
        )
        self.append_session_part(
            session_id,
            message.id,
            SessionPartKind.TODO_UPDATE,
            text=sanitized_content,
            metadata={"todo_id": todo_id, "status": status_value, "priority": priority, **safety_evidence},
            redaction_state=RedactionState.REDACTED,
        )
        self.append_store_event(
            EventStreamType.SESSION,
            session_id,
            "todo.updated",
            {"todo_id": todo_id, "status": status_value, "priority": priority, "summary": sanitized_content, **safety_evidence},
            session_id=session_id,
            message_id=message.id,
            redaction_state=RedactionState.REDACTED,
        )
        return SessionTodoRecord(
            id=todo_id,
            session_id=session_id,
            content=sanitized_content,
            status=status_value,
            priority=priority,
            source_message_id=source_message_id,
            created_at=parse_dt(timestamp),
            updated_at=parse_dt(timestamp),
        )

    def list_session_todos(self, session_id: str, status: str | None = None) -> list[SessionTodoRecord]:
        params: list[Any] = [session_id]
        where = "session_id = ?"
        if status is not None:
            where += " AND status = ?"
            params.append(_normalize_session_todo_status(status))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM session_todos WHERE {where} ORDER BY priority DESC, created_at ASC, id ASC",
                params,
            ).fetchall()
        return [self._row_to_session_todo(row) for row in rows]

    def append_session_question(
        self,
        session_id: str,
        question: str,
        *,
        choices: list[str] | None = None,
        source_message_id: str | None = None,
    ) -> SessionPartRecord:
        sanitized_question = str(sanitize_for_logging(question))[:16 * 1024]
        sanitized_choices = [str(sanitize_for_logging(choice))[:2048] for choice in choices or []]
        safety_evidence = _session_local_tool_evidence("question")
        message = self.append_session_message(
            session_id,
            SessionMessageRole.TOOL,
            sanitized_question,
            parent_message_id=source_message_id,
        )
        part = self.append_session_part(
            session_id,
            message.id,
            SessionPartKind.QUESTION,
            text=sanitized_question,
            metadata={"choices": sanitized_choices, **safety_evidence},
            redaction_state=RedactionState.REDACTED,
        )
        self.append_store_event(
            EventStreamType.SESSION,
            session_id,
            "question.requested",
            {"summary": sanitized_question, "choices": sanitized_choices, **safety_evidence},
            session_id=session_id,
            message_id=message.id,
            redaction_state=RedactionState.REDACTED,
        )
        return part

    def request_session_permission(
        self,
        session_id: str,
        *,
        tool_id: str,
        normalized_action: str,
        normalized_target_pattern: str,
        boundary_kind: SessionPermissionBoundaryKind | str,
        risk: str,
        run_id: str | None = None,
        scope: SessionPermissionScope | str = SessionPermissionScope.ONCE,
        source: SessionPermissionSource | str = SessionPermissionSource.POLICY,
        expires_at: datetime | str | None = None,
        policy_reasons: list[str] | None = None,
        revocable: bool = True,
    ) -> SessionPermissionRequest:
        permission_id = f"perm_{uuid.uuid4().hex[:12]}"
        requested_at = now_iso()
        boundary_value = SessionPermissionBoundaryKind(
            boundary_kind.value if isinstance(boundary_kind, SessionPermissionBoundaryKind) else boundary_kind
        )
        scope_value = SessionPermissionScope(scope.value if isinstance(scope, SessionPermissionScope) else scope)
        source_value = SessionPermissionSource(source.value if isinstance(source, SessionPermissionSource) else source)
        expiry = _permission_expiry_iso(expires_at, scope_value)
        reasons = [str(sanitize_for_logging(reason)) for reason in policy_reasons or []]
        action = str(sanitize_for_logging(normalized_action))
        target = str(sanitize_for_logging(normalized_target_pattern))
        clean_tool_id = str(sanitize_for_logging(tool_id))
        clean_risk = str(sanitize_for_logging(risk))
        with self.connect() as conn:
            self._require_session(conn, session_id)
            if run_id is not None:
                self._require_run(conn, run_id)
            conn.execute(
                """
                INSERT INTO session_permissions (
                  id, session_id, run_id, tool_id, normalized_action, normalized_target_pattern,
                  boundary_kind, risk, status, scope, source, revocable, policy_reasons_json,
                  requested_at, resolved_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    permission_id,
                    session_id,
                    run_id,
                    clean_tool_id,
                    action,
                    target,
                    boundary_value.value,
                    clean_risk,
                    SessionPermissionStatus.PENDING.value,
                    scope_value.value,
                    source_value.value,
                    1 if revocable else 0,
                    json.dumps(reasons, sort_keys=True, default=str),
                    requested_at,
                    None,
                    expiry,
                ),
            )
        self.append_store_event(
            EventStreamType.SESSION,
            session_id,
            "permission.requested",
            {
                "permission_id": permission_id,
                "tool_id": clean_tool_id,
                "normalized_action": action,
                "normalized_target_pattern": target,
                "boundary_kind": boundary_value.value,
                "risk": clean_risk,
                "scope": scope_value.value,
                "source": source_value.value,
                "expires_at": expiry,
                "summary": f"{clean_tool_id} {action} {target}",
                "policy_reasons": reasons,
            },
            session_id=session_id,
            run_id=run_id,
            redaction_state=RedactionState.REDACTED,
        )
        return self.get_session_permission(permission_id)

    def resolve_session_permission(
        self,
        permission_id: str,
        status: SessionPermissionStatus | str,
        *,
        source: SessionPermissionSource | str = SessionPermissionSource.USER,
        reason: str | None = None,
    ) -> SessionPermissionRequest:
        status_value = SessionPermissionStatus(status.value if isinstance(status, SessionPermissionStatus) else status)
        if status_value not in {SessionPermissionStatus.ALLOWED, SessionPermissionStatus.DENIED, SessionPermissionStatus.CANCELLED}:
            raise ValueError(f"Unsupported permission resolution status: {status_value.value}")
        source_value = SessionPermissionSource(source.value if isinstance(source, SessionPermissionSource) else source)
        resolved_at = now_iso()
        current = self.get_session_permission(permission_id)
        if current.status != SessionPermissionStatus.PENDING:
            raise ValueError(f"Permission request is not pending: {permission_id}")
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE session_permissions
                SET status = ?, source = ?, resolved_at = ?
                WHERE id = ?
                """,
                (status_value.value, source_value.value, resolved_at, permission_id),
            )
        resolved = self.get_session_permission(permission_id)
        self.append_store_event(
            EventStreamType.SESSION,
            resolved.session_id,
            "permission.resolved",
            {
                "permission_id": permission_id,
                "status": status_value.value,
                "source": source_value.value,
                "reason": str(sanitize_for_logging(reason)) if reason else None,
                "summary": f"{resolved.tool_id} {status_value.value}",
            },
            session_id=resolved.session_id,
            run_id=resolved.run_id,
            redaction_state=RedactionState.REDACTED,
        )
        return resolved

    def expire_session_permission(self, permission_id: str, *, reason: str | None = None) -> SessionPermissionRequest:
        current = self.get_session_permission(permission_id)
        if current.status == SessionPermissionStatus.EXPIRED:
            return current
        if current.status != SessionPermissionStatus.ALLOWED:
            raise ValueError(f"Permission request is not allowed: {permission_id}")
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE session_permissions
                SET status = ?, resolved_at = ?
                WHERE id = ?
                """,
                (SessionPermissionStatus.EXPIRED.value, timestamp, permission_id),
            )
        expired = self.get_session_permission(permission_id)
        self.append_store_event(
            EventStreamType.SESSION,
            expired.session_id,
            "permission.expired",
            {
                "permission_id": permission_id,
                "status": SessionPermissionStatus.EXPIRED.value,
                "reason": str(sanitize_for_logging(reason)) if reason else None,
                "summary": f"{expired.tool_id} expired",
            },
            session_id=expired.session_id,
            run_id=expired.run_id,
            redaction_state=RedactionState.REDACTED,
        )
        return expired

    def get_session_permission(self, permission_id: str) -> SessionPermissionRequest:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM session_permissions WHERE id = ?", (permission_id,)).fetchone()
        if row is None:
            raise KeyError(f"Session permission not found: {permission_id}")
        return self._row_to_session_permission(row)

    def list_session_permissions(
        self,
        session_id: str,
        status: SessionPermissionStatus | str | None = None,
    ) -> list[SessionPermissionRequest]:
        params: list[Any] = [session_id]
        where = "session_id = ?"
        if status is not None:
            status_value = SessionPermissionStatus(status.value if isinstance(status, SessionPermissionStatus) else status)
            where += " AND status = ?"
            params.append(status_value.value)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM session_permissions WHERE {where} ORDER BY requested_at ASC, id ASC",
                params,
            ).fetchall()
        return [self._row_to_session_permission(row) for row in rows]

    def attach_session_to_objective(self, session_id: str, objective_id: str) -> SessionSpec:
        self.get_session(session_id)
        with self.connect() as conn:
            self._require_objective(conn, objective_id)
            conn.execute("UPDATE objectives SET session_id = ? WHERE id = ?", (session_id, objective_id))
        return self.update_session(session_id, objective_id=objective_id)

    def attach_session_to_task(self, session_id: str, task_id: str) -> SessionSpec:
        self.get_session(session_id)
        with self.connect() as conn:
            self._require_task(conn, task_id)
            conn.execute("UPDATE tasks SET session_id = ? WHERE id = ?", (session_id, task_id))
        return self.update_session(session_id, active_task_id=task_id)

    def attach_session_to_run(self, session_id: str, run_id: str) -> SessionSpec:
        self.get_session(session_id)
        with self.connect() as conn:
            self._require_run(conn, run_id)
            conn.execute("UPDATE runs SET session_id = ? WHERE id = ?", (session_id, run_id))
            conn.execute("UPDATE events SET session_id = ? WHERE run_id = ?", (session_id, run_id))
            conn.execute("UPDATE artifacts SET session_id = ? WHERE run_id = ?", (session_id, run_id))
        self.write_run_manifest(run_id)
        return self.update_session(session_id, active_run_id=run_id)

    def clear_stale_session_active_run(
        self,
        session_id: str,
        *,
        missing_run_id: str,
        actor: str = "doctor",
    ) -> StoredEventRecord:
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT active_run_id FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                raise KeyError(f"Session not found: {session_id}")
            active_run_id = row["active_run_id"]
            if active_run_id != missing_run_id:
                raise ValueError(f"Session {session_id} active_run_id changed while repairing.")
            run_row = conn.execute("SELECT id FROM runs WHERE id = ?", (missing_run_id,)).fetchone()
            if run_row is not None:
                raise ValueError(f"Run exists and cannot be cleared as stale: {missing_run_id}")
            conn.execute(
                "UPDATE sessions SET active_run_id = NULL, updated_at = ? WHERE id = ?",
                (timestamp, session_id),
            )
        return self.append_store_event(
            EventStreamType.SESSION,
            session_id,
            "session.active_run_repaired",
            {
                "actor": actor,
                "missing_run_id": missing_run_id,
                "active_run_id_cleared": True,
                "mutation_scope": "session_active_run_pointer_only",
                "runs_deleted": False,
                "tasks_mutated": False,
                "artifacts_deleted": False,
                "messages_mutated": False,
                "events_deleted": False,
                "process_started": False,
                "provider_called": False,
                "network_called": False,
                "filesystem_modified": False,
                "permission_granting": False,
            },
            session_id=session_id,
            redaction_state=RedactionState.NOT_REQUIRED,
        )

    def disable_execution_control(
        self,
        target_kind: KillSwitchTargetKind | str,
        target_id: str,
        *,
        reason: str,
        actor: str = DEFAULT_TASK_LEASE_OWNER,
        metadata: dict[str, Any] | None = None,
    ) -> KillSwitchRecord:
        return self._set_execution_control(
            target_kind,
            target_id,
            disabled=True,
            reason=reason,
            actor=actor,
            metadata=metadata or {},
        )

    def enable_execution_control(
        self,
        target_kind: KillSwitchTargetKind | str,
        target_id: str,
        *,
        reason: str = "Control enabled.",
        actor: str = DEFAULT_TASK_LEASE_OWNER,
        metadata: dict[str, Any] | None = None,
    ) -> KillSwitchRecord:
        return self._set_execution_control(
            target_kind,
            target_id,
            disabled=False,
            reason=reason,
            actor=actor,
            metadata=metadata or {},
        )

    def _set_execution_control(
        self,
        target_kind: KillSwitchTargetKind | str,
        target_id: str,
        *,
        disabled: bool,
        reason: str,
        actor: str,
        metadata: dict[str, Any],
    ) -> KillSwitchRecord:
        kind = KillSwitchTargetKind(target_kind.value if isinstance(target_kind, KillSwitchTargetKind) else target_kind)
        normalized_target = str(target_id or "*")
        timestamp = now_iso()
        control_id = _execution_control_id(kind, normalized_target)
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM execution_controls WHERE target_kind = ? AND target_id = ?",
                (kind.value, normalized_target),
            ).fetchone()
            created_at = existing["created_at"] if existing is not None else timestamp
            conn.execute(
                """
                INSERT INTO execution_controls (
                  id, target_kind, target_id, disabled, reason, actor, created_at, updated_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(target_kind, target_id) DO UPDATE SET
                  disabled = excluded.disabled,
                  reason = excluded.reason,
                  actor = excluded.actor,
                  updated_at = excluded.updated_at,
                  metadata_json = excluded.metadata_json
                """,
                (
                    control_id,
                    kind.value,
                    normalized_target,
                    1 if disabled else 0,
                    str(sanitize_for_logging(reason)),
                    str(sanitize_for_logging(actor)),
                    created_at,
                    timestamp,
                    json.dumps(sanitize_for_logging(metadata), sort_keys=True, default=str),
                ),
            )
        return self.get_execution_control(kind, normalized_target)

    def get_execution_control(
        self,
        target_kind: KillSwitchTargetKind | str,
        target_id: str,
    ) -> KillSwitchRecord:
        kind = KillSwitchTargetKind(target_kind.value if isinstance(target_kind, KillSwitchTargetKind) else target_kind)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM execution_controls WHERE target_kind = ? AND target_id = ?",
                (kind.value, str(target_id or "*")),
            ).fetchone()
        if row is None:
            raise KeyError(f"Execution control not found: {kind.value}:{target_id}")
        return self._row_to_kill_switch(row)

    def list_execution_controls(self) -> list[KillSwitchRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM execution_controls ORDER BY target_kind ASC, target_id ASC"
            ).fetchall()
        return [self._row_to_kill_switch(row) for row in rows]

    def active_execution_controls(self) -> list[KillSwitchRecord]:
        return [control for control in self.list_execution_controls() if control.disabled]

    def _active_execution_controls_in_conn(self, conn: sqlite3.Connection) -> list[KillSwitchRecord]:
        rows = conn.execute(
            """
            SELECT * FROM execution_controls
            WHERE disabled = 1
            ORDER BY target_kind ASC, target_id ASC
            """
        ).fetchall()
        return [self._row_to_kill_switch(row) for row in rows]

    def reset_adapter_breaker(
        self,
        adapter_id: str,
        *,
        reason: str,
        actor: str = DEFAULT_TASK_LEASE_OWNER,
        metadata: dict[str, Any] | None = None,
    ) -> AdapterBreakerState:
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO execution_breaker_resets (
                  id, adapter_id, reason, actor, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    f"breaker_reset_{uuid.uuid4().hex[:12]}",
                    str(adapter_id),
                    str(sanitize_for_logging(reason)),
                    str(sanitize_for_logging(actor)),
                    timestamp,
                    json.dumps(sanitize_for_logging(metadata or {}), sort_keys=True, default=str),
                ),
            )
        return self.adapter_breaker_state(adapter_id)

    def adapter_breaker_state(
        self,
        adapter_id: str,
        *,
        threshold: int = ADAPTER_BREAKER_THRESHOLD,
        window_seconds: int = ADAPTER_BREAKER_WINDOW_SECONDS,
    ) -> AdapterBreakerState:
        adapter = str(adapter_id)
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        last_reset = self._latest_breaker_reset_at(adapter)
        effective_cutoff = max(cutoff, last_reset) if last_reset is not None else cutoff
        failures: list[DaemonEvent] = []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM daemon_events
                WHERE created_at >= ?
                ORDER BY created_at ASC, id ASC
                """,
                (effective_cutoff.isoformat(),),
            ).fetchall()
        for row in rows:
            event = self._row_to_daemon_event(row)
            if _event_counts_for_adapter_breaker(event, adapter):
                failures.append(event)
        opened_at = failures[threshold - 1].created_at if len(failures) >= threshold else None
        reasons = [
            str(sanitize_for_logging(event.metadata.get("error") or event.metadata.get("reason_code") or event.message))
            for event in failures[-threshold:]
        ]
        return AdapterBreakerState(
            adapter_id=adapter,
            status=BreakerStatus.OPEN if len(failures) >= threshold else BreakerStatus.CLOSED,
            failure_count=len(failures),
            threshold=threshold,
            window_seconds=window_seconds,
            opened_at=opened_at,
            last_reset_at=last_reset,
            reasons=reasons if len(failures) >= threshold else [],
        )

    def list_adapter_breaker_states(
        self,
        adapter_ids: list[str],
        *,
        threshold: int = ADAPTER_BREAKER_THRESHOLD,
        window_seconds: int = ADAPTER_BREAKER_WINDOW_SECONDS,
    ) -> list[AdapterBreakerState]:
        return [
            self.adapter_breaker_state(adapter_id, threshold=threshold, window_seconds=window_seconds)
            for adapter_id in adapter_ids
        ]

    def _latest_breaker_reset_at(self, adapter_id: str) -> datetime | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT created_at FROM execution_breaker_resets
                WHERE adapter_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (adapter_id,),
            ).fetchone()
        return parse_dt(row["created_at"]) if row is not None else None

    def connect(self) -> sqlite3.Connection:
        self.harness_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def inspect_required_session_schema(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return {
                "db_exists": False,
                "required_tables": list(REQUIRED_SESSION_SCHEMA_TABLES),
                "present_tables": [],
                "missing_tables": list(REQUIRED_SESSION_SCHEMA_TABLES),
                "ok": False,
                "repairable": False,
            }
        with self.connect() as conn:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        present = {str(row["name"]) for row in rows}
        missing = [table for table in REQUIRED_SESSION_SCHEMA_TABLES if table not in present]
        return {
            "db_exists": True,
            "required_tables": list(REQUIRED_SESSION_SCHEMA_TABLES),
            "present_tables": sorted(table for table in present if table in REQUIRED_SESSION_SCHEMA_TABLES),
            "missing_tables": missing,
            "ok": not missing,
            "repairable": bool(missing),
        }

    def create_run(
        self,
        goal: str | None,
        task_type: str | None,
        status: str = "created",
        backend: BackendConfig | None = None,
        approval_id: str | None = None,
        task_id: str | None = None,
        objective_id: str | None = None,
        session_id: str | None = None,
    ) -> RunRecord:
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        timestamp = now_iso()
        with self.connect() as conn:
            self._insert_run_in_conn(
                conn,
                run_id=run_id,
                timestamp=timestamp,
                goal=goal,
                task_type=task_type,
                status=status,
                backend=backend,
                approval_id=approval_id,
                task_id=task_id,
                objective_id=objective_id,
                session_id=session_id,
            )
        self.initialize_run_artifacts(run_id)
        if backend:
            self.persist_backend_snapshot(run_id, backend)
        self.write_run_manifest(run_id)
        if session_id is not None:
            self.update_session(session_id, active_run_id=run_id)
        return self.get_run(run_id)

    def _insert_run_in_conn(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        timestamp: str,
        goal: str | None,
        task_type: str | None,
        status: str,
        backend: BackendConfig | None,
        approval_id: str | None,
        task_id: str | None,
        objective_id: str | None,
        session_id: str | None = None,
    ) -> None:
        if task_id is not None:
            self._require_task(conn, task_id)
        if objective_id is not None:
            self._require_objective(conn, objective_id)
        if session_id is not None:
            row = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                raise KeyError(f"Session not found: {session_id}")
        conn.execute(
            """
            INSERT INTO runs (
              id, goal, task_type, status, project_root, created_at, updated_at,
              backend_name, backend_kind, billing_mode, execution_location,
              data_boundary, allow_network, approval_id, task_id, objective_id, session_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                sanitize_for_logging(goal) if goal is not None else None,
                task_type,
                status,
                str(self.project_root),
                timestamp,
                timestamp,
                backend.name if backend else None,
                backend.kind.value if backend else None,
                backend.metadata.billing_mode.value if backend else None,
                backend.metadata.execution_location.value if backend else None,
                backend.metadata.data_boundary.value if backend else None,
                int(backend.metadata.allow_network) if backend else None,
                approval_id,
                task_id,
                objective_id,
                session_id,
            ),
        )

    def initialize_run_artifacts(self, run_id: str) -> dict[str, Path]:
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        paths = {
            "events": run_dir / "events.jsonl",
            "transcript": run_dir / "transcript.jsonl",
            "final_report": run_dir / "final_report.md",
            "manifest": run_dir / "manifest.json",
        }
        for path in paths.values():
            path.touch(exist_ok=True)
        return paths

    def list_runs(self) -> list[RunRecord]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
        return [self._row_to_run(row) for row in rows]

    def prune_runs(self, keep: int = 20) -> list[str]:
        """Delete all unreferenced runs except the N most recent. Returns removed run ids."""
        if keep < 0:
            raise ValueError("--keep must be greater than or equal to 0.")
        runs = self.list_runs()
        if len(runs) <= keep:
            return []
        prune_ids = [run.id for run in runs[keep:]]
        removable_ids: list[str] = []
        with self.connect() as conn:
            for run_id in prune_ids:
                if self._run_has_external_references(conn, run_id):
                    continue
                removable_ids.append(run_id)
                conn.execute("DELETE FROM events WHERE run_id = ?", (run_id,))
                conn.execute("DELETE FROM artifacts WHERE run_id = ?", (run_id,))
                conn.execute("DELETE FROM backend_snapshots WHERE run_id = ?", (run_id,))
                conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        for run_id in removable_ids:
            run_dir = self.runs_dir / run_id
            if run_dir.exists():
                shutil.rmtree(run_dir)
        return removable_ids

    def _run_has_external_references(self, conn: sqlite3.Connection, run_id: str) -> bool:
        reference_queries = (
            "SELECT 1 FROM tasks WHERE run_id = ? LIMIT 1",
            "SELECT 1 FROM task_attempts WHERE run_id = ? LIMIT 1",
            "SELECT 1 FROM sessions WHERE active_run_id = ? LIMIT 1",
            "SELECT 1 FROM session_messages WHERE run_id = ? LIMIT 1",
            "SELECT 1 FROM session_parts WHERE run_id = ? LIMIT 1",
            "SELECT 1 FROM session_permissions WHERE run_id = ? LIMIT 1",
            "SELECT 1 FROM session_run_links WHERE run_id = ? LIMIT 1",
            "SELECT 1 FROM event_store WHERE run_id = ? LIMIT 1",
            "SELECT 1 FROM run_baselines WHERE run_id = ? LIMIT 1",
        )
        return any(conn.execute(query, (run_id,)).fetchone() is not None for query in reference_queries)

    def get_run(self, run_id: str) -> RunRecord:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Run not found: {run_id}")
        return self._row_to_run(row)

    def get_task_attempt(self, attempt_id: str) -> TaskAttempt:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM task_attempts WHERE id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise KeyError(f"Task attempt not found: {attempt_id}")
        return self._row_to_task_attempt(row)

    def get_task_lease(self, lease_id: str) -> TaskLease:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM task_leases WHERE id = ?", (lease_id,)).fetchone()
        if row is None:
            raise KeyError(f"Task lease not found: {lease_id}")
        return self._row_to_task_lease(row)

    def _require_active_lease_authority(self, lease: TaskLease, owner: str, *, action: str) -> None:
        if lease.status != TaskLeaseStatus.ACTIVE:
            raise ValueError(f"{action} requires active lease: {lease.status.value}")
        if lease.owner != owner:
            raise ValueError(f"Lease owner mismatch: lease is owned by {lease.owner}, not {owner}.")

    def _refreshed_task_lease_context(
        self,
        lease_or_id: TaskLease | str,
    ) -> tuple[TaskLease, TaskAttempt | None, TaskRecord | None]:
        lease = self.get_task_lease(lease_or_id.id if isinstance(lease_or_id, TaskLease) else lease_or_id)
        attempt = None
        if lease.attempt_id is not None:
            try:
                attempt = self.get_task_attempt(lease.attempt_id)
            except KeyError:
                attempt = None
        try:
            task = self.get_task(lease.task_id)
        except KeyError:
            task = None
        return lease, attempt, task

    def update_run_status(self, run_id: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                (status, now_iso(), run_id),
            )
        self.write_run_manifest(run_id)

    def create_objective(
        self,
        title: str,
        description: str = "",
        priority: int = 0,
        workbench_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
        status: str | ObjectiveStatus = ObjectiveStatus.ACTIVE,
    ) -> ObjectiveRecord:
        objective_id = f"obj_{uuid.uuid4().hex[:12]}"
        timestamp = now_iso()
        metadata = metadata or {}
        initial_status = normalize_objective_status(status)
        if initial_status not in {ObjectiveStatus.CREATED, ObjectiveStatus.ACTIVE}:
            raise ValueError(f"Objective creation supports only created or active status: {initial_status.value}")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO objectives (
                  id, title, description, status, project_root, created_at, updated_at,
                  priority, workbench_id, metadata_json, session_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    objective_id,
                    str(sanitize_for_logging(title)),
                    str(sanitize_for_logging(description)),
                    initial_status.value,
                    str(self.project_root),
                    timestamp,
                    timestamp,
                    priority,
                    workbench_id,
                    json.dumps(sanitize_for_logging(metadata), sort_keys=True, default=str),
                    session_id,
                ),
            )
        if session_id is not None:
            self.update_session(session_id, objective_id=objective_id)
        return self.get_objective(objective_id)

    def list_objectives(self) -> list[ObjectiveRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM objectives ORDER BY priority DESC, created_at ASC"
            ).fetchall()
        return [self._row_to_objective(row) for row in rows]

    def get_objective(self, objective_id: str) -> ObjectiveRecord:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM objectives WHERE id = ?", (objective_id,)).fetchone()
        if row is None:
            raise KeyError(f"Objective not found: {objective_id}")
        return self._row_to_objective(row)

    def update_objective_status(
        self,
        objective_id: str,
        status: str | ObjectiveStatus,
        *,
        reason: str = "",
        actor: str = "operator",
        metadata: dict[str, Any] | None = None,
    ) -> ObjectiveRecord:
        next_status = normalize_objective_status(status)
        timestamp = now_iso()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM objectives WHERE id = ?", (objective_id,)).fetchone()
            if row is None:
                raise KeyError(f"Objective not found: {objective_id}")
            objective = self._row_to_objective(row)
            validate_objective_transition(objective.status, next_status)
            objective_metadata = dict(objective.metadata)
            raw_events = objective_metadata.get("lifecycle_events")
            lifecycle_events = list(raw_events) if isinstance(raw_events, list) else []
            lifecycle_event = sanitize_for_logging(
                {
                    "from_status": objective.status.value,
                    "to_status": next_status.value,
                    "reason": reason,
                    "actor": actor,
                    "created_at": timestamp,
                    "metadata": metadata or {},
                }
            )
            lifecycle_events.append(lifecycle_event)
            objective_metadata["lifecycle_events"] = lifecycle_events[-50:]
            objective_metadata["last_lifecycle_event"] = lifecycle_event
            conn.execute(
                """
                UPDATE objectives
                SET status = ?, updated_at = ?, metadata_json = ?
                WHERE id = ?
                """,
                (
                    next_status.value,
                    timestamp,
                    json.dumps(sanitize_for_logging(objective_metadata), sort_keys=True, default=str),
                    objective_id,
                ),
            )
        return self.get_objective(objective_id)

    def retry_objective(
        self,
        objective_id: str,
        *,
        reason: str = "",
        actor: str = "operator",
        metadata: dict[str, Any] | None = None,
    ) -> tuple[ObjectiveRecord, list[TaskRecord]]:
        objective = self.get_objective(objective_id)
        tasks = self.list_tasks(objective_id=objective_id)
        failed_tasks = [task for task in tasks if task.status == TaskStatus.FAILED]
        if objective.status not in {ObjectiveStatus.ACTIVE, ObjectiveStatus.TIMED_OUT}:
            raise ValueError(f"Objective retry requires active or timed_out status: {objective.status.value}")
        if objective.status == ObjectiveStatus.ACTIVE and not failed_tasks:
            raise ValueError("Objective retry requires failed tasks for active objectives")
        replay_rejections = [
            rejection
            for task in failed_tasks
            if (rejection := self._task_retry_replay_rejection(task))
        ]
        if replay_rejections:
            raise ValueError("; ".join(replay_rejections))
        retry_metadata = {
            "source": "objective_retry",
            "failed_task_count": len(failed_tasks),
            "retried_task_ids": [task.id for task in failed_tasks],
            **(metadata or {}),
        }
        retrying = self.update_objective_status(
            objective_id,
            ObjectiveStatus.RETRYING,
            reason=reason or "Objective retry started.",
            actor=actor,
            metadata=retry_metadata,
        )
        retried_tasks = [self.retry_task(task.id) for task in failed_tasks]
        retried_statuses = {task.id: task.status.value for task in retried_tasks}
        objective = self.update_objective_status(
            retrying.id,
            ObjectiveStatus.ACTIVE,
            reason=reason or "Objective retry ready.",
            actor=actor,
            metadata={
                **retry_metadata,
                "retried_task_statuses": retried_statuses,
            },
        )
        return objective, retried_tasks

    def import_project_agent(self, loaded_bundle: LoadedAgentBundle) -> ProjectAgentRecord:
        agent_id = loaded_bundle.bundle.agent.id
        imported_at = now_iso()
        agent_json = loaded_bundle.bundle.agent.model_dump(mode="json")
        profiles_json = [profile.model_dump(mode="json") for profile in sorted(loaded_bundle.profiles, key=lambda item: item.id)]
        with self.connect() as conn:
            existing = conn.execute("SELECT agent_id FROM project_agents WHERE agent_id = ?", (agent_id,)).fetchone()
            if existing is not None:
                raise ValueError(f"Project agent already imported: {agent_id}")
            conn.execute(
                """
                INSERT INTO project_agents (
                  agent_id, workbench_id, project_root, imported_at, source_path,
                  content_sha256, agent_json, profiles_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    loaded_bundle.bundle.workbench_id,
                    str(self.project_root),
                    imported_at,
                    str(loaded_bundle.source_path),
                    agent_bundle_content_sha256(loaded_bundle),
                    json.dumps(sanitize_for_logging(agent_json), sort_keys=True, default=str),
                    json.dumps(sanitize_for_logging(profiles_json), sort_keys=True, default=str),
                ),
            )
        return self.get_project_agent(agent_id)

    def list_project_agents(self) -> list[ProjectAgentRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM project_agents ORDER BY workbench_id ASC, imported_at ASC, agent_id ASC"
            ).fetchall()
        return [self._row_to_project_agent(row) for row in rows]

    def get_project_agent(self, agent_id: str) -> ProjectAgentRecord:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM project_agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if row is None:
            raise KeyError(f"Project agent not found: {agent_id}")
        return self._row_to_project_agent(row)

    def project_agent_drift_status(self, agent_id: str) -> dict[str, Any]:
        record = self.get_project_agent(agent_id)
        if not record.source_path.exists():
            return {
                "status": "missing",
                "imported_sha256": record.content_sha256,
                "current_sha256": None,
                "message": f"Source bundle missing: {record.source_path}",
            }
        try:
            loaded = load_agent_bundle(record.source_path)
            current_sha256 = agent_bundle_content_sha256(loaded)
        except AgentBundleError as exc:
            return {
                "status": "unavailable",
                "imported_sha256": record.content_sha256,
                "current_sha256": None,
                "message": str(exc),
            }
        return {
            "status": "verified" if current_sha256 == record.content_sha256 else "changed",
            "imported_sha256": record.content_sha256,
            "current_sha256": current_sha256,
            "message": None,
        }

    def preview_project_agent(self, agent_id: str) -> dict[str, Any]:
        record = self.get_project_agent(agent_id)
        registry = self._project_agent_registry(record)
        preview = preview_agent_effective_policy(registry, agent_id)
        workbench = registry.get_workbench(record.workbench_id)
        return {
            "schema_version": "harness.project_agent_preview/v1",
            "ok": True,
            "agent_id": record.agent_id,
            "workbench_id": record.workbench_id,
            "source_path": str(record.source_path),
            "imported_at": record.imported_at.isoformat(),
            "content_sha256": record.content_sha256,
            "drift": self.project_agent_drift_status(agent_id),
            "agent": preview["agent"],
            "profiles": preview["profiles"],
            "parent_chain": preview["parent_chain"],
            "effective_agent": preview["effective_agent"],
            "workbench": _sort_json(workbench.model_dump(mode="json")),
            "errors": [],
            "warnings": [],
        }

    def remove_project_agent(self, agent_id: str) -> ProjectAgentRecord:
        if agent_id in builtin_spec_registry().agents:
            raise ValueError(f"Cannot remove built-in agent: {agent_id}")
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM project_agents WHERE agent_id = ?", (agent_id,)).fetchone()
            if row is None:
                raise KeyError(f"Project agent not found: {agent_id}")
            task_count = conn.execute(
                "SELECT COUNT(*) AS count FROM tasks WHERE agent_id = ? AND spec_source_kind = 'project'",
                (agent_id,),
            ).fetchone()["count"]
            if task_count:
                raise ValueError(f"Cannot remove project agent referenced by tasks: {agent_id}")
            conn.execute("DELETE FROM project_agents WHERE agent_id = ?", (agent_id,))
        return self._row_to_project_agent(row)

    def create_task(
        self,
        title: str,
        description: str = "",
        priority: int = 0,
        objective_id: str | None = None,
        workbench_id: str | None = None,
        agent_id: str | None = None,
        spec_source_kind: str | None = None,
        spec_source_path: Path | None = None,
        depends_on: list[str] | None = None,
        required_approvals: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        session_id: str | None = None,
    ) -> TaskRecord:
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        idempotency_key = idempotency_key or f"task_idem_{uuid.uuid4().hex[:16]}"
        timestamp = now_iso()
        depends_on = depends_on or []
        required_approvals = required_approvals or []
        metadata = metadata or {}
        with self.connect() as conn:
            existing = conn.execute("SELECT * FROM tasks WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if existing is not None:
                return self._row_to_task(existing)
            _validate_registered_execution_task_payload(metadata, agent_id=agent_id, depends_on=depends_on)
            if objective_id is not None:
                self._require_objective(conn, objective_id)
            if session_id is not None:
                row = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
                if row is None:
                    raise KeyError(f"Session not found: {session_id}")
            for dependency_id in depends_on:
                self._require_task(conn, dependency_id)
            dependencies_satisfied = self._dependency_ids_completed(conn, depends_on)
            initial_status = (
                TaskStatus.WAITING_APPROVAL
                if required_approvals
                else TaskStatus.BLOCKED
                if not dependencies_satisfied
                else TaskStatus.READY
            )
            conn.execute(
                """
                INSERT INTO tasks (
                  id, title, description, status, project_root, created_at, updated_at,
                  priority, objective_id, workbench_id, agent_id, spec_source_kind, spec_source_path,
                  depends_on_json, run_id, metadata_json, idempotency_key,
                  required_approvals_json, approval_state, session_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    str(sanitize_for_logging(title)),
                    str(sanitize_for_logging(description)),
                    initial_status.value,
                    str(self.project_root),
                    timestamp,
                    timestamp,
                    priority,
                    objective_id,
                    workbench_id,
                    agent_id,
                    spec_source_kind,
                    str(spec_source_path) if spec_source_path is not None else None,
                    json.dumps(depends_on, sort_keys=True),
                    None,
                    json.dumps(sanitize_for_logging(metadata), sort_keys=True, default=str),
                    idempotency_key,
                    json.dumps(sanitize_for_logging(required_approvals), sort_keys=True, default=str),
                    "required" if required_approvals else None,
                    session_id,
                ),
            )
            for dependency_id in depends_on:
                self._create_task_dependency(
                    conn,
                    upstream_task_id=dependency_id,
                    downstream_task_id=task_id,
                    dependency_type=TaskDependencyType.SUCCESS,
                    required_artifact_kind=None,
                    created_at=timestamp,
                )
            self._record_task_transition(
                conn,
                task_id=task_id,
                from_status=None,
                to_status=initial_status,
                reason="task_created",
                actor="system",
                metadata={},
                created_at=timestamp,
            )
        if session_id is not None:
            self.update_session(session_id, active_task_id=task_id)
        return self.get_task(task_id)

    def list_tasks(self, status: str | None = None, objective_id: str | None = None) -> list[TaskRecord]:
        with self.connect() as conn:
            if objective_id is not None:
                self._require_objective(conn, objective_id)
            if status is None:
                if objective_id is None:
                    rows = conn.execute(
                        "SELECT * FROM tasks ORDER BY priority DESC, created_at ASC"
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT * FROM tasks
                        WHERE objective_id = ?
                        ORDER BY priority DESC, created_at ASC
                        """,
                        (objective_id,),
                    ).fetchall()
            else:
                query_status = normalize_task_status(status)
                status_values = TASK_STATUS_QUERY_ALIASES.get(query_status, (query_status.value,))
                placeholders = ", ".join("?" for _ in status_values)
                if objective_id is None:
                    rows = conn.execute(
                        f"""
                        SELECT * FROM tasks
                        WHERE status IN ({placeholders})
                        ORDER BY priority DESC, created_at ASC
                        """,
                        status_values,
                    ).fetchall()
                else:
                    rows = conn.execute(
                        f"""
                        SELECT * FROM tasks
                        WHERE status IN ({placeholders}) AND objective_id = ?
                        ORDER BY priority DESC, created_at ASC
                        """,
                        (*status_values, objective_id),
                    ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def get_task(self, task_id: str) -> TaskRecord:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Task not found: {task_id}")
        return self._row_to_task(row)

    def update_task_metadata(self, task_id: str, metadata_patch: dict[str, Any]) -> TaskRecord:
        current = self.get_task(task_id)
        metadata = dict(current.metadata or {})
        metadata.update(metadata_patch)
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(sanitize_for_logging(metadata), sort_keys=True, default=str),
                    timestamp,
                    task_id,
                ),
            )
        return self.get_task(task_id)

    def save_memory_note(
        self,
        scope_type: str | MemoryScopeType,
        scope_id: str,
        summary: str,
    ) -> MemoryRecord:
        scope = MemoryScopeType(scope_type.value if isinstance(scope_type, MemoryScopeType) else scope_type)
        trimmed = summary.strip()
        if not trimmed:
            raise ValueError("Memory note summary cannot be empty.")
        findings = scan_text_for_secrets(trimmed)
        stored_summary = redact_secret_text(trimmed) if findings else trimmed
        redaction_state = MemoryRedactionState.REDACTED if findings else MemoryRedactionState.NOT_REQUIRED
        encoded = stored_summary.encode("utf-8")
        memory_id = f"mem_{uuid.uuid4().hex[:12]}"
        timestamp = now_iso()
        lineage = {
            "source": "operator_note",
            "secret_findings": [finding.to_dict() for finding in findings],
            "permission_granting": False,
            "policy_authority": False,
            "approval_authority": False,
            "redaction_state": redaction_state.value,
            "authority_claims_stripped": _authority_claim_codes(trimmed),
        }
        record = MemoryRecord(
            id=memory_id,
            scope_type=scope,
            scope_id=scope_id,
            source_kind=MemorySourceKind.OPERATOR_NOTE,
            source_id=memory_id,
            source_artifact_id=None,
            summary=stored_summary,
            redaction_state=redaction_state,
            sha256=hashlib.sha256(encoded).hexdigest(),
            size_bytes=len(encoded),
            created_at=parse_dt(timestamp),
            updated_at=parse_dt(timestamp),
            lineage=sanitize_for_logging(lineage),
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_records (
                  id, scope_type, scope_id, source_kind, source_id, source_artifact_id,
                  summary, redaction_state, sha256, size_bytes, created_at, updated_at, lineage_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.scope_type.value,
                    record.scope_id,
                    record.source_kind.value,
                    record.source_id,
                    record.source_artifact_id,
                    record.summary,
                    record.redaction_state.value,
                    record.sha256,
                    record.size_bytes,
                    timestamp,
                    timestamp,
                    json.dumps(record.lineage, sort_keys=True, default=str),
                ),
            )
        return record

    def save_derived_memory(
        self,
        scope_type: str | MemoryScopeType,
        scope_id: str,
        source_kind: str | MemorySourceKind,
        summary: str,
        *,
        source_id: str,
        source_artifact_id: str | None = None,
    ) -> MemoryRecord:
        scope = MemoryScopeType(scope_type.value if isinstance(scope_type, MemoryScopeType) else scope_type)
        kind = MemorySourceKind(source_kind.value if isinstance(source_kind, MemorySourceKind) else source_kind)
        trimmed = summary.strip()
        if not trimmed:
            raise ValueError("Derived memory summary cannot be empty.")
        source_links = self._validate_derived_memory_source(kind, source_id, source_artifact_id)
        findings = scan_text_for_secrets(trimmed)
        stored_summary = redact_secret_text(trimmed) if findings else trimmed
        redaction_state = MemoryRedactionState.REDACTED if findings else MemoryRedactionState.NOT_REQUIRED
        encoded = stored_summary.encode("utf-8")
        memory_id = f"mem_{uuid.uuid4().hex[:12]}"
        timestamp = now_iso()
        lineage = {
            "source": kind.value,
            "source_id": source_id,
            "source_artifact_id": source_artifact_id,
            **source_links,
            "secret_findings": [finding.to_dict() for finding in findings],
            "permission_granting": False,
            "policy_authority": False,
            "approval_authority": False,
            "redaction_state": redaction_state.value,
            "authority_claims_stripped": _authority_claim_codes(trimmed),
        }
        record = MemoryRecord(
            id=memory_id,
            scope_type=scope,
            scope_id=scope_id,
            source_kind=kind,
            source_id=source_id,
            source_artifact_id=source_artifact_id,
            summary=stored_summary,
            redaction_state=redaction_state,
            sha256=hashlib.sha256(encoded).hexdigest(),
            size_bytes=len(encoded),
            created_at=parse_dt(timestamp),
            updated_at=parse_dt(timestamp),
            lineage=sanitize_for_logging(lineage),
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_records (
                  id, scope_type, scope_id, source_kind, source_id, source_artifact_id,
                  summary, redaction_state, sha256, size_bytes, created_at, updated_at, lineage_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.scope_type.value,
                    record.scope_id,
                    record.source_kind.value,
                    record.source_id,
                    record.source_artifact_id,
                    record.summary,
                    record.redaction_state.value,
                    record.sha256,
                    record.size_bytes,
                    timestamp,
                    timestamp,
                    json.dumps(record.lineage, sort_keys=True, default=str),
                ),
            )
        return record

    def _validate_derived_memory_source(
        self,
        source_kind: MemorySourceKind,
        source_id: str,
        source_artifact_id: str | None,
    ) -> dict[str, Any]:
        if source_kind == MemorySourceKind.ARTIFACT_SUMMARY:
            if not source_artifact_id:
                raise ValueError("artifact_summary memory requires source_artifact_id.")
            artifact = self.get_artifact(source_artifact_id)
            return {
                "source_run_id": artifact.run_id,
                "source_artifact_kind": artifact.kind,
                "source_artifact_sha256": artifact.sha256,
                "source_artifact_redaction_state": artifact.redaction_state,
            }
        if source_kind == MemorySourceKind.OBJECTIVE_STATE:
            objective = self.get_objective(source_id)
            return {"source_objective_id": objective.id, "source_objective_status": objective.status.value}
        if source_kind == MemorySourceKind.RUN_REVIEW:
            run = self.get_run(source_id)
            return {"source_run_id": run.id, "source_run_status": run.status, "source_task_id": run.task_id}
        if source_kind == MemorySourceKind.FAILED_ATTEMPT_SUMMARY:
            attempt = self.get_task_attempt(source_id)
            return {
                "source_attempt_id": attempt.id,
                "source_task_id": attempt.task_id,
                "source_run_id": attempt.run_id,
                "source_attempt_status": attempt.status.value,
            }
        raise ValueError(f"Unsupported derived memory source: {source_kind.value}")

    def list_memory_records(
        self,
        scope_type: str | MemoryScopeType | None = None,
        scope_id: str | None = None,
        *,
        include_forgotten: bool = False,
    ) -> list[MemoryRecord]:
        filters: list[str] = []
        params: list[Any] = []
        if scope_type is not None:
            scope = MemoryScopeType(scope_type.value if isinstance(scope_type, MemoryScopeType) else scope_type)
            filters.append("scope_type = ?")
            params.append(scope.value)
        if scope_id is not None:
            filters.append("scope_id = ?")
            params.append(scope_id)
        if not include_forgotten:
            filters.append("redaction_state != ?")
            params.append(MemoryRedactionState.FORGOTTEN.value)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        try:
            with self.connect() as conn:
                rows = conn.execute(
                    f"""
                    SELECT * FROM memory_records
                    {where}
                    ORDER BY created_at DESC, id DESC
                    """,
                    params,
                ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table: memory_records" in str(exc):
                return []
            raise
        return [self._row_to_memory_record(row) for row in rows]

    def get_memory_record(self, memory_id: str) -> MemoryRecord:
        try:
            with self.connect() as conn:
                row = conn.execute("SELECT * FROM memory_records WHERE id = ?", (memory_id,)).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table: memory_records" in str(exc):
                raise KeyError(f"Memory record not found: {memory_id}") from exc
            raise
        if row is None:
            raise KeyError(f"Memory record not found: {memory_id}")
        return self._row_to_memory_record(row)

    def forget_memory_record(self, memory_id: str) -> MemoryRecord:
        current = self.get_memory_record(memory_id)
        timestamp = now_iso()
        lineage = dict(current.lineage)
        lineage["forgotten_at"] = timestamp
        with self.connect() as conn:
            conn.execute(
                """
                DELETE FROM context_vectors
                WHERE chunk_id IN (SELECT id FROM context_chunks WHERE memory_id = ?)
                """,
                (memory_id,),
            )
            conn.execute("DELETE FROM context_chunks WHERE memory_id = ?", (memory_id,))
            conn.execute(
                """
                UPDATE memory_records
                SET summary = ?, redaction_state = ?, updated_at = ?, lineage_json = ?
                WHERE id = ?
                """,
                (
                    "[FORGOTTEN]",
                    MemoryRedactionState.FORGOTTEN.value,
                    timestamp,
                    json.dumps(sanitize_for_logging(lineage), sort_keys=True, default=str),
                    memory_id,
                ),
            )
        return self.get_memory_record(memory_id)

    def upsert_context_chunk(self, chunk: Any) -> Any:
        payload = chunk.to_row() if hasattr(chunk, "to_row") else dict(chunk)
        timestamp = now_iso()
        with self.connect() as conn:
            existing = conn.execute("SELECT created_at FROM context_chunks WHERE id = ?", (payload["id"],)).fetchone()
            created_at = existing["created_at"] if existing is not None else timestamp
            conn.execute(
                """
                INSERT INTO context_chunks (
                  id, schema_version, source_kind, trust_level, path, source_id, artifact_id, memory_id,
                  start_line, end_line, sha256, size_bytes, token_count, tokenizer, chunk_scheme,
                  text_preview, redaction_state, warnings_json, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  schema_version = excluded.schema_version,
                  source_kind = excluded.source_kind,
                  trust_level = excluded.trust_level,
                  path = excluded.path,
                  source_id = excluded.source_id,
                  artifact_id = excluded.artifact_id,
                  memory_id = excluded.memory_id,
                  start_line = excluded.start_line,
                  end_line = excluded.end_line,
                  sha256 = excluded.sha256,
                  size_bytes = excluded.size_bytes,
                  token_count = excluded.token_count,
                  tokenizer = excluded.tokenizer,
                  chunk_scheme = excluded.chunk_scheme,
                  text_preview = excluded.text_preview,
                  redaction_state = excluded.redaction_state,
                  warnings_json = excluded.warnings_json,
                  metadata_json = excluded.metadata_json,
                  updated_at = excluded.updated_at
                """,
                (
                    payload["id"],
                    payload["schema_version"],
                    payload["source_kind"],
                    payload["trust_level"],
                    payload.get("path"),
                    payload.get("source_id"),
                    payload.get("artifact_id"),
                    payload.get("memory_id"),
                    payload.get("start_line"),
                    payload.get("end_line"),
                    payload["sha256"],
                    payload["size_bytes"],
                    payload.get("token_count"),
                    payload.get("tokenizer"),
                    payload["chunk_scheme"],
                    payload["text_preview"],
                    payload.get("redaction_state"),
                    payload["warnings_json"],
                    payload["metadata_json"],
                    created_at,
                    timestamp,
                ),
            )
        return chunk

    def list_context_chunks(
        self,
        *,
        source_kind: str | None = None,
        path: str | None = None,
        memory_id: str | None = None,
        artifact_id: str | None = None,
    ) -> list[Any]:
        filters: list[str] = []
        params: list[Any] = []
        if source_kind is not None:
            filters.append("source_kind = ?")
            params.append(source_kind)
        if path is not None:
            filters.append("path = ?")
            params.append(path)
        if memory_id is not None:
            filters.append("memory_id = ?")
            params.append(memory_id)
        if artifact_id is not None:
            filters.append("artifact_id = ?")
            params.append(artifact_id)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM context_chunks
                {where}
                ORDER BY source_kind ASC, path ASC, start_line ASC, id ASC
                """,
                params,
            ).fetchall()
        return [self._row_to_context_chunk(row) for row in rows]

    def delete_context_chunks_for_memory(self, memory_id: str) -> int:
        with self.connect() as conn:
            conn.execute(
                """
                DELETE FROM context_vectors
                WHERE chunk_id IN (SELECT id FROM context_chunks WHERE memory_id = ?)
                """,
                (memory_id,),
            )
            cursor = conn.execute("DELETE FROM context_chunks WHERE memory_id = ?", (memory_id,))
            return int(cursor.rowcount or 0)

    def delete_context_chunks_for_source_path(
        self,
        source_kind: str,
        path: str,
        *,
        keep_ids: set[str] | None = None,
        chunk_scheme: str | None = None,
        tokenizer: str | None = None,
    ) -> int:
        filters = ["source_kind = ?", "path = ?"]
        params: list[Any] = [source_kind, path]
        if chunk_scheme is not None:
            filters.append("chunk_scheme = ?")
            params.append(chunk_scheme)
        if tokenizer is not None:
            filters.append("tokenizer = ?")
            params.append(tokenizer)
        if keep_ids:
            placeholders = ", ".join("?" for _ in keep_ids)
            filters.append(f"id NOT IN ({placeholders})")
            params.extend(sorted(keep_ids))
        with self.connect() as conn:
            vector_filters = list(filters)
            vector_params = list(params)
            conn.execute(
                f"""
                DELETE FROM context_vectors
                WHERE chunk_id IN (
                  SELECT id FROM context_chunks WHERE {' AND '.join(vector_filters)}
                )
                """,
                vector_params,
            )
            cursor = conn.execute(f"DELETE FROM context_chunks WHERE {' AND '.join(filters)}", params)
            return int(cursor.rowcount or 0)

    def upsert_context_vector(self, vector: Any) -> Any:
        payload = vector.to_row() if hasattr(vector, "to_row") else dict(vector)
        timestamp = now_iso()
        with self.connect() as conn:
            existing = conn.execute("SELECT created_at FROM context_vectors WHERE id = ?", (payload["id"],)).fetchone()
            created_at = existing["created_at"] if existing is not None else timestamp
            conn.execute(
                """
                INSERT INTO context_vectors (
                  id, schema_version, chunk_id, embedding_provider_id, dimension,
                  quantization, source_sha256, vector_json, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  schema_version = excluded.schema_version,
                  chunk_id = excluded.chunk_id,
                  embedding_provider_id = excluded.embedding_provider_id,
                  dimension = excluded.dimension,
                  quantization = excluded.quantization,
                  source_sha256 = excluded.source_sha256,
                  vector_json = excluded.vector_json,
                  metadata_json = excluded.metadata_json,
                  updated_at = excluded.updated_at
                """,
                (
                    payload["id"],
                    payload["schema_version"],
                    payload["chunk_id"],
                    payload["embedding_provider_id"],
                    payload["dimension"],
                    payload["quantization"],
                    payload["source_sha256"],
                    payload["vector_json"],
                    payload["metadata_json"],
                    created_at,
                    timestamp,
                ),
            )
        return vector

    def list_context_vectors(
        self,
        *,
        embedding_provider_id: str | None = None,
        chunk_id: str | None = None,
    ) -> list[Any]:
        filters: list[str] = []
        params: list[Any] = []
        if embedding_provider_id is not None:
            filters.append("embedding_provider_id = ?")
            params.append(embedding_provider_id)
        if chunk_id is not None:
            filters.append("chunk_id = ?")
            params.append(chunk_id)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM context_vectors
                {where}
                ORDER BY embedding_provider_id ASC, chunk_id ASC, id ASC
                """,
                params,
            ).fetchall()
        return [self._row_to_context_vector(row) for row in rows]

    def delete_context_vectors_not_in(self, chunk_ids: set[str], *, embedding_provider_id: str) -> int:
        filters = ["embedding_provider_id = ?"]
        params: list[Any] = [embedding_provider_id]
        if chunk_ids:
            placeholders = ", ".join("?" for _ in chunk_ids)
            filters.append(f"chunk_id NOT IN ({placeholders})")
            params.extend(sorted(chunk_ids))
        with self.connect() as conn:
            cursor = conn.execute(f"DELETE FROM context_vectors WHERE {' AND '.join(filters)}", params)
            return int(cursor.rowcount or 0)

    def stale_context_chunks(
        self,
        *,
        source_kind: str,
        path: str,
        sha256: str | None = None,
        sha256_values: set[str] | None = None,
        chunk_scheme: str | None = None,
        tokenizer: str | None = None,
    ) -> list[Any]:
        filters = ["source_kind = ?", "path = ?"]
        params: list[Any] = [source_kind, path]
        if sha256_values is not None:
            if sha256_values:
                placeholders = ", ".join("?" for _ in sha256_values)
                filters.append(f"sha256 NOT IN ({placeholders})")
                params.extend(sorted(sha256_values))
        elif sha256 is not None:
            filters.append("sha256 != ?")
            params.append(sha256)
        if chunk_scheme is not None:
            filters.append("chunk_scheme = ?")
            params.append(chunk_scheme)
        if tokenizer is not None:
            filters.append("tokenizer = ?")
            params.append(tokenizer)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM context_chunks
                WHERE {' AND '.join(filters)}
                ORDER BY updated_at ASC, id ASC
                """,
                params,
            ).fetchall()
        return [self._row_to_context_chunk(row) for row in rows]

    def update_task_status(
        self,
        task_id: str,
        status: str | TaskStatus,
        *,
        run_id: str | None = None,
    ) -> TaskRecord:
        next_status = normalize_task_status(status)
        if run_id is not None:
            self.get_run(run_id)
        timestamp = now_iso()
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Task not found: {task_id}")
            current_status = normalize_task_status(row["status"])
            validate_task_transition(current_status, next_status)
            result = conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ?, run_id = COALESCE(?, run_id) WHERE id = ?",
                (next_status.value, timestamp, run_id, task_id),
            )
            if current_status != next_status:
                self._record_task_transition(
                    conn,
                    task_id=task_id,
                    from_status=current_status,
                    to_status=next_status,
                    reason="status_updated",
                    actor="operator",
                    metadata={"run_id": run_id} if run_id is not None else {},
                    created_at=timestamp,
                )
        if result.rowcount == 0:
            raise KeyError(f"Task not found: {task_id}")
        return self.get_task(task_id)

    def cancel_task(self, task_id: str) -> TaskRecord:
        task = self.get_task(task_id)
        if task.status == TaskStatus.CANCELLED:
            raise ValueError("Invalid task transition: cancelled -> cancelled")
        return self.update_task_status(task.id, TaskStatus.CANCELLED)

    def retry_task(self, task_id: str) -> TaskRecord:
        timestamp = now_iso()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Task not found: {task_id}")
            task = self._row_to_task(row)
            if task.status != TaskStatus.FAILED:
                raise ValueError(f"Task retry requires failed status: {task.status.value}")
            replay_rejection = self._task_retry_replay_rejection(task)
            if replay_rejection:
                raise ValueError(replay_rejection)
            next_status = (
                TaskStatus.WAITING_APPROVAL
                if task.required_approvals
                else TaskStatus.BLOCKED
                if not self._dependency_ids_completed(conn, task.depends_on)
                else TaskStatus.READY
            )
            validate_task_transition(task.status, next_status)
            replay_receipt = self._task_retry_replay_receipt(
                conn,
                task=task,
                next_status=next_status,
                created_at=timestamp,
            )
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (next_status.value, timestamp, task.id),
            )
            self._record_task_transition(
                conn,
                task_id=task.id,
                from_status=task.status,
                to_status=next_status,
                reason="task_retry_authorized",
                actor="operator",
                metadata={"replay_receipt": replay_receipt},
                created_at=timestamp,
            )
        return self.get_task(task_id)

    def _task_retry_replay_rejection(self, task: TaskRecord) -> str | None:
        adapter_id = task.metadata.get("execution_adapter")
        if not isinstance(adapter_id, str) or not adapter_id.strip():
            return None
        from harness.execution import get_execution_adapter_descriptor

        descriptor = get_execution_adapter_descriptor(adapter_id)
        if descriptor is None:
            return f"Task retry requires registered execution adapter: {adapter_id}"
        if descriptor.replay_policy in {ToolReplayPolicy.SAFE, ToolReplayPolicy.IDEMPOTENT_WITH_KEY}:
            return None
        if descriptor.replay_policy == ToolReplayPolicy.NOT_REPLAYABLE:
            return f"Task retry is not allowed for {descriptor.id}: replay_policy={descriptor.replay_policy.value}"
        if descriptor.replay_policy == ToolReplayPolicy.REQUIRES_FRESH_APPROVAL:
            missing = self._missing_registered_adapter_approvals(task, descriptor)
            if missing:
                return f"Task retry requires fresh approval for {descriptor.id}: {', '.join(missing)}"
            if not descriptor.required_approvals:
                return (
                    f"Task retry requires fresh approval for {descriptor.id}: "
                    f"replay_policy={descriptor.replay_policy.value}"
                )
            return None
        return f"Task retry is not allowed for {descriptor.id}: replay_policy={descriptor.replay_policy.value}"

    def _task_replay_policy_projection(self, task: TaskRecord) -> dict[str, Any]:
        adapter_id = task.metadata.get("execution_adapter")
        adapter_id_value = adapter_id.strip() if isinstance(adapter_id, str) and adapter_id.strip() else None
        if adapter_id_value is None:
            return {
                "adapter_id": None,
                "descriptor_registered": False,
                "replay_policy": None,
                "retry_gate": "legacy_unregistered_task",
                "retry_allowed": True,
                "fresh_approval_required": False,
                "fresh_approval_revalidated": False,
            }
        from harness.execution import get_execution_adapter_descriptor

        descriptor = get_execution_adapter_descriptor(adapter_id_value)
        if descriptor is None:
            return {
                "adapter_id": adapter_id_value,
                "descriptor_registered": False,
                "replay_policy": None,
                "retry_gate": "unknown_execution_adapter",
                "retry_allowed": False,
                "fresh_approval_required": False,
                "fresh_approval_revalidated": False,
            }
        replay_policy = descriptor.replay_policy
        fresh_approval_required = replay_policy == ToolReplayPolicy.REQUIRES_FRESH_APPROVAL
        missing_approvals = self._missing_registered_adapter_approvals(task, descriptor)
        if replay_policy in {ToolReplayPolicy.SAFE, ToolReplayPolicy.IDEMPOTENT_WITH_KEY}:
            retry_gate = "safe_replay_policy"
            retry_allowed = True
        elif fresh_approval_required and not missing_approvals:
            retry_gate = "fresh_approval_revalidated"
            retry_allowed = True
        elif replay_policy == ToolReplayPolicy.NOT_REPLAYABLE:
            retry_gate = "not_replayable"
            retry_allowed = False
        else:
            retry_gate = "fresh_approval_missing" if fresh_approval_required else f"replay_{replay_policy.value}"
            retry_allowed = False
        return {
            "adapter_id": descriptor.id,
            "descriptor_registered": True,
            "replay_policy": replay_policy.value,
            "retry_gate": retry_gate,
            "retry_allowed": retry_allowed,
            "fresh_approval_required": fresh_approval_required,
            "fresh_approval_revalidated": fresh_approval_required and not missing_approvals,
            "missing_approvals": sorted(set(missing_approvals)),
        }

    def _task_retry_replay_receipt(
        self,
        conn: sqlite3.Connection,
        *,
        task: TaskRecord,
        next_status: TaskStatus,
        created_at: str,
    ) -> dict[str, Any]:
        policy = self._task_replay_policy_projection(task)
        attempt_count = self._task_attempt_count(conn, task.id)
        return {
            "schema_version": TASK_REPLAY_RECEIPT_SCHEMA_VERSION,
            "receipt_kind": "retry_authorization",
            "task_id": task.id,
            "task_idempotency_key": task.idempotency_key,
            "previous_status": task.status.value,
            "next_status": next_status.value,
            "prior_attempt_count": attempt_count,
            "active_lease_duplicate_guard": "active_lease_exclusion_before_attempt_insert",
            "created_at": created_at,
            **policy,
        }

    def _attempt_replay_receipt(
        self,
        *,
        task: TaskRecord,
        attempt_number: int,
        attempt_idempotency_key: str,
        created_at: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": TASK_REPLAY_RECEIPT_SCHEMA_VERSION,
            "receipt_kind": "attempt_replay_guard",
            "task_id": task.id,
            "task_idempotency_key": task.idempotency_key,
            "attempt_number": attempt_number,
            "attempt_idempotency_key": attempt_idempotency_key,
            "prior_attempt_count": max(0, attempt_number - 1),
            "active_lease_duplicate_guard": "active_lease_exclusion_before_attempt_insert",
            "created_at": created_at,
            **self._task_replay_policy_projection(task),
        }

    def _missing_registered_adapter_approvals(self, task: TaskRecord, descriptor: Any) -> list[str]:
        required = [str(item) for item in descriptor.required_approvals if str(item).strip()]
        if not required:
            return []
        missing: list[str] = []
        task_type = task.metadata.get("task_type")
        for approval in required:
            if approval == "hosted_provider_codex":
                if not isinstance(task_type, str) or not task_type.strip():
                    missing.append(approval)
                    continue
                from harness.approvals import ApprovalStore

                found = ApprovalStore(self.project_root).find_valid(
                    "codex_cli",
                    "hosted_provider",
                    task_type,
                    adapter_id=descriptor.id,
                    workbench_id=task.workbench_id,
                    objective_id=task.objective_id,
                )
                if found is None:
                    missing.append(approval)
                continue
            missing.append(approval)
        return missing

    def _daemon_registered_adapter_pause(
        self,
        task: TaskRecord,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        adapter_id = task.metadata.get("execution_adapter")
        if not isinstance(adapter_id, str) or not adapter_id.strip():
            return None
        from harness.execution import get_execution_adapter_descriptor, runtime_control_matches_descriptor

        descriptor = get_execution_adapter_descriptor(adapter_id)
        if descriptor is None:
            return {
                "decision": "policy_forbidden",
                "reason": f"Task references unknown execution adapter: {adapter_id}",
                "adapter_id": adapter_id,
                "approval_source": "execution_adapter_descriptor",
                "forbidden_policy_keys": ["unknown_execution_adapter"],
            }
        task_type = task.metadata.get("task_type")
        task_type_value = task_type if isinstance(task_type, str) else None
        controls = (
            self._active_execution_controls_in_conn(conn)
            if conn is not None
            else self.active_execution_controls()
        )
        for control in controls:
            if runtime_control_matches_descriptor(control, descriptor, task_type_value):
                return {
                    "decision": "control_disabled",
                    "reason": str(
                        sanitize_for_logging(
                            f"Execution control disabled {control.target_kind.value}:{control.target_id}. {control.reason}"
                        )
                    ),
                    "adapter_id": descriptor.id,
                    "task_type": task_type_value,
                    "control_id": control.id,
                    "target_kind": control.target_kind.value,
                    "target_id": control.target_id,
                    "control_reason": control.reason,
                    "control_source": "execution_control",
                }
        missing = self._missing_registered_adapter_approvals(task, descriptor)
        if not missing:
            breaker = self.adapter_breaker_state(descriptor.id)
            if breaker.status == BreakerStatus.OPEN:
                return {
                    "decision": "breaker_open",
                    "reason": (
                        f"Adapter breaker is open for {descriptor.id}: "
                        f"{breaker.failure_count}/{breaker.threshold} failures in {breaker.window_seconds} seconds."
                    ),
                    "adapter_id": descriptor.id,
                    "task_type": task_type_value,
                    "failure_count": breaker.failure_count,
                    "threshold": breaker.threshold,
                    "window_seconds": breaker.window_seconds,
                    "opened_at": breaker.opened_at.isoformat() if breaker.opened_at is not None else None,
                    "control_source": "adapter_breaker",
                }
            return None
        descriptor_required = sorted(
            {str(item) for item in descriptor.required_approvals if str(item).strip()}
        )
        return {
            "decision": "waiting_approval",
            "reason": "Registered adapter requires approval before daemon lease acquisition.",
            "required_approvals": sorted(set(task.required_approvals + missing)),
            "missing_approvals": sorted(set(missing)),
            "descriptor_required_approvals": descriptor_required,
            "adapter_id": descriptor.id,
            "task_type": task_type_value,
            "approval_source": "execution_adapter_descriptor",
        }

    def select_next_task(self) -> TaskRecord | None:
        selection = self.select_next_task_for_lease()
        return selection["task"] if selection is not None else None

    def select_next_task_for_lease(
        self,
        owner: str = DEFAULT_TASK_LEASE_OWNER,
        lease_duration_minutes: int = DEFAULT_TASK_LEASE_MINUTES,
        objective_id: str | None = None,
    ) -> dict[str, TaskAttempt | TaskLease | TaskRecord] | None:
        timestamp = now_iso()
        expires_at = (parse_dt(timestamp) + timedelta(minutes=lease_duration_minutes)).isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if objective_id is not None:
                self._require_objective(conn, objective_id)
            rows = conn.execute(
                (
                    """
                    SELECT * FROM tasks
                    WHERE status IN (?, ?) AND objective_id = ?
                    ORDER BY priority DESC, created_at ASC
                    """
                    if objective_id is not None
                    else """
                    SELECT * FROM tasks
                    WHERE status IN (?, ?)
                    ORDER BY priority DESC, created_at ASC
                    """
                ),
                (TaskStatus.READY.value, TaskStatus.BLOCKED.value, objective_id)
                if objective_id is not None
                else (TaskStatus.READY.value, TaskStatus.BLOCKED.value),
            ).fetchall()
            for row in rows:
                task = self._row_to_task(row)
                if self._task_has_active_lease(conn, task.id):
                    continue
                if task.required_approvals:
                    continue
                if not self._task_dependencies_completed(task):
                    continue
                return self._lease_task_in_conn(
                    conn,
                    task=task,
                    owner=owner,
                    timestamp=timestamp,
                    expires_at=expires_at,
                )
        return None

    def select_next_guarded_task_for_lease(
        self,
        owner: str = DEFAULT_TASK_LEASE_OWNER,
        lease_duration_minutes: int = DEFAULT_TASK_LEASE_MINUTES,
        objective_id: str | None = None,
    ) -> tuple[dict[str, TaskAttempt | TaskLease | TaskRecord] | None, list[dict[str, Any]]]:
        timestamp = now_iso()
        expires_at = (parse_dt(timestamp) + timedelta(minutes=lease_duration_minutes)).isoformat()
        pause_reasons: list[dict[str, Any]] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if objective_id is not None:
                self._require_objective(conn, objective_id)
            rows = conn.execute(
                (
                    """
                    SELECT * FROM tasks
                    WHERE status IN (?, ?, ?) AND objective_id = ?
                    ORDER BY priority DESC, created_at ASC
                    """
                    if objective_id is not None
                    else """
                    SELECT * FROM tasks
                    WHERE status IN (?, ?, ?)
                    ORDER BY priority DESC, created_at ASC
                    """
                ),
                (TaskStatus.READY.value, TaskStatus.BLOCKED.value, TaskStatus.WAITING_APPROVAL.value, objective_id)
                if objective_id is not None
                else (TaskStatus.READY.value, TaskStatus.BLOCKED.value, TaskStatus.WAITING_APPROVAL.value),
            ).fetchall()
            for row in rows:
                task = self._row_to_task(row)
                eligibility = self.daemon_task_eligibility(task, conn=conn)
                if eligibility["decision"] == "eligible":
                    return (
                        self._lease_task_in_conn(
                            conn,
                            task=task,
                            owner=owner,
                            timestamp=timestamp,
                            expires_at=expires_at,
                        ),
                        pause_reasons,
                    )
                if eligibility["decision"] in DAEMON_TASK_PAUSE_DECISIONS:
                    pause_reasons.append(eligibility)
        return None, pause_reasons

    def select_task_for_lease(
        self,
        task_id: str,
        owner: str = DEFAULT_TASK_LEASE_OWNER,
        lease_duration_minutes: int = DEFAULT_TASK_LEASE_MINUTES,
        objective_id: str | None = None,
    ) -> dict[str, TaskAttempt | TaskLease | TaskRecord] | None:
        timestamp = now_iso()
        expires_at = (parse_dt(timestamp) + timedelta(minutes=lease_duration_minutes)).isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if objective_id is not None:
                self._require_objective(conn, objective_id)
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Task not found: {task_id}")
            task = self._row_to_task(row)
            if objective_id is not None and task.objective_id != objective_id:
                return None
            if task.status not in {TaskStatus.READY, TaskStatus.BLOCKED}:
                return None
            if self._task_has_active_lease(conn, task.id):
                return None
            if task.required_approvals:
                return None
            if not self._dependency_ids_completed(conn, task.depends_on):
                return None
            return self._lease_task_in_conn(
                conn,
                task=task,
                owner=owner,
                timestamp=timestamp,
                expires_at=expires_at,
            )

    def select_guarded_task_for_lease(
        self,
        task_id: str,
        owner: str = DEFAULT_TASK_LEASE_OWNER,
        lease_duration_minutes: int = DEFAULT_TASK_LEASE_MINUTES,
        objective_id: str | None = None,
    ) -> tuple[dict[str, TaskAttempt | TaskLease | TaskRecord] | None, list[dict[str, Any]]]:
        timestamp = now_iso()
        expires_at = (parse_dt(timestamp) + timedelta(minutes=lease_duration_minutes)).isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if objective_id is not None:
                self._require_objective(conn, objective_id)
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"Task not found: {task_id}")
            task = self._row_to_task(row)
            if objective_id is not None and task.objective_id != objective_id:
                return None, []
            eligibility = self.daemon_task_eligibility(task, conn=conn)
            if eligibility["decision"] == "eligible":
                return (
                    self._lease_task_in_conn(
                        conn,
                        task=task,
                        owner=owner,
                        timestamp=timestamp,
                        expires_at=expires_at,
                    ),
                    [],
                )
            if eligibility["decision"] in DAEMON_TASK_PAUSE_DECISIONS:
                return None, [eligibility]
        return None, []

    def select_next_daemon_task_for_lease(
        self,
        owner: str,
        lease_duration_minutes: int = DEFAULT_TASK_LEASE_MINUTES,
        objective_id: str | None = None,
    ) -> tuple[dict[str, TaskAttempt | TaskLease | TaskRecord] | None, list[dict[str, Any]]]:
        return self.select_next_guarded_task_for_lease(
            owner=owner,
            lease_duration_minutes=lease_duration_minutes,
            objective_id=objective_id,
        )

    def daemon_task_eligibility(
        self,
        task: TaskRecord,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        policy = resolve_task_effective_policy(task)
        policy_hash = effective_policy_sha256(policy)
        base = {
            "task_id": task.id,
            "status": task.status.value,
            "required_approvals": sorted(set(task.required_approvals)),
            "effective_policy_sha256": policy_hash,
        }
        if task.status in {
            TaskStatus.LEASED,
            TaskStatus.RUNNING,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.SKIPPED,
        }:
            return {
                **base,
                "decision": "skipped_status",
                "reason": f"Task status is not daemon-selectable: {task.status.value}",
            }
        if conn is not None and self._task_has_active_lease(conn, task.id):
            return {
                **base,
                "decision": "active_lease",
                "reason": "Task already has an active lease.",
            }
        if task.required_approvals:
            return {
                **base,
                "decision": "waiting_approval",
                "reason": "Task has unresolved required approvals.",
            }
        adapter_pause = self._daemon_registered_adapter_pause(task, conn=conn)
        if adapter_pause is not None:
            return {
                **base,
                **adapter_pause,
            }
        forbidden_keys = self._daemon_policy_forbidden_keys(task, policy)
        if forbidden_keys:
            return {
                **base,
                "decision": "policy_forbidden",
                "reason": "Task metadata requests daemon-forbidden capability.",
                "forbidden_policy_keys": forbidden_keys,
            }
        if not self._task_dependencies_completed(task):
            return {
                **base,
                "decision": "blocked_dependency",
                "reason": "Task dependencies are not satisfied.",
                "blocked_dependency_ids": self._task_blocked_dependency_ids(task),
            }
        return {
            **base,
            "decision": "eligible",
            "reason": "Task is ready for daemon lease acquisition.",
        }

    def daemon_paused_tasks(self) -> list[dict[str, Any]]:
        rows: list[sqlite3.Row]
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status IN (?, ?, ?)
                ORDER BY priority DESC, created_at ASC
                """,
                (TaskStatus.READY.value, TaskStatus.BLOCKED.value, TaskStatus.WAITING_APPROVAL.value),
            ).fetchall()
            paused = [
                self.daemon_task_eligibility(self._row_to_task(row), conn=conn)
                for row in rows
            ]
        return [
            item
            for item in paused
            if item["decision"] in DAEMON_TASK_PAUSE_DECISIONS
        ]

    def _daemon_policy_forbidden_keys(
        self,
        task: TaskRecord,
        policy: Any,
    ) -> list[str]:
        requested = [
            key
            for key in sorted(DAEMON_POLICY_FORBIDDEN_METADATA_KEYS)
            if bool(task.metadata.get(key))
        ]
        metadata_to_policy_key = {
            "daemon_policy_forbidden": "task_queue_execution",
            "requires_active_repo_write": "active_repo_write",
            "requires_external_network": "external_network",
            "requires_docker": "docker_execution",
            "requires_paid_provider": "paid_provider",
            "requires_hosted_boundary": "hosted_boundary",
        }
        forbidden: list[str] = []
        for metadata_key in requested:
            policy_key = metadata_to_policy_key[metadata_key]
            if policy.levels.get(policy_key) in {PolicyLevel.FORBIDDEN, PolicyLevel.APPROVAL_REQUIRED}:
                forbidden.append(policy_key)
        return sorted(set(forbidden))

    def _task_blocked_dependency_ids(self, task: TaskRecord) -> list[str]:
        blocked: list[str] = []
        for dependency_id in task.depends_on:
            try:
                dependency = self.get_task(dependency_id)
            except KeyError:
                blocked.append(dependency_id)
                continue
            if dependency.status != TaskStatus.SUCCEEDED:
                blocked.append(dependency_id)
        return sorted(set(blocked))

    def _lease_task_in_conn(
        self,
        conn: sqlite3.Connection,
        *,
        task: TaskRecord,
        owner: str,
        timestamp: str,
        expires_at: str,
    ) -> dict[str, TaskAttempt | TaskLease | TaskRecord]:
        current_status = task.status
        if current_status == TaskStatus.BLOCKED:
            validate_task_transition(TaskStatus.BLOCKED, TaskStatus.READY)
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (TaskStatus.READY.value, timestamp, task.id),
            )
            self._record_task_transition(
                conn,
                task_id=task.id,
                from_status=TaskStatus.BLOCKED,
                to_status=TaskStatus.READY,
                reason="dependencies_satisfied",
                actor="system",
                metadata={},
                created_at=timestamp,
            )
            current_status = TaskStatus.READY
        validate_task_transition(current_status, TaskStatus.LEASED)
        attempt_number = self._next_attempt_number(conn, task.id)
        attempt_id = f"task_attempt_{uuid.uuid4().hex[:12]}"
        lease_id = f"task_lease_{uuid.uuid4().hex[:12]}"
        attempt_idempotency_key = f"{task.idempotency_key}:attempt:{attempt_number}"
        attempt_metadata = {
            "task_idempotency_key": task.idempotency_key,
            "attempt_idempotency_key": attempt_idempotency_key,
            "replay_receipt": self._attempt_replay_receipt(
                task=task,
                attempt_number=attempt_number,
                attempt_idempotency_key=attempt_idempotency_key,
                created_at=timestamp,
            ),
        }
        conn.execute(
            """
            INSERT INTO task_attempts (
              id, task_id, attempt_number, status, lease_id, run_id,
              created_at, started_at, finished_at, failure_code, failure_message,
              metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                task.id,
                attempt_number,
                TaskStatus.LEASED.value,
                lease_id,
                None,
                timestamp,
                None,
                None,
                None,
                None,
                json.dumps(sanitize_for_logging(attempt_metadata), sort_keys=True, default=str),
            ),
        )
        conn.execute(
            """
            INSERT INTO task_leases (
              id, task_id, attempt_id, owner, status, acquired_at, expires_at,
              heartbeat_at, released_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lease_id,
                task.id,
                attempt_id,
                owner,
                TaskLeaseStatus.ACTIVE.value,
                timestamp,
                expires_at,
                None,
                None,
                "{}",
            ),
        )
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (TaskStatus.LEASED.value, timestamp, task.id),
        )
        self._record_task_transition(
            conn,
            task_id=task.id,
            from_status=current_status,
            to_status=TaskStatus.LEASED,
            reason="task_leased",
            actor=owner,
            metadata={"attempt_id": attempt_id, "lease_id": lease_id},
            created_at=timestamp,
        )
        return {
            "task": self._row_to_task(
                conn.execute("SELECT * FROM tasks WHERE id = ?", (task.id,)).fetchone()
            ),
            "attempt": self._row_to_task_attempt(
                conn.execute("SELECT * FROM task_attempts WHERE id = ?", (attempt_id,)).fetchone()
            ),
            "lease": self._row_to_task_lease(
                conn.execute("SELECT * FROM task_leases WHERE id = ?", (lease_id,)).fetchone()
            ),
        }

    def daemon_run_once(self, owner: str, pid: int | None = None, objective_id: str | None = None) -> DaemonTickResult:
        daemon = self.ensure_daemon(owner=owner, pid=pid)
        tick_id = f"daemon_tick_{uuid.uuid4().hex[:12]}"
        renewed_leases = self.renew_daemon_leases(owner=owner, objective_id=objective_id)
        if renewed_leases:
            self.record_daemon_event(
                daemon.id,
                event_type="tick",
                message="Daemon scheduler tick renewed active lease.",
                metadata={
                    "tick_id": tick_id,
                    "decision": "renewed_lease",
                    "lease_ids": [lease.id for lease in renewed_leases],
                },
            )
            return DaemonTickResult(
                daemon_id=daemon.id,
                owner=daemon.owner,
                project_root=self.project_root,
                tick_id=tick_id,
                decision="renewed_lease",
                selected_task=None,
                attempt=None,
                lease=renewed_leases[0],
                pause_reasons=[],
            )
        selection, pause_reasons = self.select_next_daemon_task_for_lease(owner=owner, objective_id=objective_id)
        decision = "leased_task" if selection is not None else "paused" if pause_reasons else "no_eligible_task"
        metadata = {
            "tick_id": tick_id,
            "decision": decision,
            "task_id": selection["task"].id if selection is not None else None,
            "attempt_id": selection["attempt"].id if selection is not None else None,
            "lease_id": selection["lease"].id if selection is not None else None,
            "pause_reasons": pause_reasons,
        }
        self.record_daemon_event(
            daemon.id,
            event_type="tick",
            message="Daemon scheduler tick completed.",
            metadata=metadata,
        )
        return DaemonTickResult(
            daemon_id=daemon.id,
            owner=daemon.owner,
            project_root=self.project_root,
            tick_id=tick_id,
            decision=decision,
            selected_task=selection["task"] if selection is not None else None,
            attempt=selection["attempt"] if selection is not None else None,
            lease=selection["lease"] if selection is not None else None,
            pause_reasons=pause_reasons,
        )

    def execute_dry_run_lease(self, lease_id: str, owner: str = DEFAULT_TASK_LEASE_OWNER) -> DaemonDryRunResult:
        lease = self.get_task_lease(lease_id)
        self._require_active_lease_authority(lease, owner, action="Dry-run execution")
        if lease.attempt_id is None:
            raise ValueError(f"Dry-run execution requires linked task attempt: {lease.id}")
        attempt = self.get_task_attempt(lease.attempt_id)
        if attempt.run_id is not None:
            raise ValueError(
                f"Task attempt already has run_id: {attempt.id}; inspect the lease or run daemon recover"
            )
        task = self.get_task(lease.task_id)
        if task.status != TaskStatus.LEASED:
            raise ValueError(f"Dry-run execution requires leased task status: {task.status.value}")
        if task.required_approvals:
            raise ValueError("Dry-run execution rejected: task has unresolved required approvals")
        self._validate_dry_run_task_metadata(task)
        task_policy = resolve_task_effective_policy(task)
        policy_hash = effective_policy_sha256(task_policy)
        goal = task.title if not task.description else f"{task.title}\n\n{task.description}"
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        started_at = now_iso()
        with self.connect() as conn:
            self._insert_run_in_conn(
                conn,
                run_id=run_id,
                timestamp=started_at,
                goal=goal,
                task_type=DRY_RUN_TASK_TYPE,
                status="running",
                backend=None,
                approval_id=None,
                task_id=task.id,
                objective_id=task.objective_id,
            )
            validate_task_transition(TaskStatus.LEASED, TaskStatus.RUNNING)
            result = conn.execute(
                """
                UPDATE task_attempts
                SET status = ?, run_id = ?, started_at = ?
                WHERE id = ?
                  AND run_id IS NULL
                  AND lease_id = ?
                  AND EXISTS (
                    SELECT 1 FROM task_leases
                    WHERE id = ? AND status = ? AND owner = ?
                  )
                """,
                (
                    TaskStatus.RUNNING.value,
                    run_id,
                    started_at,
                    attempt.id,
                    lease.id,
                    lease.id,
                    TaskLeaseStatus.ACTIVE.value,
                    owner,
                ),
            )
            if result.rowcount == 0:
                raise ValueError(f"Dry-run execution requires active lease owned by {owner}: {lease.id}")
            task_result = conn.execute(
                "UPDATE tasks SET status = ?, run_id = ?, updated_at = ? WHERE id = ? AND status = ?",
                (TaskStatus.RUNNING.value, run_id, started_at, task.id, TaskStatus.LEASED.value),
            )
            if task_result.rowcount == 0:
                raise ValueError(f"Dry-run execution requires leased task status: {task.status.value}")
            self._record_task_transition(
                conn,
                task_id=task.id,
                from_status=TaskStatus.LEASED,
                to_status=TaskStatus.RUNNING,
                reason="dry_run_execution_started",
                actor=owner,
                metadata={"lease_id": lease.id, "attempt_id": attempt.id, "run_id": run_id},
                created_at=started_at,
            )
        paths = self.initialize_run_artifacts(run_id)
        append_jsonl(
            paths["transcript"],
            sanitize_for_logging(
                {
                    "event": "dry_run_no_tool_execution",
                    "task_id": task.id,
                    "attempt_id": attempt.id,
                    "lease_id": lease.id,
                    "run_id": run_id,
                    "policy_sha256": policy_hash,
                }
            ),
        )
        paths["final_report"].write_text(
            "\n".join(
                [
                    f"# Dry-run execution contract {run_id}",
                    "",
                    "This run was created by the daemon dry-run execution adapter.",
                    "No backend, tool, Docker, shell, network, hosted provider, or paid provider was invoked.",
                    "",
                    f"- Task id: {task.id}",
                    f"- Objective id: {task.objective_id or 'none'}",
                    f"- Workbench id: {task.workbench_id or 'none'}",
                    f"- Agent id: {task.agent_id or 'none'}",
                    f"- Attempt id: {attempt.id}",
                    f"- Lease id: {lease.id}",
                    f"- Run id: {run_id}",
                    f"- Policy sha256: {policy_hash}",
                    f"- Workflow stage: {task.metadata.get('workflow_stage') or 'none'}",
                    f"- Review role: {task.metadata.get('review_role') or 'none'}",
                    "- Artifact evidence: events, transcript, final_report, manifest",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self.append_event(
            run_id,
            "info",
            "dry_run_no_tool_execution",
            "Dry-run execution contract completed without tool execution.",
            {
                "task_id": task.id,
                "attempt_id": attempt.id,
                "lease_id": lease.id,
                "policy_sha256": policy_hash,
            },
        )
        for kind in ("events", "transcript", "final_report", "manifest"):
            self.register_artifact(
                run_id,
                kind=kind,
                path=paths[kind],
                producer="daemon_execute_dry_run",
                redaction_state="redacted",
                metadata={"dry_run": True},
            )
        finished_at = now_iso()
        with self.connect() as conn:
            validate_task_transition(TaskStatus.RUNNING, TaskStatus.SUCCEEDED)
            attempt_result = conn.execute(
                """
                UPDATE task_attempts
                SET status = ?, finished_at = ?
                WHERE id = ?
                  AND status = ?
                  AND run_id = ?
                  AND lease_id = ?
                  AND EXISTS (
                    SELECT 1 FROM task_leases
                    WHERE id = ? AND status = ? AND owner = ?
                  )
                """,
                (
                    TaskStatus.SUCCEEDED.value,
                    finished_at,
                    attempt.id,
                    TaskStatus.RUNNING.value,
                    run_id,
                    lease.id,
                    lease.id,
                    TaskLeaseStatus.ACTIVE.value,
                    owner,
                ),
            )
            if attempt_result.rowcount == 0:
                raise ValueError(f"Dry-run finalization requires active lease owned by {owner}: {lease.id}")
            lease_result = conn.execute(
                """
                UPDATE task_leases
                SET status = ?, released_at = ?, metadata_json = ?
                WHERE id = ? AND status = ? AND owner = ?
                """,
                (
                    TaskLeaseStatus.RELEASED.value,
                    finished_at,
                    json.dumps(
                        sanitize_for_logging({"run_id": run_id, "decision": "dry_run_no_tool_execution"}),
                        sort_keys=True,
                    ),
                    lease.id,
                    TaskLeaseStatus.ACTIVE.value,
                    owner,
                ),
            )
            if lease_result.rowcount == 0:
                raise ValueError(f"Dry-run finalization requires active lease owned by {owner}: {lease.id}")
            task_result = conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND status = ? AND run_id = ?",
                (TaskStatus.SUCCEEDED.value, finished_at, task.id, TaskStatus.RUNNING.value, run_id),
            )
            if task_result.rowcount == 0:
                raise ValueError(f"Dry-run finalization requires running task linked to run: {task.id}")
            conn.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                ("completed", finished_at, run_id),
            )
            self._record_task_transition(
                conn,
                task_id=task.id,
                from_status=TaskStatus.RUNNING,
                to_status=TaskStatus.SUCCEEDED,
                reason="dry_run_execution_succeeded",
                actor=owner,
                metadata={"lease_id": lease.id, "attempt_id": attempt.id, "run_id": run_id},
                created_at=finished_at,
            )
        daemon = self.ensure_daemon(owner=lease.owner)
        self.record_daemon_event(
            daemon.id,
            event_type="execute_dry_run",
            message="Dry-run execution contract linked lease to run evidence.",
            metadata={
                "lease_id": lease.id,
                "attempt_id": attempt.id,
                "task_id": task.id,
                "run_id": run_id,
                "decision": "dry_run_no_tool_execution",
                "policy_sha256": policy_hash,
            },
        )
        self.write_run_manifest(run_id)
        return DaemonDryRunResult(
            decision="dry_run_no_tool_execution",
            project_root=self.project_root,
            task=self.get_task(task.id),
            attempt=self.get_task_attempt(attempt.id),
            lease=self.get_task_lease(lease.id),
            run=self.get_run(run_id),
            manifest=self.build_run_manifest(run_id),
            policy_sha256=policy_hash,
        )

    def _validate_dry_run_task_metadata(self, task: TaskRecord) -> None:
        if task.metadata.get("execution_adapter") != DRY_RUN_EXECUTION_ADAPTER:
            raise ValueError("Dry-run execution requires execution_adapter=dry_run")
        if task.metadata.get("task_type") != DRY_RUN_TASK_TYPE:
            raise ValueError("Dry-run execution requires task_type=phase_1a_test")
        forbidden = sorted(key for key in DRY_RUN_FORBIDDEN_METADATA_KEYS if bool(task.metadata.get(key)))
        if forbidden:
            raise ValueError(f"Dry-run execution rejected by task metadata: {', '.join(forbidden)}")

    def validate_read_only_lease_for_execution(
        self,
        lease_id: str,
        *,
        owner: str | None = None,
    ) -> tuple[TaskLease, TaskAttempt, TaskRecord]:
        lease = self.get_task_lease(lease_id)
        if lease.status != TaskLeaseStatus.ACTIVE:
            raise ValueError(f"Read-only execution requires active lease: {lease.status.value}")
        if owner is not None:
            self._require_active_lease_authority(lease, owner, action="Read-only execution")
        if lease.attempt_id is None:
            raise ValueError(f"Read-only execution requires linked task attempt: {lease.id}")
        attempt = self.get_task_attempt(lease.attempt_id)
        if attempt.run_id is not None:
            raise ValueError(
                f"Task attempt already has run_id: {attempt.id}; inspect the lease or run daemon recover"
            )
        task = self.get_task(lease.task_id)
        if task.status != TaskStatus.LEASED:
            raise ValueError(f"Read-only execution requires leased task status: {task.status.value}")
        if task.required_approvals:
            raise ValueError("Read-only execution rejected: task has unresolved required approvals")
        self._validate_read_only_task_metadata(task)
        return lease, attempt, task

    def validate_execution_lease_for_run(
        self,
        lease_id: str,
        *,
        owner: str | None = None,
    ) -> tuple[TaskLease, TaskAttempt, TaskRecord]:
        lease = self.get_task_lease(lease_id)
        if lease.status != TaskLeaseStatus.ACTIVE:
            raise ValueError(f"Execution requires active lease: {lease.status.value}")
        if owner is not None:
            self._require_active_lease_authority(lease, owner, action="Execution")
        if lease.attempt_id is None:
            raise ValueError(f"Execution requires linked task attempt: {lease.id}")
        attempt = self.get_task_attempt(lease.attempt_id)
        if attempt.run_id is not None:
            raise ValueError(
                f"Task attempt already has run_id: {attempt.id}; inspect the lease or run daemon recover"
            )
        task = self.get_task(lease.task_id)
        if task.status != TaskStatus.LEASED:
            raise ValueError(f"Execution requires leased task status: {task.status.value}")
        if task.required_approvals:
            raise ValueError("Execution rejected: task has unresolved required approvals")
        return lease, attempt, task

    def finalize_rejected_task_lease(
        self,
        lease_id: str,
        *,
        reason_code: str,
        rejection_reasons: list[str],
        decision: str,
        adapter_id: str | None = None,
        security_decision_id: str | None = None,
        policy_sha256: str | None = None,
        owner: str | None = None,
    ) -> tuple[TaskLease, TaskAttempt | None, TaskRecord | None]:
        """Close a no-run adapter rejection without relying on stale lease recovery."""

        terminal_skip_codes = {"duplicate_run", "lease_owner_mismatch"}
        approval_required_codes = {
            "missing_required_approval",
            "unresolved_task_approvals",
            "missing_hosted_approval",
        }
        sanitized_reasons = [str(sanitize_for_logging(str(reason))) for reason in rejection_reasons]

        with self.connect() as conn:
            lease_row = conn.execute("SELECT * FROM task_leases WHERE id = ?", (lease_id,)).fetchone()
            if lease_row is None:
                raise KeyError(f"Task lease not found: {lease_id}")
            lease = self._row_to_task_lease(lease_row)
            attempt: TaskAttempt | None = None
            if lease.attempt_id is not None:
                attempt_row = conn.execute("SELECT * FROM task_attempts WHERE id = ?", (lease.attempt_id,)).fetchone()
                attempt = self._row_to_task_attempt(attempt_row) if attempt_row is not None else None
            task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (lease.task_id,)).fetchone()
            task = self._row_to_task(task_row) if task_row is not None else None

            if (
                reason_code in terminal_skip_codes
                or lease.status != TaskLeaseStatus.ACTIVE
                or (attempt is not None and attempt.run_id is not None)
                or (task is not None and task.run_id is not None)
            ):
                return self._refreshed_task_lease_context(lease)

            timestamp = now_iso()
            final_status = (
                TaskStatus.WAITING_APPROVAL
                if reason_code in approval_required_codes or "approval" in reason_code
                else TaskStatus.FAILED
            )
            transition_reason = (
                "adapter_rejection_waiting_approval"
                if final_status == TaskStatus.WAITING_APPROVAL
                else "adapter_rejection_failed"
            )
            failure_message = "; ".join(sanitized_reasons) if sanitized_reasons else reason_code
            finalizer_metadata = {
                "decision": decision,
                "adapter_id": adapter_id,
                "reason_code": reason_code,
                "rejection_reasons": sanitized_reasons,
                "security_decision_id": security_decision_id,
                "policy_sha256": policy_sha256,
                "terminal_task_status": final_status.value,
                "finalized_by": "adapter_rejection",
            }
            conn.execute(
                """
                UPDATE task_leases
                SET status = ?, released_at = ?, metadata_json = ?
                WHERE id = ? AND status = ?
                """,
                (
                    TaskLeaseStatus.RELEASED.value,
                    timestamp,
                    json.dumps(sanitize_for_logging({**lease.metadata, **finalizer_metadata}), sort_keys=True),
                    lease.id,
                    TaskLeaseStatus.ACTIVE.value,
                ),
            )
            if attempt is not None:
                conn.execute(
                    """
                    UPDATE task_attempts
                    SET status = ?,
                        finished_at = COALESCE(finished_at, ?),
                        failure_code = ?,
                        failure_message = ?
                    WHERE id = ? AND run_id IS NULL
                    """,
                    (final_status.value, timestamp, reason_code, failure_message, attempt.id),
                )
            if task is not None and task.status != final_status:
                validate_task_transition(task.status, final_status)
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (final_status.value, timestamp, task.id),
                )
                self._record_task_transition(
                    conn,
                    task_id=task.id,
                    from_status=task.status,
                    to_status=final_status,
                    reason=transition_reason,
                    actor=owner or lease.owner,
                    metadata={
                        "lease_id": lease.id,
                        "attempt_id": lease.attempt_id,
                        "decision": decision,
                        "adapter_id": adapter_id,
                        "reason_code": reason_code,
                        "rejection_reasons": sanitized_reasons,
                        "security_decision_id": security_decision_id,
                        "policy_sha256": policy_sha256,
                    },
                    created_at=timestamp,
                )
        return self._refreshed_task_lease_context(lease_id)

    def start_attempt_run(
        self,
        lease_id: str,
        *,
        task_type: str,
        backend: BackendConfig | None,
        approval_id: str | None,
        owner: str = DEFAULT_TASK_LEASE_OWNER,
    ) -> RunRecord:
        lease, attempt, task = self.validate_execution_lease_for_run(lease_id, owner=owner)
        goal = task.title if not task.description else f"{task.title}\n\n{task.description}"
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        started_at = now_iso()
        with self.connect() as conn:
            self._insert_run_in_conn(
                conn,
                run_id=run_id,
                timestamp=started_at,
                goal=goal,
                task_type=task_type,
                status="running",
                backend=backend,
                approval_id=approval_id,
                task_id=task.id,
                objective_id=task.objective_id,
            )
            validate_task_transition(TaskStatus.LEASED, TaskStatus.RUNNING)
            result = conn.execute(
                """
                UPDATE task_attempts
                SET status = ?, run_id = ?, started_at = ?
                WHERE id = ?
                  AND run_id IS NULL
                  AND lease_id = ?
                  AND EXISTS (
                    SELECT 1 FROM task_leases
                    WHERE id = ? AND status = ? AND owner = ?
                  )
                """,
                (
                    TaskStatus.RUNNING.value,
                    run_id,
                    started_at,
                    attempt.id,
                    lease.id,
                    lease.id,
                    TaskLeaseStatus.ACTIVE.value,
                    owner,
                ),
            )
            if result.rowcount == 0:
                raise ValueError(f"Execution requires active lease owned by {owner}: {lease.id}")
            task_result = conn.execute(
                "UPDATE tasks SET status = ?, run_id = ?, updated_at = ? WHERE id = ? AND status = ?",
                (TaskStatus.RUNNING.value, run_id, started_at, task.id, TaskStatus.LEASED.value),
            )
            if task_result.rowcount == 0:
                raise ValueError(f"Execution requires leased task status: {task.status.value}")
            self._record_task_transition(
                conn,
                task_id=task.id,
                from_status=TaskStatus.LEASED,
                to_status=TaskStatus.RUNNING,
                reason="execution_started",
                actor=owner,
                metadata={"lease_id": lease.id, "attempt_id": attempt.id, "run_id": run_id},
                created_at=started_at,
            )
        self.initialize_run_artifacts(run_id)
        if backend:
            self.persist_backend_snapshot(run_id, backend)
        self.write_run_manifest(run_id)
        return self.get_run(run_id)

    def finish_attempt_run(
        self,
        lease_id: str,
        *,
        run_id: str,
        owner: str = DEFAULT_TASK_LEASE_OWNER,
        success: bool,
        decision: str,
        run_status: str,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> None:
        lease = self.get_task_lease(lease_id)
        self._require_active_lease_authority(lease, owner, action="Execution finalization")
        if lease.attempt_id is None:
            raise ValueError(f"Execution requires linked task attempt: {lease.id}")
        attempt = self.get_task_attempt(lease.attempt_id)
        task = self.get_task(lease.task_id)
        if attempt.run_id != run_id:
            raise ValueError(f"Execution run mismatch for attempt: {attempt.id}")
        finished_at = now_iso()
        next_status = TaskStatus.SUCCEEDED if success else TaskStatus.FAILED
        reason = "execution_succeeded" if success else "execution_failed"
        with self.connect() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE id = ?",
                (run_status, finished_at, run_id),
            )
            current_status = task.status
            if current_status == TaskStatus.LEASED:
                validate_task_transition(TaskStatus.LEASED, TaskStatus.RUNNING)
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (TaskStatus.RUNNING.value, finished_at, task.id),
                )
                self._record_task_transition(
                    conn,
                    task_id=task.id,
                    from_status=TaskStatus.LEASED,
                    to_status=TaskStatus.RUNNING,
                    reason=f"{reason}_running",
                    actor=owner,
                    metadata={"lease_id": lease.id, "attempt_id": attempt.id, "run_id": run_id},
                    created_at=finished_at,
                )
                current_status = TaskStatus.RUNNING
            if current_status != next_status:
                validate_task_transition(current_status, next_status)
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (next_status.value, finished_at, task.id),
                )
                self._record_task_transition(
                    conn,
                    task_id=task.id,
                    from_status=current_status,
                    to_status=next_status,
                    reason=reason,
                    actor=owner,
                    metadata={
                        "lease_id": lease.id,
                        "attempt_id": attempt.id,
                        "run_id": run_id,
                        "decision": decision,
                    },
                    created_at=finished_at,
                )
            conn.execute(
                """
                UPDATE task_attempts
                SET status = ?,
                    finished_at = ?,
                    failure_code = ?,
                    failure_message = ?
                WHERE id = ?
                """,
                (next_status.value, finished_at, failure_code, failure_message, attempt.id),
            )
            result = conn.execute(
                """
                UPDATE task_leases
                SET status = ?, released_at = ?, metadata_json = ?
                WHERE id = ? AND status = ? AND owner = ?
                """,
                (
                    TaskLeaseStatus.RELEASED.value,
                    finished_at,
                    json.dumps(
                        sanitize_for_logging({**lease.metadata, "run_id": run_id, "decision": decision}),
                        sort_keys=True,
                    ),
                    lease.id,
                    TaskLeaseStatus.ACTIVE.value,
                    owner,
                ),
            )
            if result.rowcount == 0:
                raise ValueError(f"Execution finalization requires active lease owned by {owner}: {lease.id}")
        self.write_run_manifest(run_id)

    def start_read_only_lease_run(
        self,
        lease_id: str,
        *,
        backend: BackendConfig,
        approval_id: str | None = None,
        owner: str = DEFAULT_TASK_LEASE_OWNER,
    ) -> RunRecord:
        lease, attempt, task = self.validate_read_only_lease_for_execution(lease_id, owner=owner)
        goal = task.title if not task.description else f"{task.title}\n\n{task.description}"
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        started_at = now_iso()
        with self.connect() as conn:
            self._insert_run_in_conn(
                conn,
                run_id=run_id,
                timestamp=started_at,
                goal=goal,
                task_type=READ_ONLY_TASK_TYPE,
                status="running",
                backend=backend,
                approval_id=approval_id,
                task_id=task.id,
                objective_id=task.objective_id,
            )
            validate_task_transition(TaskStatus.LEASED, TaskStatus.RUNNING)
            result = conn.execute(
                """
                UPDATE task_attempts
                SET status = ?, run_id = ?, started_at = ?
                WHERE id = ?
                  AND run_id IS NULL
                  AND lease_id = ?
                  AND EXISTS (
                    SELECT 1 FROM task_leases
                    WHERE id = ? AND status = ? AND owner = ?
                  )
                """,
                (
                    TaskStatus.RUNNING.value,
                    run_id,
                    started_at,
                    attempt.id,
                    lease.id,
                    lease.id,
                    TaskLeaseStatus.ACTIVE.value,
                    owner,
                ),
            )
            if result.rowcount == 0:
                raise ValueError(f"Read-only execution requires active lease owned by {owner}: {lease.id}")
            task_result = conn.execute(
                "UPDATE tasks SET status = ?, run_id = ?, updated_at = ? WHERE id = ? AND status = ?",
                (TaskStatus.RUNNING.value, run_id, started_at, task.id, TaskStatus.LEASED.value),
            )
            if task_result.rowcount == 0:
                raise ValueError(f"Read-only execution requires leased task status: {task.status.value}")
            self._record_task_transition(
                conn,
                task_id=task.id,
                from_status=TaskStatus.LEASED,
                to_status=TaskStatus.RUNNING,
                reason="read_only_execution_started",
                actor=owner,
                metadata={"lease_id": lease.id, "attempt_id": attempt.id, "run_id": run_id},
                created_at=started_at,
            )
        self.initialize_run_artifacts(run_id)
        self.persist_backend_snapshot(run_id, backend)
        self.write_run_manifest(run_id)
        return self.get_run(run_id)

    def finish_read_only_lease_run(
        self,
        lease_id: str,
        *,
        run_id: str,
        owner: str = DEFAULT_TASK_LEASE_OWNER,
        success: bool,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> None:
        lease = self.get_task_lease(lease_id)
        self._require_active_lease_authority(lease, owner, action="Read-only execution finalization")
        if lease.attempt_id is None:
            raise ValueError(f"Read-only execution requires linked task attempt: {lease.id}")
        attempt = self.get_task_attempt(lease.attempt_id)
        task = self.get_task(lease.task_id)
        if attempt.run_id != run_id:
            raise ValueError(f"Read-only execution run mismatch for attempt: {attempt.id}")
        finished_at = now_iso()
        next_status = TaskStatus.SUCCEEDED if success else TaskStatus.FAILED
        reason = "read_only_execution_succeeded" if success else "read_only_execution_failed"
        with self.connect() as conn:
            current_status = task.status
            if current_status == TaskStatus.LEASED:
                validate_task_transition(TaskStatus.LEASED, TaskStatus.RUNNING)
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (TaskStatus.RUNNING.value, finished_at, task.id),
                )
                self._record_task_transition(
                    conn,
                    task_id=task.id,
                    from_status=TaskStatus.LEASED,
                    to_status=TaskStatus.RUNNING,
                    reason=f"{reason}_running",
                    actor=owner,
                    metadata={"lease_id": lease.id, "attempt_id": attempt.id, "run_id": run_id},
                    created_at=finished_at,
                )
                current_status = TaskStatus.RUNNING
            if current_status != next_status:
                validate_task_transition(current_status, next_status)
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (next_status.value, finished_at, task.id),
                )
                self._record_task_transition(
                    conn,
                    task_id=task.id,
                    from_status=current_status,
                    to_status=next_status,
                    reason=reason,
                    actor=owner,
                    metadata={"lease_id": lease.id, "attempt_id": attempt.id, "run_id": run_id},
                    created_at=finished_at,
                )
            conn.execute(
                """
                UPDATE task_attempts
                SET status = ?,
                    finished_at = ?,
                    failure_code = ?,
                    failure_message = ?
                WHERE id = ?
                """,
                (next_status.value, finished_at, failure_code, failure_message, attempt.id),
            )
            result = conn.execute(
                """
                UPDATE task_leases
                SET status = ?, released_at = ?, metadata_json = ?
                WHERE id = ? AND status = ? AND owner = ?
                """,
                (
                    TaskLeaseStatus.RELEASED.value,
                    finished_at,
                    json.dumps(
                        sanitize_for_logging(
                            {
                                **lease.metadata,
                                "run_id": run_id,
                                "decision": "read_only_summary_completed" if success else "read_only_summary_failed",
                            }
                        ),
                        sort_keys=True,
                    ),
                    lease.id,
                    TaskLeaseStatus.ACTIVE.value,
                    owner,
                ),
            )
            if result.rowcount == 0:
                raise ValueError(f"Read-only execution finalization requires active lease owned by {owner}: {lease.id}")

    def _validate_read_only_task_metadata(self, task: TaskRecord) -> None:
        if task.metadata.get("execution_adapter") != READ_ONLY_EXECUTION_ADAPTER:
            raise ValueError("Read-only execution requires execution_adapter=read_only_summary")
        if task.metadata.get("task_type") != READ_ONLY_TASK_TYPE:
            raise ValueError("Read-only execution requires task_type=read_only_repo_summary")
        forbidden = sorted(key for key in READ_ONLY_FORBIDDEN_METADATA_KEYS if bool(task.metadata.get(key)))
        if forbidden:
            raise ValueError(f"Read-only execution rejected by task metadata: {', '.join(forbidden)}")

    def inspect_task_lease(self, lease_id: str) -> DaemonLeaseInspection:
        from harness.execution import evaluate_registered_adapter_security_decision, inspect_execution_eligibility

        lease = self.get_task_lease(lease_id)
        task: TaskRecord | None = None
        attempt: TaskAttempt | None = None
        run: RunRecord | None = None
        manifest: RunManifest | None = None
        try:
            task = self.get_task(lease.task_id)
        except KeyError:
            task = None
        if lease.attempt_id is not None:
            try:
                attempt = self.get_task_attempt(lease.attempt_id)
            except KeyError:
                attempt = None
        if attempt is not None and attempt.run_id is not None:
            try:
                run = self.get_run(attempt.run_id)
                manifest = self.build_run_manifest(run.id)
            except KeyError:
                run = None
                manifest = None
        context_provenance = self.build_context_provenance(task=task, run_id=run.id if run is not None else None)
        execution_eligibility = inspect_execution_eligibility(self.project_root, lease, task, attempt)
        security_decision = evaluate_registered_adapter_security_decision(
            self.project_root,
            lease,
            task,
            attempt,
            owner=lease.owner,
        )
        blocked_state_explanations = [
            *explanations_from_eligibility(
                execution_eligibility,
                lease_id=lease.id,
                project_root=str(self.project_root),
            ),
            *explanations_from_security_decision(
                security_decision,
                lease_id=lease.id,
                project_root=str(self.project_root),
            ),
        ]
        return DaemonLeaseInspection(
            project_root=self.project_root,
            lease=lease,
            task=task,
            attempt=attempt,
            run=run,
            manifest=manifest,
            dry_run_eligibility=self._dry_run_eligibility_for_inspection(lease, task, attempt),
            read_only_eligibility=self._read_only_eligibility_for_inspection(lease, task, attempt),
            execution_eligibility=execution_eligibility,
            security_decision=security_decision,
            context_provenance=context_provenance,
            untrusted_context_warnings=_context_warnings(context_provenance),
            blocked_state_explanations=blocked_state_explanations,
            recovery_recommendation=self._lease_recovery_recommendation(lease, task, attempt, run),
        )

    def _dry_run_eligibility_for_inspection(
        self,
        lease: TaskLease,
        task: TaskRecord | None,
        attempt: TaskAttempt | None,
    ) -> dict[str, Any]:
        if task is None:
            return {"eligible": False, "reason": "Task not found."}
        if attempt is None:
            return {"eligible": False, "reason": "Task attempt not found."}
        if lease.status != TaskLeaseStatus.ACTIVE:
            return {"eligible": False, "reason": f"Lease is not active: {lease.status.value}."}
        if attempt.run_id is not None:
            return {"eligible": False, "reason": "Task attempt is already linked to a run."}
        if task.status != TaskStatus.LEASED:
            return {"eligible": False, "reason": f"Task status is not leased: {task.status.value}."}
        if task.required_approvals:
            return {
                "eligible": False,
                "reason": "Task has unresolved required approvals.",
                "required_approvals": sorted(set(task.required_approvals)),
            }
        try:
            self._validate_dry_run_task_metadata(task)
        except ValueError as exc:
            return {"eligible": False, "reason": str(exc)}
        return {"eligible": True, "reason": "Dry-run execution is available."}

    def _read_only_eligibility_for_inspection(
        self,
        lease: TaskLease,
        task: TaskRecord | None,
        attempt: TaskAttempt | None,
    ) -> dict[str, Any]:
        if task is None:
            return {"eligible": False, "reason": "Task not found."}
        if attempt is None:
            return {"eligible": False, "reason": "Task attempt not found."}
        if lease.status != TaskLeaseStatus.ACTIVE:
            return {"eligible": False, "reason": f"Lease is not active: {lease.status.value}."}
        if attempt.run_id is not None:
            return {"eligible": False, "reason": "Task attempt is already linked to a run."}
        if task.status != TaskStatus.LEASED:
            return {"eligible": False, "reason": f"Task status is not leased: {task.status.value}."}
        if task.required_approvals:
            return {
                "eligible": False,
                "reason": "Task has unresolved required approvals.",
                "required_approvals": sorted(set(task.required_approvals)),
            }
        try:
            self._validate_read_only_task_metadata(task)
        except ValueError as exc:
            return {"eligible": False, "reason": str(exc)}
        return {"eligible": True, "reason": "Read-only summary execution is available."}

    def _lease_recovery_recommendation(
        self,
        lease: TaskLease,
        task: TaskRecord | None,
        attempt: TaskAttempt | None,
        run: RunRecord | None,
    ) -> dict[str, Any]:
        if task is None or attempt is None:
            return {"action": "inspect", "reason": "Lease is missing linked task or attempt."}
        if attempt.run_id is None:
            if lease.status == TaskLeaseStatus.ACTIVE and lease.expires_at <= datetime.now(timezone.utc):
                return {"action": "recover_expired_unexecuted_lease", "reason": "Active lease expired before run creation."}
            return {"action": "none", "reason": "Lease has no linked run."}
        if run is None:
            return {"action": "inspect", "reason": "Attempt references a missing run."}
        if run.status == "completed" and (
            task.status != TaskStatus.SUCCEEDED
            or attempt.status != TaskStatus.SUCCEEDED
            or lease.status == TaskLeaseStatus.ACTIVE
        ):
            return {"action": "reconcile_succeeded", "reason": "Completed linked run evidence is not fully reflected in task state."}
        if run.status == "failed" and (
            task.status != TaskStatus.FAILED
            or attempt.status != TaskStatus.FAILED
            or lease.status == TaskLeaseStatus.ACTIVE
        ):
            return {"action": "reconcile_failed", "reason": "Failed linked run evidence is not fully reflected in task state."}
        if lease.status == TaskLeaseStatus.ACTIVE and lease.expires_at <= datetime.now(timezone.utc):
            return {"action": "fail_for_operator_inspection", "reason": "Expired active lease has non-terminal linked run."}
        return {"action": "none", "reason": "No recovery action recommended."}

    def renew_daemon_leases(
        self,
        owner: str,
        lease_duration_minutes: int = DEFAULT_TASK_LEASE_MINUTES,
        objective_id: str | None = None,
    ) -> list[TaskLease]:
        timestamp = now_iso()
        expires_at = (parse_dt(timestamp) + timedelta(minutes=lease_duration_minutes)).isoformat()
        renewed_ids: list[str] = []
        with self.connect() as conn:
            if objective_id is not None:
                rows = conn.execute(
                    """
                    SELECT tl.* FROM task_leases tl
                    JOIN tasks t ON t.id = tl.task_id
                    WHERE tl.owner = ? AND tl.status = ? AND tl.expires_at > ? AND t.objective_id = ?
                    ORDER BY tl.acquired_at ASC, tl.id ASC
                    """,
                    (owner, TaskLeaseStatus.ACTIVE.value, timestamp, objective_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM task_leases
                    WHERE owner = ? AND status = ? AND expires_at > ?
                    ORDER BY acquired_at ASC, id ASC
                    """,
                    (owner, TaskLeaseStatus.ACTIVE.value, timestamp),
                ).fetchall()
            daemon: DaemonRecord | None = None
            for row in rows:
                lease = self._row_to_task_lease(row)
                renewal_rejection = self._active_lease_renewal_rejection(conn, lease)
                if renewal_rejection is not None:
                    if daemon is None:
                        daemon = self.ensure_daemon(owner=owner)
                    self._release_inconsistent_active_lease(
                        conn,
                        daemon_id=daemon.id,
                        lease=lease,
                        owner=owner,
                        reason=renewal_rejection["reason"],
                        timestamp=timestamp,
                    )
                    continue
                conn.execute(
                    "UPDATE task_leases SET heartbeat_at = ?, expires_at = ? WHERE id = ?",
                    (timestamp, expires_at, row["id"]),
                )
                renewed_ids.append(row["id"])
        return [lease for lease in self.list_task_leases() if lease.id in set(renewed_ids)]

    def _active_lease_renewal_rejection(
        self,
        conn: sqlite3.Connection,
        lease: TaskLease,
    ) -> dict[str, str] | None:
        task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (lease.task_id,)).fetchone()
        if task_row is None:
            return {"reason": "missing_task"}
        task = self._row_to_task(task_row)
        if task.status not in {TaskStatus.LEASED, TaskStatus.RUNNING}:
            return {"reason": f"task_status_{task.status.value}"}
        if lease.attempt_id is None:
            return {"reason": "missing_attempt"}
        attempt_row = conn.execute("SELECT * FROM task_attempts WHERE id = ?", (lease.attempt_id,)).fetchone()
        if attempt_row is None:
            return {"reason": "missing_attempt"}
        attempt = self._row_to_task_attempt(attempt_row)
        if attempt.run_id is None:
            if task.status == TaskStatus.LEASED and attempt.status == TaskStatus.LEASED:
                return None
            return {"reason": f"no_run_attempt_status_{attempt.status.value}"}
        run_row = conn.execute("SELECT * FROM runs WHERE id = ?", (attempt.run_id,)).fetchone()
        if run_row is None:
            return {"reason": "missing_run"}
        run = self._row_to_run(run_row)
        if task.status == TaskStatus.RUNNING and attempt.status == TaskStatus.RUNNING and run.status not in {
            "completed",
            "failed",
        }:
            return None
        return {"reason": f"linked_run_status_{run.status}"}

    def _release_inconsistent_active_lease(
        self,
        conn: sqlite3.Connection,
        *,
        daemon_id: str,
        lease: TaskLease,
        owner: str,
        reason: str,
        timestamp: str,
    ) -> str | None:
        if lease.status != TaskLeaseStatus.ACTIVE:
            return None
        task: TaskRecord | None = None
        task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (lease.task_id,)).fetchone()
        if task_row is not None:
            task = self._row_to_task(task_row)
        attempt: TaskAttempt | None = None
        if lease.attempt_id is not None:
            attempt_row = conn.execute("SELECT * FROM task_attempts WHERE id = ?", (lease.attempt_id,)).fetchone()
            if attempt_row is not None:
                attempt = self._row_to_task_attempt(attempt_row)
        next_task_status: TaskStatus | None = None
        next_attempt_status: TaskStatus | None = None
        if task is not None and task.status in {TaskStatus.LEASED, TaskStatus.RUNNING}:
            if attempt is None:
                next_task_status = self._task_requeue_status(task)
            elif attempt.run_id is None:
                if attempt.status in {
                    TaskStatus.WAITING_APPROVAL,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                    TaskStatus.SKIPPED,
                }:
                    next_task_status = attempt.status
                else:
                    next_task_status = TaskStatus.FAILED
                next_attempt_status = next_task_status
        if next_attempt_status is not None and attempt is not None and attempt.status != next_attempt_status:
            conn.execute(
                """
                UPDATE task_attempts
                SET status = ?,
                    finished_at = COALESCE(finished_at, ?),
                    failure_code = COALESCE(failure_code, ?),
                    failure_message = COALESCE(failure_message, ?)
                WHERE id = ? AND run_id IS NULL
                """,
                (
                    next_attempt_status.value,
                    timestamp,
                    "inconsistent_active_lease",
                    "Daemon renewal released an inconsistent active lease before run creation.",
                    attempt.id,
                ),
            )
        if next_task_status is not None and task is not None and task.status != next_task_status:
            validate_task_transition(task.status, next_task_status)
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (next_task_status.value, timestamp, task.id),
            )
            self._record_task_transition(
                conn,
                task_id=task.id,
                from_status=task.status,
                to_status=next_task_status,
                reason="inconsistent_active_lease_released",
                actor=owner,
                metadata={"lease_id": lease.id, "attempt_id": lease.attempt_id, "reason": reason},
                created_at=timestamp,
            )
        conn.execute(
            """
            UPDATE task_leases
            SET status = ?, released_at = ?, heartbeat_at = COALESCE(heartbeat_at, ?), metadata_json = ?
            WHERE id = ? AND status = ?
            """,
            (
                TaskLeaseStatus.RELEASED.value,
                timestamp,
                timestamp,
                json.dumps(
                    sanitize_for_logging(
                        {
                            **lease.metadata,
                            "decision": "inconsistent_active_lease_released",
                            "reason": reason,
                        }
                    ),
                    sort_keys=True,
                    default=str,
                ),
                lease.id,
                TaskLeaseStatus.ACTIVE.value,
            ),
        )
        return self._record_daemon_event(
            conn,
            daemon_id=daemon_id,
            event_type="release_inconsistent_lease",
            message="Released inconsistent active lease instead of renewing it.",
            metadata={
                "lease_id": lease.id,
                "task_id": lease.task_id,
                "attempt_id": lease.attempt_id,
                "owner": owner,
                "reason": reason,
                "lease_status": TaskLeaseStatus.RELEASED.value,
                "task_status": (
                    next_task_status.value
                    if next_task_status is not None
                    else task.status.value
                    if task is not None
                    else None
                ),
                "attempt_status": (
                    next_attempt_status.value
                    if next_attempt_status is not None
                    else attempt.status.value
                    if attempt is not None
                    else None
                ),
            },
            created_at=timestamp,
        )

    def recover_daemon_leases(self, owner: str, pid: int | None = None) -> DaemonRecoveryResult:
        daemon = self.ensure_daemon(owner=owner, pid=pid)
        timestamp = now_iso()
        expired_ids: list[str] = []
        recovered_task_ids: list[str] = []
        event_ids: list[str] = []
        with self.connect() as conn:
            dry_run_recovery = self._recover_dry_run_contracts(
                conn,
                daemon_id=daemon.id,
                owner=owner,
                timestamp=timestamp,
            )
            expired_ids.extend(dry_run_recovery["expired_ids"])
            recovered_task_ids.extend(dry_run_recovery["recovered_task_ids"])
            event_ids.extend(dry_run_recovery["event_ids"])
            rows = conn.execute(
                """
                SELECT * FROM task_leases
                WHERE status = ? AND expires_at <= ?
                ORDER BY expires_at ASC, id ASC
                """,
                (TaskLeaseStatus.ACTIVE.value, timestamp),
            ).fetchall()
            for row in rows:
                lease = self._row_to_task_lease(row)
                if lease.id in set(expired_ids):
                    continue
                recovery = self._recover_expired_active_lease(
                    conn,
                    daemon_id=daemon.id,
                    owner=owner,
                    lease=lease,
                    timestamp=timestamp,
                )
                if recovery["lease_status"] == TaskLeaseStatus.EXPIRED.value:
                    expired_ids.append(lease.id)
                if recovery["recovered_task_id"] is not None:
                    recovered_task_ids.append(recovery["recovered_task_id"])
                if recovery["event_id"] is not None:
                    event_ids.append(recovery["event_id"])
        return DaemonRecoveryResult(
            daemon_id=daemon.id,
            owner=daemon.owner,
            project_root=self.project_root,
            renewed_leases=[],
            expired_leases=[lease for lease in self.list_task_leases() if lease.id in set(expired_ids)],
            recovered_tasks=[self.get_task(task_id) for task_id in recovered_task_ids],
            events=[self.get_daemon_event(event_id) for event_id in event_ids],
        )

    def _recover_expired_active_lease(
        self,
        conn: sqlite3.Connection,
        *,
        daemon_id: str,
        owner: str,
        lease: TaskLease,
        timestamp: str,
    ) -> dict[str, str | None]:
        task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (lease.task_id,)).fetchone()
        task = self._row_to_task(task_row) if task_row is not None else None
        attempt: TaskAttempt | None = None
        if lease.attempt_id is not None:
            attempt_row = conn.execute("SELECT * FROM task_attempts WHERE id = ?", (lease.attempt_id,)).fetchone()
            if attempt_row is not None:
                attempt = self._row_to_task_attempt(attempt_row)

        if task is None:
            event_id = self._expire_inconsistent_active_lease(
                conn,
                daemon_id=daemon_id,
                owner=owner,
                lease=lease,
                task=None,
                attempt=attempt,
                reason="missing_task",
                timestamp=timestamp,
            )
            return {
                "lease_status": TaskLeaseStatus.EXPIRED.value,
                "recovered_task_id": None,
                "event_id": event_id,
            }

        if attempt is not None and attempt.run_id is not None:
            run_row = conn.execute("SELECT * FROM runs WHERE id = ?", (attempt.run_id,)).fetchone()
            run = self._row_to_run(run_row) if run_row is not None else None
            if run is not None and run.status == "completed":
                changed = self._reconcile_dry_run_terminal_state(
                    conn,
                    task=task,
                    attempt=attempt,
                    lease=lease,
                    next_status=TaskStatus.SUCCEEDED,
                    lease_status=TaskLeaseStatus.RELEASED,
                    timestamp=timestamp,
                    actor=owner,
                    reason="execution_recovery_succeeded",
                    failure_code=None,
                    failure_message=None,
                )
                event_id = self._record_daemon_event(
                    conn,
                    daemon_id=daemon_id,
                    event_type="recover_execution",
                    message="Reconciled completed execution evidence to succeeded task state.",
                    metadata={
                        "lease_id": lease.id,
                        "attempt_id": attempt.id,
                        "task_id": task.id,
                        "run_id": run.id,
                        "run_status": run.status,
                        "next_status": TaskStatus.SUCCEEDED.value,
                    },
                    created_at=timestamp,
                )
                return {
                    "lease_status": TaskLeaseStatus.RELEASED.value,
                    "recovered_task_id": task.id if changed else None,
                    "event_id": event_id,
                }
            if run is not None and run.status == "failed":
                changed = self._reconcile_dry_run_terminal_state(
                    conn,
                    task=task,
                    attempt=attempt,
                    lease=lease,
                    next_status=TaskStatus.FAILED,
                    lease_status=TaskLeaseStatus.RELEASED,
                    timestamp=timestamp,
                    actor=owner,
                    reason="execution_recovery_failed",
                    failure_code="execution_failed",
                    failure_message="Execution recovery reconciled failed run evidence.",
                )
                event_id = self._record_daemon_event(
                    conn,
                    daemon_id=daemon_id,
                    event_type="recover_execution",
                    message="Reconciled failed execution evidence to failed task state.",
                    metadata={
                        "lease_id": lease.id,
                        "attempt_id": attempt.id,
                        "task_id": task.id,
                        "run_id": run.id,
                        "run_status": run.status,
                        "next_status": TaskStatus.FAILED.value,
                    },
                    created_at=timestamp,
                )
                return {
                    "lease_status": TaskLeaseStatus.RELEASED.value,
                    "recovered_task_id": task.id if changed else None,
                    "event_id": event_id,
                }

            failure_reason = "missing_run" if run is None else "nonterminal_linked_run"
            changed = self._reconcile_dry_run_terminal_state(
                conn,
                task=task,
                attempt=attempt,
                lease=lease,
                next_status=TaskStatus.FAILED,
                lease_status=TaskLeaseStatus.EXPIRED,
                timestamp=timestamp,
                actor=owner,
                reason="execution_recovery_required",
                failure_code="execution_recovery_required",
                failure_message="Daemon recovery found an expired lease with missing or non-terminal linked run.",
            )
            event_id = self._record_daemon_event(
                conn,
                daemon_id=daemon_id,
                event_type="recover_execution",
                message="Failed execution task for operator inspection after expired linked run.",
                metadata={
                    "lease_id": lease.id,
                    "attempt_id": attempt.id,
                    "task_id": task.id,
                    "run_id": attempt.run_id,
                    "run_status": run.status if run is not None else None,
                    "reason": failure_reason,
                    "next_status": TaskStatus.FAILED.value,
                    "failure_code": "execution_recovery_required",
                },
                created_at=timestamp,
            )
            return {
                "lease_status": TaskLeaseStatus.EXPIRED.value,
                "recovered_task_id": task.id if changed else None,
                "event_id": event_id,
            }

        if task.status not in {TaskStatus.LEASED, TaskStatus.RUNNING}:
            event_id = self._expire_inconsistent_active_lease(
                conn,
                daemon_id=daemon_id,
                owner=owner,
                lease=lease,
                task=task,
                attempt=attempt,
                reason=f"task_status_{task.status.value}",
                timestamp=timestamp,
            )
            return {
                "lease_status": TaskLeaseStatus.EXPIRED.value,
                "recovered_task_id": None,
                "event_id": event_id,
            }

        next_status = self._task_requeue_status(task)
        validate_task_transition(task.status, next_status)
        conn.execute(
            """
            UPDATE task_leases
            SET status = ?, released_at = ?, heartbeat_at = COALESCE(heartbeat_at, ?), metadata_json = ?
            WHERE id = ?
            """,
            (
                TaskLeaseStatus.EXPIRED.value,
                timestamp,
                timestamp,
                json.dumps(
                    sanitize_for_logging(
                        {
                            **lease.metadata,
                            "decision": "lease_expired",
                            "reason": "expired_before_execution",
                        }
                    ),
                    sort_keys=True,
                    default=str,
                ),
                lease.id,
            ),
        )
        if lease.attempt_id is not None:
            conn.execute(
                """
                UPDATE task_attempts
                SET status = ?, finished_at = ?, failure_code = ?, failure_message = ?
                WHERE id = ? AND run_id IS NULL
                """,
                (
                    TaskStatus.FAILED.value,
                    timestamp,
                    "lease_expired",
                    "Daemon recovery expired an active lease before execution.",
                    lease.attempt_id,
                ),
            )
        conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (next_status.value, timestamp, task.id),
        )
        self._record_task_transition(
            conn,
            task_id=task.id,
            from_status=task.status,
            to_status=next_status,
            reason="lease_expired",
            actor=owner,
            metadata={"lease_id": lease.id, "attempt_id": lease.attempt_id},
            created_at=timestamp,
        )
        event_id = self._record_daemon_event(
            conn,
            daemon_id=daemon_id,
            event_type="recover_lease",
            message="Expired active lease and returned task to queue.",
            metadata={
                "lease_id": lease.id,
                "task_id": task.id,
                "attempt_id": lease.attempt_id,
                "next_status": next_status.value,
            },
            created_at=timestamp,
        )
        return {
            "lease_status": TaskLeaseStatus.EXPIRED.value,
            "recovered_task_id": task.id,
            "event_id": event_id,
        }

    def _expire_inconsistent_active_lease(
        self,
        conn: sqlite3.Connection,
        *,
        daemon_id: str,
        owner: str,
        lease: TaskLease,
        task: TaskRecord | None,
        attempt: TaskAttempt | None,
        reason: str,
        timestamp: str,
    ) -> str:
        if attempt is not None and attempt.run_id is None and attempt.status in {TaskStatus.LEASED, TaskStatus.RUNNING}:
            conn.execute(
                """
                UPDATE task_attempts
                SET status = ?,
                    finished_at = COALESCE(finished_at, ?),
                    failure_code = COALESCE(failure_code, ?),
                    failure_message = COALESCE(failure_message, ?)
                WHERE id = ?
                """,
                (
                    TaskStatus.FAILED.value,
                    timestamp,
                    "inconsistent_expired_lease",
                    "Daemon recovery expired an inconsistent active lease.",
                    attempt.id,
                ),
            )
            attempt = attempt.model_copy(update={"status": TaskStatus.FAILED})
        conn.execute(
            """
            UPDATE task_leases
            SET status = ?, released_at = ?, heartbeat_at = COALESCE(heartbeat_at, ?), metadata_json = ?
            WHERE id = ? AND status = ?
            """,
            (
                TaskLeaseStatus.EXPIRED.value,
                timestamp,
                timestamp,
                json.dumps(
                    sanitize_for_logging(
                        {
                            **lease.metadata,
                            "decision": "inconsistent_expired_lease",
                            "reason": reason,
                        }
                    ),
                    sort_keys=True,
                    default=str,
                ),
                lease.id,
                TaskLeaseStatus.ACTIVE.value,
            ),
        )
        return self._record_daemon_event(
            conn,
            daemon_id=daemon_id,
            event_type="recover_inconsistent_lease",
            message="Expired inconsistent active lease during daemon recovery.",
            metadata={
                "lease_id": lease.id,
                "task_id": lease.task_id,
                "attempt_id": lease.attempt_id,
                "owner": owner,
                "reason": reason,
                "lease_status": TaskLeaseStatus.EXPIRED.value,
                "task_status": task.status.value if task is not None else None,
                "attempt_status": attempt.status.value if attempt is not None else None,
            },
            created_at=timestamp,
        )

    def _recover_dry_run_contracts(
        self,
        conn: sqlite3.Connection,
        *,
        daemon_id: str,
        owner: str,
        timestamp: str,
    ) -> dict[str, list[str]]:
        expired_ids: list[str] = []
        recovered_task_ids: list[str] = []
        event_ids: list[str] = []
        rows = conn.execute(
            """
            SELECT
              task_leases.id AS lease_id,
              task_leases.status AS lease_status,
              task_leases.expires_at AS lease_expires_at,
              task_leases.attempt_id AS lease_attempt_id,
              task_attempts.run_id AS attempt_run_id,
              runs.status AS run_status
            FROM task_leases
            JOIN task_attempts ON task_attempts.id = task_leases.attempt_id
            JOIN runs ON runs.id = task_attempts.run_id
            WHERE task_leases.status IN (?, ?)
            ORDER BY task_leases.acquired_at ASC, task_leases.id ASC
            """,
            (TaskLeaseStatus.ACTIVE.value, TaskLeaseStatus.RELEASED.value),
        ).fetchall()
        for row in rows:
            lease = self._row_to_task_lease(
                conn.execute("SELECT * FROM task_leases WHERE id = ?", (row["lease_id"],)).fetchone()
            )
            task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (lease.task_id,)).fetchone()
            if task_row is None:
                continue
            task = self._row_to_task(task_row)
            adapter = task.metadata.get("execution_adapter")
            if adapter not in {DRY_RUN_EXECUTION_ADAPTER, READ_ONLY_EXECUTION_ADAPTER}:
                continue
            attempt = self._row_to_task_attempt(
                conn.execute("SELECT * FROM task_attempts WHERE id = ?", (lease.attempt_id,)).fetchone()
            )
            run = self._row_to_run(conn.execute("SELECT * FROM runs WHERE id = ?", (attempt.run_id,)).fetchone())
            expected_task_type = DRY_RUN_TASK_TYPE if adapter == DRY_RUN_EXECUTION_ADAPTER else READ_ONLY_TASK_TYPE
            if run.task_type != expected_task_type:
                continue
            label = "dry_run" if adapter == DRY_RUN_EXECUTION_ADAPTER else "read_only"
            label_text = "Dry-run" if adapter == DRY_RUN_EXECUTION_ADAPTER else "Read-only"
            if run.status == "completed":
                changed = self._reconcile_dry_run_terminal_state(
                    conn,
                    task=task,
                    attempt=attempt,
                    lease=lease,
                    next_status=TaskStatus.SUCCEEDED,
                    lease_status=TaskLeaseStatus.RELEASED,
                    timestamp=timestamp,
                    actor=owner,
                    reason=f"{label}_recovery_succeeded",
                    failure_code=None,
                    failure_message=None,
                )
                if changed:
                    recovered_task_ids.append(task.id)
                    event_ids.append(
                        self._record_daemon_event(
                            conn,
                            daemon_id=daemon_id,
                            event_type=f"recover_{label}",
                            message=f"Reconciled completed {label_text.lower()} evidence to succeeded task state.",
                            metadata={
                                "lease_id": lease.id,
                                "attempt_id": attempt.id,
                                "task_id": task.id,
                                "run_id": run.id,
                                "next_status": TaskStatus.SUCCEEDED.value,
                            },
                            created_at=timestamp,
                        )
                    )
                continue
            if run.status == "failed":
                changed = self._reconcile_dry_run_terminal_state(
                    conn,
                    task=task,
                    attempt=attempt,
                    lease=lease,
                    next_status=TaskStatus.FAILED,
                    lease_status=TaskLeaseStatus.RELEASED,
                    timestamp=timestamp,
                    actor=owner,
                    reason=f"{label}_recovery_failed",
                    failure_code=f"{label}_failed",
                    failure_message=f"{label_text} recovery reconciled failed run evidence.",
                )
                if changed:
                    recovered_task_ids.append(task.id)
                    event_ids.append(
                        self._record_daemon_event(
                            conn,
                            daemon_id=daemon_id,
                            event_type=f"recover_{label}",
                            message=f"Reconciled failed {label_text.lower()} evidence to failed task state.",
                            metadata={
                                "lease_id": lease.id,
                                "attempt_id": attempt.id,
                                "task_id": task.id,
                                "run_id": run.id,
                                "next_status": TaskStatus.FAILED.value,
                            },
                            created_at=timestamp,
                        )
                    )
                continue
            if lease.status == TaskLeaseStatus.ACTIVE and lease.expires_at <= parse_dt(timestamp):
                changed = self._reconcile_dry_run_terminal_state(
                    conn,
                    task=task,
                    attempt=attempt,
                    lease=lease,
                    next_status=TaskStatus.FAILED,
                    lease_status=TaskLeaseStatus.EXPIRED,
                    timestamp=timestamp,
                    actor=owner,
                    reason=f"{label}_recovery_required",
                    failure_code=f"{label}_recovery_required",
                    failure_message=f"{label_text} recovery found expired lease with non-terminal linked run.",
                )
                if changed:
                    expired_ids.append(lease.id)
                    recovered_task_ids.append(task.id)
                    event_ids.append(
                        self._record_daemon_event(
                            conn,
                            daemon_id=daemon_id,
                            event_type=f"recover_{label}",
                            message=f"Failed {label_text.lower()} task for operator inspection after expired linked run.",
                            metadata={
                                "lease_id": lease.id,
                                "attempt_id": attempt.id,
                                "task_id": task.id,
                                "run_id": run.id,
                                "next_status": TaskStatus.FAILED.value,
                                "failure_code": f"{label}_recovery_required",
                            },
                            created_at=timestamp,
                        )
                    )
                continue
            if lease.status == TaskLeaseStatus.ACTIVE and task.status == TaskStatus.RUNNING:
                event_ids.append(
                    self._record_daemon_event(
                        conn,
                        daemon_id=daemon_id,
                        event_type=f"inspect_{label}",
                        message=f"{label_text} lease has non-terminal linked run; operator inspection recommended.",
                        metadata={
                            "lease_id": lease.id,
                            "attempt_id": attempt.id,
                            "task_id": task.id,
                            "run_id": run.id,
                            "run_status": run.status,
                        },
                        created_at=timestamp,
                    )
                )
        return {
            "expired_ids": expired_ids,
            "recovered_task_ids": recovered_task_ids,
            "event_ids": event_ids,
        }

    def _reconcile_dry_run_terminal_state(
        self,
        conn: sqlite3.Connection,
        *,
        task: TaskRecord,
        attempt: TaskAttempt,
        lease: TaskLease,
        next_status: TaskStatus,
        lease_status: TaskLeaseStatus,
        timestamp: str,
        actor: str,
        reason: str,
        failure_code: str | None,
        failure_message: str | None,
    ) -> bool:
        changed = False
        if task.status not in {next_status, TaskStatus.CANCELLED, TaskStatus.SKIPPED}:
            current_status = task.status
            if current_status == TaskStatus.LEASED:
                validate_task_transition(TaskStatus.LEASED, TaskStatus.RUNNING)
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (TaskStatus.RUNNING.value, timestamp, task.id),
                )
                self._record_task_transition(
                    conn,
                    task_id=task.id,
                    from_status=TaskStatus.LEASED,
                    to_status=TaskStatus.RUNNING,
                    reason=f"{reason}_running",
                    actor=actor,
                    metadata={"lease_id": lease.id, "attempt_id": attempt.id, "run_id": attempt.run_id},
                    created_at=timestamp,
                )
                current_status = TaskStatus.RUNNING
            validate_task_transition(current_status, next_status)
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (next_status.value, timestamp, task.id),
            )
            self._record_task_transition(
                conn,
                task_id=task.id,
                from_status=current_status,
                to_status=next_status,
                reason=reason,
                actor=actor,
                metadata={"lease_id": lease.id, "attempt_id": attempt.id, "run_id": attempt.run_id},
                created_at=timestamp,
            )
            changed = True
        if attempt.status != next_status:
            conn.execute(
                """
                UPDATE task_attempts
                SET status = ?,
                    finished_at = COALESCE(finished_at, ?),
                    failure_code = COALESCE(?, failure_code),
                    failure_message = COALESCE(?, failure_message)
                WHERE id = ?
                """,
                (next_status.value, timestamp, failure_code, failure_message, attempt.id),
            )
            changed = True
        if lease.status != lease_status:
            conn.execute(
                """
                UPDATE task_leases
                SET status = ?, released_at = COALESCE(released_at, ?), metadata_json = ?
                WHERE id = ?
                """,
                (
                    lease_status.value,
                    timestamp,
                    json.dumps(
                        sanitize_for_logging(
                            {
                                **lease.metadata,
                                "run_id": attempt.run_id,
                                "recovery_reason": reason,
                            }
                        ),
                        sort_keys=True,
                    ),
                    lease.id,
                ),
            )
            changed = True
        return changed

    def _task_requeue_status(self, task: TaskRecord) -> TaskStatus:
        if task.required_approvals:
            return TaskStatus.WAITING_APPROVAL
        if not self._task_dependencies_completed(task):
            return TaskStatus.BLOCKED
        return TaskStatus.READY

    def ensure_daemon(self, owner: str, pid: int | None = None) -> DaemonRecord:
        timestamp = now_iso()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM daemon_records
                WHERE owner = ? AND status = ?
                ORDER BY heartbeat_at DESC
                LIMIT 1
                """,
                (owner, DaemonStatus.RUNNING.value),
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE daemon_records SET heartbeat_at = ?, pid = COALESCE(?, pid) WHERE id = ?",
                    (timestamp, pid, row["id"]),
                )
                daemon_id = row["id"]
                self._record_daemon_event(
                    conn,
                    daemon_id=daemon_id,
                    event_type="heartbeat",
                    message="Daemon heartbeat recorded.",
                    metadata={},
                    created_at=timestamp,
                )
                updated = conn.execute(
                    "SELECT * FROM daemon_records WHERE id = ?", (daemon_id,)
                ).fetchone()
                return self._row_to_daemon(updated)
            daemon_id = f"daemon_{uuid.uuid4().hex[:12]}"
            conn.execute(
                """
                INSERT INTO daemon_records (
                  id, owner, status, pid, project_root, started_at, heartbeat_at,
                  stopped_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    daemon_id,
                    owner,
                    DaemonStatus.RUNNING.value,
                    pid,
                    str(self.project_root),
                    timestamp,
                    timestamp,
                    None,
                    "{}",
                ),
            )
            self._record_daemon_event(
                conn,
                daemon_id=daemon_id,
                event_type="start",
                message="Daemon record started.",
                metadata={},
                created_at=timestamp,
            )
        return self.get_daemon(daemon_id)

    def stop_daemons(self, owner: str | None = None) -> list[DaemonRecord]:
        timestamp = now_iso()
        stopped_ids: list[str] = []
        with self.connect() as conn:
            if owner is None:
                rows = conn.execute(
                    """
                    SELECT * FROM daemon_records
                    WHERE status IN (?, ?)
                    ORDER BY heartbeat_at DESC
                    """,
                    (DaemonStatus.RUNNING.value, DaemonStatus.STALE.value),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM daemon_records
                    WHERE status IN (?, ?) AND owner = ?
                    ORDER BY heartbeat_at DESC
                    """,
                    (DaemonStatus.RUNNING.value, DaemonStatus.STALE.value, owner),
                ).fetchall()
            for row in rows:
                stopped_ids.append(row["id"])
                daemon_owner = row["owner"]
                conn.execute(
                    "UPDATE daemon_records SET status = ?, stopped_at = ?, heartbeat_at = ? WHERE id = ?",
                    (DaemonStatus.STOPPED.value, timestamp, timestamp, row["id"]),
                )
                self._expire_active_daemon_leases(
                    conn,
                    daemon_id=row["id"],
                    owner=daemon_owner,
                    timestamp=timestamp,
                    event_type="stop_lease",
                    message="Stopped daemon lease and returned task to queue.",
                    transition_reason="daemon_stopped",
                )
                self._record_daemon_event(
                    conn,
                    daemon_id=row["id"],
                    event_type="stop",
                    message="Daemon record stopped.",
                    metadata={},
                    created_at=timestamp,
                )
        return [self.get_daemon(daemon_id) for daemon_id in stopped_ids]

    def daemon_status(
        self,
        *,
        stale_after_seconds: int = DEFAULT_DAEMON_STALE_AFTER_SECONDS,
    ) -> DaemonStatusResult:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
        self._mark_stale_daemons(cutoff)
        active_daemons = self.list_daemons(include_stopped=False)
        return DaemonStatusResult(
            project_root=self.project_root,
            active_daemons=active_daemons,
            latest_events=self.list_daemon_events(limit=20),
            paused_tasks=self.daemon_paused_tasks(),
            stale_after_seconds=stale_after_seconds,
        )

    def _mark_stale_daemons(self, cutoff: datetime) -> list[str]:
        timestamp = now_iso()
        stale_ids: list[str] = []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM daemon_records
                WHERE status = ? AND heartbeat_at < ?
                ORDER BY heartbeat_at ASC, id ASC
                """,
                (DaemonStatus.RUNNING.value, cutoff.isoformat()),
            ).fetchall()
            for row in rows:
                stale_ids.append(row["id"])
                conn.execute(
                    "UPDATE daemon_records SET status = ? WHERE id = ?",
                    (DaemonStatus.STALE.value, row["id"]),
                )
                self._expire_active_daemon_leases(
                    conn,
                    daemon_id=row["id"],
                    owner=row["owner"],
                    timestamp=timestamp,
                    event_type="stale_lease",
                    message="Stale daemon lease expired and task returned to queue.",
                    transition_reason="daemon_stale",
                )
                self._record_daemon_event(
                    conn,
                    daemon_id=row["id"],
                    event_type="stale",
                    message="Daemon heartbeat exceeded stale timeout.",
                    metadata={"stale_after": cutoff.isoformat()},
                    created_at=timestamp,
                )
        return stale_ids

    def _expire_active_daemon_leases(
        self,
        conn: sqlite3.Connection,
        *,
        daemon_id: str,
        owner: str,
        timestamp: str,
        event_type: str,
        message: str,
        transition_reason: str,
    ) -> list[str]:
        rows = conn.execute(
            """
            SELECT * FROM task_leases
            WHERE owner = ? AND status = ?
            ORDER BY acquired_at ASC, id ASC
            """,
            (owner, TaskLeaseStatus.ACTIVE.value),
        ).fetchall()
        expired_ids: list[str] = []
        for row in rows:
            lease = self._row_to_task_lease(row)
            if self._expire_active_daemon_lease_for_shutdown(
                conn,
                daemon_id=daemon_id,
                owner=owner,
                lease=lease,
                timestamp=timestamp,
                event_type=event_type,
                message=message,
                transition_reason=transition_reason,
            ):
                expired_ids.append(lease.id)
        return expired_ids

    def _expire_active_daemon_lease_for_shutdown(
        self,
        conn: sqlite3.Connection,
        *,
        daemon_id: str,
        owner: str,
        lease: TaskLease,
        timestamp: str,
        event_type: str,
        message: str,
        transition_reason: str,
    ) -> bool:
        task_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (lease.task_id,)).fetchone()
        task = self._row_to_task(task_row) if task_row is not None else None
        attempt: TaskAttempt | None = None
        if lease.attempt_id is not None:
            attempt_row = conn.execute("SELECT * FROM task_attempts WHERE id = ?", (lease.attempt_id,)).fetchone()
            if attempt_row is not None:
                attempt = self._row_to_task_attempt(attempt_row)

        if task is None:
            conn.execute(
                """
                UPDATE task_leases
                SET status = ?, released_at = ?, heartbeat_at = COALESCE(heartbeat_at, ?), metadata_json = ?
                WHERE id = ?
                """,
                (
                    TaskLeaseStatus.EXPIRED.value,
                    timestamp,
                    timestamp,
                    json.dumps(
                        sanitize_for_logging(
                            {**lease.metadata, "decision": transition_reason, "reason": "missing_task"}
                        ),
                        sort_keys=True,
                        default=str,
                    ),
                    lease.id,
                ),
            )
            self._record_daemon_event(
                conn,
                daemon_id=daemon_id,
                event_type=event_type,
                message=message,
                metadata={
                    "lease_id": lease.id,
                    "task_id": lease.task_id,
                    "attempt_id": lease.attempt_id,
                    "lease_status": TaskLeaseStatus.EXPIRED.value,
                    "reason": "missing_task",
                },
                created_at=timestamp,
            )
            return True

        if attempt is not None and attempt.run_id is not None:
            run_row = conn.execute("SELECT * FROM runs WHERE id = ?", (attempt.run_id,)).fetchone()
            run = self._row_to_run(run_row) if run_row is not None else None
            if run is not None and run.status == "completed":
                self._reconcile_dry_run_terminal_state(
                    conn,
                    task=task,
                    attempt=attempt,
                    lease=lease,
                    next_status=TaskStatus.SUCCEEDED,
                    lease_status=TaskLeaseStatus.RELEASED,
                    timestamp=timestamp,
                    actor=owner,
                    reason=f"{transition_reason}_execution_succeeded",
                    failure_code=None,
                    failure_message=None,
                )
                self._record_daemon_event(
                    conn,
                    daemon_id=daemon_id,
                    event_type=event_type,
                    message="Reconciled completed linked-run evidence while expiring daemon-owned leases.",
                    metadata={
                        "lease_id": lease.id,
                        "task_id": task.id,
                        "attempt_id": attempt.id,
                        "run_id": run.id,
                        "run_status": run.status,
                        "lease_status": TaskLeaseStatus.RELEASED.value,
                        "next_status": TaskStatus.SUCCEEDED.value,
                        "reason": "linked_run_completed",
                    },
                    created_at=timestamp,
                )
                return False
            if run is not None and run.status == "failed":
                self._reconcile_dry_run_terminal_state(
                    conn,
                    task=task,
                    attempt=attempt,
                    lease=lease,
                    next_status=TaskStatus.FAILED,
                    lease_status=TaskLeaseStatus.RELEASED,
                    timestamp=timestamp,
                    actor=owner,
                    reason=f"{transition_reason}_execution_failed",
                    failure_code=transition_reason,
                    failure_message="Daemon shutdown reconciled failed linked-run evidence.",
                )
                self._record_daemon_event(
                    conn,
                    daemon_id=daemon_id,
                    event_type=event_type,
                    message="Reconciled failed linked-run evidence while expiring daemon-owned leases.",
                    metadata={
                        "lease_id": lease.id,
                        "task_id": task.id,
                        "attempt_id": attempt.id,
                        "run_id": run.id,
                        "run_status": run.status,
                        "lease_status": TaskLeaseStatus.RELEASED.value,
                        "next_status": TaskStatus.FAILED.value,
                        "reason": "linked_run_failed",
                    },
                    created_at=timestamp,
                )
                return False
            failure_reason = "missing_run" if run is None else "nonterminal_linked_run"
            self._reconcile_dry_run_terminal_state(
                conn,
                task=task,
                attempt=attempt,
                lease=lease,
                next_status=TaskStatus.FAILED,
                lease_status=TaskLeaseStatus.EXPIRED,
                timestamp=timestamp,
                actor=owner,
                reason=transition_reason,
                failure_code=transition_reason,
                failure_message="Daemon shutdown found an active lease with missing or non-terminal linked run.",
            )
            self._record_daemon_event(
                conn,
                daemon_id=daemon_id,
                event_type=event_type,
                message="Failed linked-run task for operator inspection while expiring daemon-owned leases.",
                metadata={
                    "lease_id": lease.id,
                    "task_id": task.id,
                    "attempt_id": attempt.id,
                    "run_id": attempt.run_id,
                    "run_status": run.status if run is not None else None,
                    "lease_status": TaskLeaseStatus.EXPIRED.value,
                    "next_status": TaskStatus.FAILED.value,
                    "reason": failure_reason,
                    "failure_code": transition_reason,
                },
                created_at=timestamp,
            )
            return True

        if task.status not in {TaskStatus.LEASED, TaskStatus.RUNNING}:
            self._expire_inconsistent_active_lease(
                conn,
                daemon_id=daemon_id,
                owner=owner,
                lease=lease,
                task=task,
                attempt=attempt,
                reason=f"task_status_{task.status.value}",
                timestamp=timestamp,
            )
            self._record_daemon_event(
                conn,
                daemon_id=daemon_id,
                event_type=event_type,
                message="Expired inconsistent daemon-owned active lease.",
                metadata={
                    "lease_id": lease.id,
                    "task_id": task.id,
                    "attempt_id": lease.attempt_id,
                    "lease_status": TaskLeaseStatus.EXPIRED.value,
                    "task_status": task.status.value,
                    "reason": f"task_status_{task.status.value}",
                },
                created_at=timestamp,
            )
            return True

        next_status = self._task_requeue_status(task)
        validate_task_transition(task.status, next_status)
        conn.execute(
            """
            UPDATE task_leases
            SET status = ?, released_at = ?, heartbeat_at = COALESCE(heartbeat_at, ?), metadata_json = ?
            WHERE id = ?
            """,
            (
                TaskLeaseStatus.EXPIRED.value,
                timestamp,
                timestamp,
                json.dumps(
                    sanitize_for_logging(
                        {**lease.metadata, "decision": transition_reason, "reason": "expired_before_execution"}
                    ),
                    sort_keys=True,
                    default=str,
                ),
                lease.id,
            ),
        )
        if lease.attempt_id is not None:
            conn.execute(
                """
                UPDATE task_attempts
                SET status = ?, finished_at = ?, failure_code = ?, failure_message = ?
                WHERE id = ? AND run_id IS NULL
                """,
                (
                    TaskStatus.FAILED.value,
                    timestamp,
                    transition_reason,
                    message,
                    lease.attempt_id,
                ),
            )
        if task.status != next_status:
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (next_status.value, timestamp, task.id),
            )
            self._record_task_transition(
                conn,
                task_id=task.id,
                from_status=task.status,
                to_status=next_status,
                reason=transition_reason,
                actor=owner,
                metadata={"lease_id": lease.id, "attempt_id": lease.attempt_id, "daemon_id": daemon_id},
                created_at=timestamp,
            )
        self._record_daemon_event(
            conn,
            daemon_id=daemon_id,
            event_type=event_type,
            message=message,
            metadata={
                "lease_id": lease.id,
                "task_id": task.id,
                "attempt_id": lease.attempt_id,
                "lease_status": TaskLeaseStatus.EXPIRED.value,
                "next_status": next_status.value,
                "reason": "expired_before_execution",
            },
            created_at=timestamp,
        )
        return True

    def get_daemon(self, daemon_id: str) -> DaemonRecord:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM daemon_records WHERE id = ?", (daemon_id,)).fetchone()
        if row is None:
            raise KeyError(f"Daemon not found: {daemon_id}")
        return self._row_to_daemon(row)

    def list_daemons(self, include_stopped: bool = False) -> list[DaemonRecord]:
        with self.connect() as conn:
            if include_stopped:
                rows = conn.execute(
                    "SELECT * FROM daemon_records ORDER BY heartbeat_at DESC, id ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM daemon_records
                    WHERE status IN (?, ?)
                    ORDER BY heartbeat_at DESC, id ASC
                    """,
                    (DaemonStatus.RUNNING.value, DaemonStatus.STALE.value),
                ).fetchall()
        return [self._row_to_daemon(row) for row in rows]

    def record_daemon_event(
        self,
        daemon_id: str,
        event_type: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> DaemonEvent:
        self.get_daemon(daemon_id)
        timestamp = now_iso()
        with self.connect() as conn:
            event_id = self._record_daemon_event(
                conn,
                daemon_id=daemon_id,
                event_type=event_type,
                message=message,
                metadata=metadata or {},
                created_at=timestamp,
            )
        return self.get_daemon_event(event_id)

    def get_daemon_event(self, event_id: str) -> DaemonEvent:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM daemon_events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            raise KeyError(f"Daemon event not found: {event_id}")
        return self._row_to_daemon_event(row)

    def list_daemon_events(self, daemon_id: str | None = None, limit: int = 50) -> list[DaemonEvent]:
        with self.connect() as conn:
            if daemon_id is None:
                rows = conn.execute(
                    """
                    SELECT * FROM daemon_events
                    ORDER BY created_at DESC, id ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                self.get_daemon(daemon_id)
                rows = conn.execute(
                    """
                    SELECT * FROM daemon_events
                    WHERE daemon_id = ?
                    ORDER BY created_at DESC, id ASC
                    LIMIT ?
                    """,
                    (daemon_id, limit),
                ).fetchall()
        return [self._row_to_daemon_event(row) for row in rows]

    def _record_daemon_event(
        self,
        conn: sqlite3.Connection,
        *,
        daemon_id: str,
        event_type: str,
        message: str,
        metadata: dict[str, Any],
        created_at: str,
    ) -> str:
        event_id = f"daemon_evt_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO daemon_events (
              id, daemon_id, event_type, message, created_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                daemon_id,
                event_type,
                message,
                created_at,
                json.dumps(sanitize_for_logging(metadata), sort_keys=True, default=str),
            ),
        )
        return event_id

    def select_next_task_legacy(self) -> TaskRecord | None:
        candidates = [
            task
            for task in self.list_tasks()
            if task.status in {TaskStatus.READY, TaskStatus.BLOCKED}
        ]
        for task in candidates:
            if not self._task_dependencies_completed(task):
                continue
            if task.required_approvals:
                continue
            if task.status == TaskStatus.BLOCKED:
                task = self.update_task_status(task.id, TaskStatus.READY)
            return self.update_task_status(task.id, TaskStatus.RUNNING)
        return None

    def list_task_attempts(self, task_id: str | None = None) -> list[TaskAttempt]:
        with self.connect() as conn:
            if task_id is None:
                rows = conn.execute(
                    "SELECT * FROM task_attempts ORDER BY created_at ASC, id ASC"
                ).fetchall()
            else:
                self._require_task(conn, task_id)
                rows = conn.execute(
                    """
                    SELECT * FROM task_attempts
                    WHERE task_id = ?
                    ORDER BY attempt_number ASC, created_at ASC, id ASC
                    """,
                    (task_id,),
                ).fetchall()
        return [self._row_to_task_attempt(row) for row in rows]

    def list_task_leases(self, task_id: str | None = None) -> list[TaskLease]:
        with self.connect() as conn:
            if task_id is None:
                rows = conn.execute(
                    "SELECT * FROM task_leases ORDER BY acquired_at ASC, id ASC"
                ).fetchall()
            else:
                self._require_task(conn, task_id)
                rows = conn.execute(
                    """
                    SELECT * FROM task_leases
                    WHERE task_id = ?
                    ORDER BY acquired_at ASC, id ASC
                    """,
                    (task_id,),
                ).fetchall()
        return [self._row_to_task_lease(row) for row in rows]

    def _task_has_active_lease(self, conn: sqlite3.Connection, task_id: str) -> bool:
        row = conn.execute(
            "SELECT id FROM task_leases WHERE task_id = ? AND status = ? LIMIT 1",
            (task_id, TaskLeaseStatus.ACTIVE.value),
        ).fetchone()
        return row is not None

    def _next_attempt_number(self, conn: sqlite3.Connection, task_id: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(attempt_number), 0) AS max_attempt FROM task_attempts WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return int(row["max_attempt"]) + 1

    def _task_attempt_count(self, conn: sqlite3.Connection, task_id: str) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS attempt_count FROM task_attempts WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return int(row["attempt_count"])

    def _task_dependencies_completed(self, task: TaskRecord) -> bool:
        for dependency_id in task.depends_on:
            try:
                dependency = self.get_task(dependency_id)
            except KeyError:
                return False
            if dependency.status != TaskStatus.SUCCEEDED:
                return False
        return True

    def _dependency_ids_completed(self, conn: sqlite3.Connection, dependency_ids: list[str]) -> bool:
        for dependency_id in dependency_ids:
            row = conn.execute("SELECT status FROM tasks WHERE id = ?", (dependency_id,)).fetchone()
            if row is None or normalize_task_status(row["status"]) != TaskStatus.SUCCEEDED:
                return False
        return True

    def create_task_dependency(
        self,
        upstream_task_id: str,
        downstream_task_id: str,
        dependency_type: TaskDependencyType = TaskDependencyType.SUCCESS,
        required_artifact_kind: str | None = None,
    ) -> TaskDependency:
        timestamp = now_iso()
        with self.connect() as conn:
            self._require_task(conn, upstream_task_id)
            self._require_task(conn, downstream_task_id)
            return self._create_task_dependency(
                conn,
                upstream_task_id=upstream_task_id,
                downstream_task_id=downstream_task_id,
                dependency_type=dependency_type,
                required_artifact_kind=required_artifact_kind,
                created_at=timestamp,
            )

    def list_task_dependencies(self, task_id: str | None = None) -> list[TaskDependency]:
        with self.connect() as conn:
            if task_id is None:
                rows = conn.execute(
                    "SELECT * FROM task_dependencies ORDER BY created_at ASC, id ASC"
                ).fetchall()
            else:
                self._require_task(conn, task_id)
                rows = conn.execute(
                    """
                    SELECT * FROM task_dependencies
                    WHERE upstream_task_id = ? OR downstream_task_id = ?
                    ORDER BY created_at ASC, id ASC
                    """,
                    (task_id, task_id),
                ).fetchall()
        return [self._row_to_task_dependency(row) for row in rows]

    def build_task_graph(self, objective_id: str | None = None) -> dict[str, Any]:
        if objective_id is not None:
            self.get_objective(objective_id)
        tasks = self.list_tasks(objective_id=objective_id)
        task_ids = {task.id for task in tasks}
        objectives = self.list_objectives()
        if objective_id is not None:
            objectives = [objective for objective in objectives if objective.id == objective_id]
        dependencies = [
            dependency
            for dependency in self.list_task_dependencies()
            if dependency.upstream_task_id in task_ids or dependency.downstream_task_id in task_ids
        ]
        blocked_reasons = {task.id: self._blocked_reasons(task) for task in tasks}
        return {
            "objectives": [objective.model_dump(mode="json") for objective in objectives],
            "tasks": [task.model_dump(mode="json") for task in tasks],
            "dependencies": [dependency.model_dump(mode="json") for dependency in dependencies],
            "blocked_reasons": blocked_reasons,
        }

    def _blocked_reasons(self, task: TaskRecord) -> list[dict[str, Any]]:
        reasons: list[dict[str, Any]] = []
        for dependency_id in task.depends_on:
            try:
                dependency = self.get_task(dependency_id)
            except KeyError:
                reasons.append({"kind": "missing_dependency", "task_id": dependency_id})
                continue
            if dependency.status != TaskStatus.SUCCEEDED:
                reasons.append(
                    {
                        "kind": "unsatisfied_dependency",
                        "task_id": dependency_id,
                        "status": dependency.status.value,
                    }
                )
        if task.required_approvals:
            reasons.append(
                {
                    "kind": "unresolved_required_approvals",
                    "required_approvals": task.required_approvals,
                    "approval_state": task.approval_state,
                }
            )
        return reasons

    def _create_task_dependency(
        self,
        conn: sqlite3.Connection,
        *,
        upstream_task_id: str,
        downstream_task_id: str,
        dependency_type: TaskDependencyType,
        required_artifact_kind: str | None,
        created_at: str,
    ) -> TaskDependency:
        if upstream_task_id == downstream_task_id:
            raise ValueError("Task cannot depend on itself")
        if self._dependency_path_exists(conn, downstream_task_id, upstream_task_id):
            raise ValueError(
                f"Task dependency cycle detected: {upstream_task_id} -> {downstream_task_id}"
            )
        dependency_id = f"task_dep_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO task_dependencies (
              id, upstream_task_id, downstream_task_id, dependency_type,
              required_artifact_kind, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                dependency_id,
                upstream_task_id,
                downstream_task_id,
                dependency_type.value,
                required_artifact_kind,
                created_at,
            ),
        )
        return TaskDependency(
            id=dependency_id,
            upstream_task_id=upstream_task_id,
            downstream_task_id=downstream_task_id,
            dependency_type=dependency_type,
            required_artifact_kind=required_artifact_kind,
            created_at=parse_dt(created_at),
        )

    def _dependency_path_exists(
        self,
        conn: sqlite3.Connection,
        start_task_id: str,
        target_task_id: str,
    ) -> bool:
        seen: set[str] = set()
        stack = [start_task_id]
        while stack:
            current = stack.pop()
            if current == target_task_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            rows = conn.execute(
                "SELECT downstream_task_id FROM task_dependencies WHERE upstream_task_id = ?",
                (current,),
            ).fetchall()
            stack.extend(row["downstream_task_id"] for row in rows)
        return False

    def _require_task(self, conn: sqlite3.Connection, task_id: str) -> None:
        row = conn.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"Task not found: {task_id}")

    def _require_objective(self, conn: sqlite3.Connection, objective_id: str) -> None:
        row = conn.execute("SELECT id FROM objectives WHERE id = ?", (objective_id,)).fetchone()
        if row is None:
            raise KeyError(f"Objective not found: {objective_id}")

    def _require_run(self, conn: sqlite3.Connection, run_id: str) -> None:
        row = conn.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Run not found: {run_id}")

    def _require_session(self, conn: sqlite3.Connection, session_id: str) -> None:
        row = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(f"Session not found: {session_id}")

    def list_task_transitions(self, task_id: str) -> list[TaskTransitionRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_transitions
                WHERE task_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (task_id,),
            ).fetchall()
        return [self._row_to_task_transition(row) for row in rows]

    def _record_task_transition(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: str,
        from_status: TaskStatus | None,
        to_status: TaskStatus,
        reason: str,
        actor: str,
        metadata: dict[str, Any],
        created_at: str,
    ) -> None:
        transition_id = f"task_transition_{uuid.uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO task_transitions (
              id, task_id, from_status, to_status, reason, actor, created_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transition_id,
                task_id,
                from_status.value if from_status is not None else None,
                to_status.value,
                reason,
                actor,
                created_at,
                json.dumps(sanitize_for_logging(metadata), sort_keys=True, default=str),
            ),
        )

    def append_event(
        self,
        run_id: str,
        level: str,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> EventRecord:
        return self._append_event_record(
            run_id=run_id,
            level=level,
            event_type=event_type,
            message=message,
            payload=payload,
            session_id=session_id,
            visibility=EventVisibility.USER_VISIBLE,
            redaction_state=RedactionState.REDACTED,
        )

    def append_store_event(
        self,
        stream_type: EventStreamType | str,
        stream_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        visibility: EventVisibility | str = EventVisibility.USER_VISIBLE,
        redaction_state: RedactionState | str = RedactionState.REDACTED,
        session_id: str | None = None,
        message_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        artifact_id: str | None = None,
        actor: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        artifact_refs: list[str] | None = None,
        created_at: str | None = None,
    ) -> StoredEventRecord:
        stream_value = EventStreamType(stream_type.value if isinstance(stream_type, EventStreamType) else stream_type)
        visibility_value = EventVisibility(visibility.value if isinstance(visibility, EventVisibility) else visibility)
        redaction_value = RedactionState(redaction_state.value if isinstance(redaction_state, RedactionState) else redaction_state)
        event_id = f"evt2_{uuid.uuid4().hex[:12]}"
        timestamp = created_at or now_iso()
        sanitized_payload = sanitize_for_logging(payload or {})
        sanitized_actor = sanitize_for_logging(actor or {})
        sanitized_artifact_refs = sanitize_for_logging(artifact_refs or [])
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            seq = self._next_store_event_seq(conn, stream_value.value, stream_id)
            conn.execute(
                """
                INSERT INTO event_store (
                  id, stream_type, stream_id, seq, kind, visibility, redaction_state,
                  session_id, message_id, run_id, task_id, artifact_id, actor_json,
                  correlation_id, causation_id, payload_json, artifact_refs_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    stream_value.value,
                    stream_id,
                    seq,
                    kind,
                    visibility_value.value,
                    redaction_value.value,
                    session_id,
                    message_id,
                    run_id,
                    task_id,
                    artifact_id,
                    json.dumps(sanitized_actor, sort_keys=True, default=str),
                    correlation_id,
                    causation_id,
                    json.dumps(sanitized_payload, sort_keys=True, default=str),
                    json.dumps(sanitized_artifact_refs, sort_keys=True, default=str),
                    timestamp,
                ),
            )
        event = StoredEventRecord(
            id=event_id,
            stream_type=stream_value,
            stream_id=stream_id,
            seq=seq,
            kind=kind,
            visibility=visibility_value,
            redaction_state=redaction_value,
            session_id=session_id,
            message_id=message_id,
            run_id=run_id,
            task_id=task_id,
            artifact_id=artifact_id,
            actor=sanitized_actor,
            correlation_id=correlation_id,
            causation_id=causation_id,
            payload=sanitized_payload,
            artifact_refs=sanitized_artifact_refs,
            created_at=parse_dt(timestamp),
        )
        try:
            from harness.event_broker import get_event_broker

            get_event_broker(self.project_root).publish(event)
        except Exception:
            logger.exception("Failed to publish persisted store event: %s", event.id)
        return event

    def _next_store_event_seq(self, conn: sqlite3.Connection, stream_type: str, stream_id: str) -> int:
        row = conn.execute(
            """
            SELECT COALESCE(MAX(seq), 0) AS max_seq
            FROM event_store
            WHERE stream_type = ? AND stream_id = ?
            """,
            (stream_type, stream_id),
        ).fetchone()
        return int(row["max_seq"] or 0) + 1

    def list_store_events(
        self,
        stream_type: EventStreamType | str,
        stream_id: str,
        *,
        after_seq: int | None = None,
        limit: int | None = None,
    ) -> list[StoredEventRecord]:
        stream_value = EventStreamType(stream_type.value if isinstance(stream_type, EventStreamType) else stream_type)
        params: list[Any] = [stream_value.value, stream_id]
        where = "stream_type = ? AND stream_id = ?"
        if after_seq is not None:
            where += " AND seq > ?"
            params.append(after_seq)
        sql = f"SELECT * FROM event_store WHERE {where} ORDER BY seq ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_stored_event(row) for row in rows]

    def list_session_store_events(
        self, session_id: str, *, after_seq: int | None = None, limit: int | None = None
    ) -> list[StoredEventRecord]:
        params: list[Any] = [session_id]
        where = "session_id = ?"
        if after_seq is not None:
            where += " AND seq > ?"
            params.append(after_seq)
        sql = f"SELECT * FROM event_store WHERE {where} ORDER BY created_at ASC, id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_stored_event(row) for row in rows]

    def append_run_event(
        self,
        run_id: str,
        event_type: RunEventType | str,
        payload: dict[str, Any] | None = None,
        *,
        message: str = "",
        visibility: EventVisibility | str = EventVisibility.USER_VISIBLE,
        redaction_state: RedactionState | str = RedactionState.REDACTED,
        level: str = "info",
        trace_id: str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
    ) -> EventRecord:
        event_value = event_type.value if isinstance(event_type, RunEventType) else str(event_type)
        visibility_value = EventVisibility(visibility.value if isinstance(visibility, EventVisibility) else visibility)
        redaction_value = RedactionState(redaction_state.value if isinstance(redaction_state, RedactionState) else redaction_state)
        return self._append_event_record(
            run_id=run_id,
            level=level,
            event_type=event_value,
            message=message or event_value,
            payload=payload,
            session_id=session_id,
            visibility=visibility_value,
            redaction_state=redaction_value,
            trace_id=trace_id,
            task_id=task_id,
        )

    def append_token_usage_event(
        self,
        run_id: str,
        usage: TokenUsageSnapshot,
        *,
        trace_id: str | None = None,
        task_id: str | None = None,
    ) -> EventRecord:
        return self.append_run_event(
            run_id,
            RunEventType.TOKEN_USAGE_UPDATED,
            usage.model_dump(mode="json", exclude_none=True),
            message="Token usage updated.",
            redaction_state=RedactionState.NOT_REQUIRED,
            trace_id=trace_id,
            task_id=task_id,
        )

    def _append_event_record(
        self,
        *,
        run_id: str,
        level: str,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
        session_id: str | None = None,
        visibility: EventVisibility = EventVisibility.USER_VISIBLE,
        redaction_state: RedactionState = RedactionState.REDACTED,
        trace_id: str | None = None,
        task_id: str | None = None,
    ) -> EventRecord:
        event_id = f"evt_{uuid.uuid4().hex[:12]}"
        timestamp = now_iso()
        payload = sanitize_for_logging(payload or {})
        payload_json = json.dumps(payload, sort_keys=True, default=str)
        run = self.get_run(run_id)
        session_id = session_id if session_id is not None else run.session_id
        task_id = task_id if task_id is not None else run.task_id
        seq: int
        with self.connect() as conn:
            seq = self._next_event_seq(conn, run_id)
            conn.execute(
                """
                INSERT INTO events (
                  id, run_id, created_at, level, event_type, message, payload_json, session_id,
                  schema_version, seq, task_id, trace_id, visibility, redaction_state
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    run_id,
                    timestamp,
                    level,
                    event_type,
                    str(sanitize_for_logging(message)),
                    payload_json,
                    session_id,
                    "harness.event/v1",
                    seq,
                    task_id,
                    trace_id,
                    visibility.value,
                    redaction_state.value,
                ),
            )
        record = EventRecord(
            id=event_id,
            run_id=run_id,
            created_at=parse_dt(timestamp),
            level=level,
            event_type=event_type,
            message=str(sanitize_for_logging(message)),
            session_id=session_id,
            task_id=task_id,
            trace_id=trace_id,
            seq=seq,
            visibility=visibility,
            redaction_state=redaction_state,
            payload=payload,
        )
        self.append_store_event(
            EventStreamType.RUN,
            run_id,
            event_type,
            {
                "level": level,
                "message": str(sanitize_for_logging(message)),
                "payload": payload,
                "legacy_event_id": event_id,
            },
            visibility=visibility,
            redaction_state=redaction_state,
            session_id=session_id,
            run_id=run_id,
            task_id=task_id,
            correlation_id=trace_id,
            created_at=timestamp,
        )
        append_jsonl(self.runs_dir / run_id / "events.jsonl", record.jsonl_envelope())
        return record

    def _next_event_seq(self, conn: sqlite3.Connection, run_id: str) -> int:
        row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS max_seq FROM events WHERE run_id = ?", (run_id,)).fetchone()
        return int(row["max_seq"] or 0) + 1

    def list_events(self, run_id: str) -> list[EventRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY COALESCE(seq, 0) ASC, created_at ASC", (run_id,)
            ).fetchall()
        return [
            EventRecord(
                schema_version=(row["schema_version"] or "harness.event/v1")
                if "schema_version" in row.keys()
                else "harness.event/v1",
                id=row["id"],
                run_id=row["run_id"],
                created_at=parse_dt(row["created_at"]),
                level=row["level"],
                event_type=row["event_type"],
                message=row["message"],
                session_id=row["session_id"] if "session_id" in row.keys() else None,
                task_id=row["task_id"] if "task_id" in row.keys() else None,
                trace_id=row["trace_id"] if "trace_id" in row.keys() else None,
                seq=row["seq"] if "seq" in row.keys() else None,
                visibility=EventVisibility(row["visibility"] or EventVisibility.USER_VISIBLE.value)
                if "visibility" in row.keys()
                else EventVisibility.USER_VISIBLE,
                redaction_state=RedactionState(row["redaction_state"] or RedactionState.REDACTED.value)
                if "redaction_state" in row.keys()
                else RedactionState.REDACTED,
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        ]

    def register_artifact(
        self,
        run_id: str,
        kind: str,
        path: Path,
        metadata: dict[str, Any] | None = None,
        producer: str | None = None,
        redaction_state: str = "unknown",
        session_id: str | None = None,
    ) -> ArtifactRecord:
        from harness.integrity import artifact_provenance_from_metadata, with_artifact_provenance_metadata

        run = self.get_run(run_id)
        session_id = session_id if session_id is not None else run.session_id
        if not path.exists():
            raise FileNotFoundError(f"Artifact path not found: {path}")
        path, redaction_state, metadata = self._prepare_artifact_registration(
            run_id=run_id,
            path=path,
            metadata=metadata or {},
            redaction_state=redaction_state,
        )
        artifact_id = f"art_{uuid.uuid4().hex[:12]}"
        timestamp = now_iso()
        sha256, size_bytes = self._artifact_file_evidence(path)
        if kind in MUTABLE_RUN_ARTIFACT_KINDS and "mutable_run_artifact" not in metadata:
            metadata["mutable_run_artifact"] = True
        metadata = with_artifact_provenance_metadata(
            artifact_id=artifact_id,
            run_id=run_id,
            kind=kind,
            producer=producer,
            sha256=sha256,
            redaction_state=redaction_state,
            metadata=metadata,
            created_at=timestamp,
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO artifacts (
                  id, run_id, kind, path, created_at, schema_version, sha256,
                  size_bytes, producer, redaction_state, evidence_status, metadata_json, session_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    run_id,
                    kind,
                    str(path),
                    timestamp,
                    "harness.artifact/v1",
                    sha256,
                    size_bytes,
                    producer,
                    redaction_state,
                    "verified",
                    json.dumps(metadata, sort_keys=True, default=str),
                    session_id,
                ),
            )
        record = ArtifactRecord(
            id=artifact_id,
            run_id=run_id,
            session_id=session_id,
            kind=kind,
            path=path,
            created_at=parse_dt(timestamp),
            sha256=sha256,
            size_bytes=size_bytes,
            producer=producer,
            redaction_state=redaction_state,
            evidence_status="verified",
            metadata=metadata,
            provenance=artifact_provenance_from_metadata(
                artifact_id=artifact_id,
                run_id=run_id,
                kind=kind,
                producer=producer,
                sha256=sha256,
                redaction_state=redaction_state,
                metadata=metadata,
                created_at=parse_dt(timestamp),
            ),
        )
        if session_id is not None:
            self.append_store_event(
                EventStreamType.SESSION,
                session_id,
                RunEventType.ARTIFACT_REGISTERED.value,
                {
                    "artifact_id": artifact_id,
                    "kind": kind,
                    "path": str(path),
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                    "producer": producer,
                    "redaction_state": redaction_state,
                },
                session_id=session_id,
                run_id=run_id,
                artifact_id=artifact_id,
                artifact_refs=[artifact_id],
                redaction_state=RedactionState.NOT_REQUIRED
                if redaction_state == "not_required"
                else RedactionState.REDACTED,
                created_at=timestamp,
            )
        self.write_run_manifest(run_id)
        return record

    def refresh_artifact_evidence(self, artifact_id: str) -> ArtifactRecord:
        from harness.integrity import artifact_provenance_from_metadata, with_artifact_provenance_metadata

        artifact = self.get_artifact(artifact_id)
        metadata = dict(artifact.metadata)
        if not artifact.path.exists():
            with self.connect() as conn:
                conn.execute(
                    "UPDATE artifacts SET evidence_status = ? WHERE id = ?",
                    ("missing", artifact.id),
                )
            return self.get_artifact(artifact.id)
        sha256, size_bytes = self._artifact_file_evidence(artifact.path)
        metadata.pop("provenance", None)
        metadata = with_artifact_provenance_metadata(
            artifact_id=artifact.id,
            run_id=artifact.run_id,
            kind=artifact.kind,
            producer=artifact.producer,
            sha256=sha256,
            redaction_state=artifact.redaction_state,
            metadata=metadata,
            created_at=artifact.created_at,
        )
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE artifacts
                SET sha256 = ?, size_bytes = ?, evidence_status = ?, metadata_json = ?
                WHERE id = ?
                """,
                (sha256, size_bytes, "verified", json.dumps(metadata, sort_keys=True, default=str), artifact.id),
            )
        refreshed = self.get_artifact(artifact.id)
        return refreshed.model_copy(
            update={
                "provenance": artifact_provenance_from_metadata(
                    artifact_id=refreshed.id,
                    run_id=refreshed.run_id,
                    kind=refreshed.kind,
                    producer=refreshed.producer,
                    sha256=refreshed.sha256,
                    redaction_state=refreshed.redaction_state,
                    metadata=refreshed.metadata,
                    created_at=refreshed.created_at,
                )
            }
        )

    def refresh_run_artifact_evidence(self, run_id: str, *, include_manifest: bool = True) -> list[ArtifactRecord]:
        refreshed: list[ArtifactRecord] = []
        for artifact in self.list_artifacts(run_id):
            if artifact.kind == "manifest" and not include_manifest:
                continue
            if artifact.kind not in MUTABLE_RUN_ARTIFACT_KINDS and not artifact.metadata.get("mutable_run_artifact"):
                continue
            refreshed.append(self.refresh_artifact_evidence(artifact.id))
        return refreshed

    def _prepare_artifact_registration(
        self,
        *,
        run_id: str,
        path: Path,
        metadata: dict[str, Any],
        redaction_state: str,
    ) -> tuple[Path, str, dict[str, Any]]:
        if is_secret_path(path):
            raise SecretBlockedError(f"Blocked secret-like artifact path: {path.name}")
        metadata = dict(sanitize_for_logging(metadata))
        if redaction_state != "unknown":
            return path, redaction_state, metadata
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path, "not_required", metadata
        findings = scan_text_for_secrets(text)
        if not findings:
            return path, "not_required", metadata
        source_sha256, source_size = self._artifact_file_evidence(path)
        redacted_path = _redacted_artifact_path(path)
        redacted_path.write_text(redact_secret_text(text), encoding="utf-8")
        metadata["redaction_lineage"] = sanitize_for_logging(
            {
                "source_path": str(path),
                "source_sha256": source_sha256,
                "source_size_bytes": source_size,
                "findings": [finding.to_dict() for finding in findings],
                "derived_path": str(redacted_path),
            }
        )
        return redacted_path, "redacted", metadata

    def get_artifact(self, artifact_id: str) -> ArtifactRecord:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if row is None:
            raise KeyError(f"Artifact not found: {artifact_id}")
        return self._row_to_artifact(row)

    def list_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        self.get_run(run_id)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at ASC", (run_id,)
            ).fetchall()
        return [self._row_to_artifact(row) for row in rows]

    def verify_artifact(self, artifact_id: str) -> ArtifactRecord:
        artifact = self.get_artifact(artifact_id)
        status = self._artifact_evidence_status(artifact)
        return artifact.model_copy(update={"evidence_status": status})

    def verify_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        return [self.verify_artifact(artifact.id) for artifact in self.list_artifacts(run_id)]

    def _artifact_file_evidence(self, path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size_bytes = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size_bytes += len(chunk)
                digest.update(chunk)
        return digest.hexdigest(), size_bytes

    def _artifact_evidence_status(self, artifact: ArtifactRecord) -> str:
        if not artifact.path.exists():
            return "missing"
        if artifact.sha256 is None or artifact.size_bytes is None:
            return "unknown"
        sha256, size_bytes = self._artifact_file_evidence(artifact.path)
        if sha256 == artifact.sha256 and size_bytes == artifact.size_bytes:
            return "verified"
        return "mismatch"

    def persist_backend_snapshot(self, run_id: str, backend: BackendConfig) -> None:
        snapshot_id = f"backend_{uuid.uuid4().hex[:12]}"
        timestamp = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO backend_snapshots (
                  id, run_id, backend_name, backend_kind, metadata_json,
                  capabilities_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    run_id,
                    backend.name,
                    backend.kind.value,
                    backend.metadata.model_dump_json(),
                    backend.capabilities.model_dump_json(),
                    timestamp,
                ),
            )

    def generate_final_report(self, run_id: str) -> Path:
        run = self.get_run(run_id)
        artifacts = self.list_artifacts(run_id)
        events = self.list_events(run_id)
        report_path = self.runs_dir / run_id / "final_report.md"
        lines = [
            f"# Run {run.id}",
            "",
            f"- Status: {run.status}",
            f"- Goal: {run.goal or ''}",
            f"- Task type: {run.task_type or ''}",
            f"- Project root: {run.project_root}",
            f"- Created: {run.created_at.isoformat()}",
            f"- Updated: {run.updated_at.isoformat()}",
            f"- Backend: {run.backend_name or 'none'}",
            f"- Backend kind: {run.backend_kind.value if run.backend_kind else 'none'}",
            f"- Billing mode: {run.billing_mode.value if run.billing_mode else 'none'}",
            f"- Execution location: {run.execution_location.value if run.execution_location else 'none'}",
            f"- Data boundary: {run.data_boundary.value if run.data_boundary else 'none'}",
            f"- Allow network: {run.allow_network if run.allow_network is not None else 'none'}",
            "",
            "## Artifacts",
            "",
        ]
        if artifacts:
            lines.extend([f"- {artifact.kind}: {artifact.path}" for artifact in artifacts])
        else:
            lines.append("- none")
        lines.extend(["", "## Events", "", f"- Event count: {len(events)}", ""])
        report_path.write_text("\n".join(lines), encoding="utf-8")
        self.refresh_run_artifact_evidence(run_id, include_manifest=False)
        self.write_run_manifest(run_id)
        return report_path

    def write_run_manifest(self, run_id: str) -> Path:
        self.refresh_run_artifact_evidence(run_id, include_manifest=False)
        manifest = self.build_run_manifest(run_id)
        path = self.runs_dir / run_id / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for artifact in self.list_artifacts(run_id):
            if artifact.kind == "manifest" and artifact.path == path:
                self.refresh_artifact_evidence(artifact.id)
                break
        return path

    def build_context_provenance(
        self,
        *,
        run_id: str | None = None,
        task: TaskRecord | None = None,
    ) -> list[ContextProvenanceRecord]:
        records: list[ContextProvenanceRecord] = []
        run: RunRecord | None = None
        if run_id is not None:
            try:
                run = self.get_run(run_id)
            except KeyError:
                run = None
        if task is None and run is not None and run.task_id is not None:
            try:
                task = self.get_task(run.task_id)
            except KeyError:
                task = None
        if run is not None and run.goal:
            records.append(
                ContextProvenanceRecord(
                    id=_provenance_id("run_goal", run.id),
                    source_kind=ContextSourceKind.RUN_GOAL,
                    trust_level=ContextTrustLevel.TRUSTED_OPERATOR,
                    label=str(sanitize_for_logging("Run goal")),
                    source_id=run.id,
                    sha256=hashlib.sha256(str(sanitize_for_logging(run.goal)).encode("utf-8")).hexdigest(),
                    redaction_state="not_required",
                    lineage={"authority": "operator_request", "permission_granting": False},
                )
            )
        if task is not None:
            task_text = f"{task.title}\n{task.description}".strip()
            records.append(
                ContextProvenanceRecord(
                    id=_provenance_id("task_metadata", task.id),
                    source_kind=ContextSourceKind.TASK_METADATA,
                    trust_level=ContextTrustLevel.TRUSTED_OPERATOR,
                    label=str(sanitize_for_logging(f"Task metadata for {task.id}")),
                    source_id=task.id,
                    sha256=hashlib.sha256(str(sanitize_for_logging(task_text)).encode("utf-8")).hexdigest()
                    if task_text
                    else None,
                    redaction_state="not_required",
                    lineage={"authority": "task_record", "permission_granting": False},
                )
            )
        if run is not None:
            for artifact in self.list_artifacts(run.id):
                source_kind, trust_level, warnings = _artifact_context_classification(artifact.kind)
                records.append(
                    ContextProvenanceRecord(
                        id=_provenance_id("artifact", artifact.id),
                        source_kind=source_kind,
                        trust_level=trust_level,
                        label=str(sanitize_for_logging(f"Artifact {artifact.kind}")),
                        source_id=run.id,
                        artifact_id=artifact.id,
                        path=artifact.path,
                        sha256=artifact.sha256,
                        redaction_state=artifact.redaction_state,
                        lineage={
                            "kind": artifact.kind,
                            "producer": artifact.producer,
                            "evidence_status": self._artifact_evidence_status(artifact),
                            "permission_granting": False,
                        },
                        warnings=warnings,
                    )
                )
        for memory in self.list_memory_records()[:5]:
            records.append(
                ContextProvenanceRecord(
                    id=_provenance_id("memory", memory.id),
                    source_kind=ContextSourceKind.MEMORY_RECORD,
                    trust_level=ContextTrustLevel.MEMORY,
                    label=str(sanitize_for_logging(f"Memory {memory.scope_type.value}:{memory.scope_id}")),
                    source_id=memory.source_id,
                    memory_id=memory.id,
                    sha256=memory.sha256,
                    redaction_state=memory.redaction_state.value,
                    lineage={
                        **sanitize_for_logging(memory.lineage),
                        "permission_granting": False,
                        "policy_authority": False,
                        "approval_authority": False,
                    },
                    warnings=["memory_not_authority"],
                )
            )
        return records

    def build_run_manifest(self, run_id: str) -> RunManifest:
        run = self.get_run(run_id)
        backend_descriptor = self._latest_backend_descriptor(run_id)
        effective_policy = resolve_run_effective_policy(run, backend_descriptor)
        artifacts = [
            ManifestArtifact(
                id=artifact.id,
                run_id=artifact.run_id,
                kind=artifact.kind,
                path=artifact.path,
                created_at=artifact.created_at,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                producer=artifact.producer,
                redaction_state=artifact.redaction_state,
                evidence_status=self._artifact_evidence_status(artifact),
                metadata=artifact.metadata,
                provenance=artifact.provenance,
            )
            for artifact in self.list_artifacts(run_id)
        ]
        context_provenance = self.build_context_provenance(run_id=run_id)
        autonomy_event = next(
            (event for event in reversed(self.list_events(run_id)) if event.event_type == "autonomy_decision"),
            None,
        )
        autonomy_payload = autonomy_event.payload if autonomy_event is not None else {}
        adapter_descriptor = _execution_adapter_descriptor_for_run_record(self, run)
        sandbox_profile_id = (
            adapter_descriptor.sandbox_profile_id
            if adapter_descriptor is not None
            else _legacy_sandbox_profile_id_for_task_type(run.task_type)
        )
        return RunManifest(
            run_id=run.id,
            goal=run.goal,
            task_type=run.task_type,
            run_mode=run_mode_for_task_type(run.task_type),
            status=run.status,
            project_root=run.project_root,
            created_at=run.created_at,
            updated_at=run.updated_at,
            approval_id=run.approval_id,
            backend_descriptor=backend_descriptor,
            artifacts=artifacts,
            task_id=run.task_id,
            objective_id=run.objective_id,
            effective_policy=effective_policy,
            effective_policy_sha256=effective_policy_sha256(effective_policy),
            backend_descriptor_sha256=backend_descriptor_sha256(backend_descriptor),
            sandbox_profile=sandbox_profile_dict(sandbox_profile_id),
            delegate_budget=_delegate_budget_for_run_descriptor(adapter_descriptor),
            autonomy_decision_id=autonomy_payload.get("autonomy_decision_id"),
            autonomous_approval_id=autonomy_payload.get("autonomous_approval_id"),
            autonomous_outcome_id=autonomy_payload.get("autonomous_outcome_id"),
            context_provenance=context_provenance,
            untrusted_context_warnings=_context_warnings(context_provenance),
        )

    def build_run_evidence_snapshot(self, run_id: str) -> dict[str, Any]:
        from harness.integrity import adapter_descriptor_evidence

        manifest = self.build_run_manifest(run_id).model_dump(mode="json")
        return sanitize_for_logging(
            {
                "run_id": manifest["run_id"],
                "run_status": {"status": manifest["status"]},
                "effective_policy_sha256": manifest.get("effective_policy_sha256"),
                "backend_descriptor_sha256": manifest.get("backend_descriptor_sha256"),
                "sandbox_profile": manifest.get("sandbox_profile"),
                "adapter_descriptors": adapter_descriptor_evidence(),
                "approvals": {
                    "approval_id": manifest.get("approval_id"),
                    "required_approvals": (
                        manifest.get("effective_policy", {}).get("required_approvals", [])
                        if manifest.get("effective_policy")
                        else []
                    ),
                },
                "task_objective_linkage": {
                    "task_id": manifest.get("task_id"),
                    "objective_id": manifest.get("objective_id"),
                    "trace_id": manifest.get("trace_id"),
                },
                "artifacts": [
                    {
                        "id": artifact.get("id"),
                        "kind": artifact.get("kind"),
                        "sha256": artifact.get("sha256"),
                        "size_bytes": artifact.get("size_bytes"),
                        "producer": artifact.get("producer"),
                        "redaction_state": artifact.get("redaction_state"),
                        "evidence_status": artifact.get("evidence_status"),
                        "metadata": artifact.get("metadata", {}),
                        "provenance": artifact.get("provenance"),
                    }
                    for artifact in sorted(
                        manifest.get("artifacts", []),
                        key=lambda item: (item.get("kind") or "", item.get("id") or ""),
                    )
                ],
                "test_result_evidence": {
                    "validation_results": manifest.get("validation_results"),
                    "test_artifacts": [
                        {
                            "id": artifact.get("id"),
                            "kind": artifact.get("kind"),
                            "sha256": artifact.get("sha256"),
                            "size_bytes": artifact.get("size_bytes"),
                            "evidence_status": artifact.get("evidence_status"),
                        }
                        for artifact in sorted(
                            manifest.get("artifacts", []),
                            key=lambda item: (item.get("kind") or "", item.get("id") or ""),
                        )
                        if "test" in (artifact.get("kind") or "")
                        or "pytest" in (artifact.get("kind") or "")
                    ],
                },
            }
        )

    def set_run_baseline(self, name: str, run_id: str) -> RunBaselineRecord:
        if not name.strip():
            raise ValueError("Baseline name is required")
        snapshot = self.build_run_evidence_snapshot(run_id)
        evidence_sha256 = self._stable_json_sha256(snapshot)
        timestamp = now_iso()
        snapshot_json = json.dumps(snapshot, sort_keys=True, default=str)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO run_baselines (name, run_id, created_at, evidence_sha256, snapshot_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                  run_id = excluded.run_id,
                  created_at = excluded.created_at,
                  evidence_sha256 = excluded.evidence_sha256,
                  snapshot_json = excluded.snapshot_json
                """,
                (name, run_id, timestamp, evidence_sha256, snapshot_json),
            )
        return self.get_run_baseline(name)

    def get_run_baseline(self, name: str) -> RunBaselineRecord:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM run_baselines WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise KeyError(f"Baseline not found: {name}")
        return self._row_to_run_baseline(row)

    def compare_runs(self, run_a: str, run_b: str) -> RunCompareResult:
        return self._compare_snapshots(
            run_a=run_a,
            run_b=run_b,
            snapshot_a=self.build_run_evidence_snapshot(run_a),
            snapshot_b=self.build_run_evidence_snapshot(run_b),
        )

    def compare_run_to_baseline(self, run_id: str, baseline_name: str) -> dict[str, Any]:
        baseline = self.get_run_baseline(baseline_name)
        comparison = self._compare_snapshots(
            run_a=baseline.run_id,
            run_b=run_id,
            snapshot_a=baseline.snapshot,
            snapshot_b=self.build_run_evidence_snapshot(run_id),
        )
        return {
            "schema_version": "harness.baseline_compare/v1",
            "ok": True,
            "baseline": baseline.model_dump(mode="json"),
            "comparison": comparison.model_dump(mode="json"),
        }

    def _compare_snapshots(
        self,
        *,
        run_a: str,
        run_b: str,
        snapshot_a: dict[str, Any],
        snapshot_b: dict[str, Any],
    ) -> RunCompareResult:
        section_names = [
            "run_status",
            "effective_policy_sha256",
            "backend_descriptor_sha256",
            "sandbox_profile",
            "approvals",
            "task_objective_linkage",
            "artifacts",
            "test_result_evidence",
        ]
        sections = {
            section: {
                "matches": snapshot_a.get(section) == snapshot_b.get(section),
                "run_a": snapshot_a.get(section),
                "run_b": snapshot_b.get(section),
            }
            for section in section_names
        }
        changed_sections = [section for section, value in sections.items() if not value["matches"]]
        return RunCompareResult(
            run_a=run_a,
            run_b=run_b,
            matches=not changed_sections,
            changed_sections=changed_sections,
            sections=sections,
        )

    def _stable_json_sha256(self, value: Any) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _latest_backend_descriptor(self, run_id: str) -> BackendDescriptor | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT backend_name, backend_kind, metadata_json, capabilities_json
                FROM backend_snapshots
                WHERE run_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return BackendDescriptor(
            name=row["backend_name"],
            kind=BackendKind(row["backend_kind"]),
            metadata=BackendMetadata.model_validate_json(row["metadata_json"]),
            capabilities=BackendCapabilities.model_validate_json(row["capabilities_json"]),
        )

    def _row_to_artifact(self, row: sqlite3.Row) -> ArtifactRecord:
        from harness.integrity import artifact_provenance_from_metadata

        metadata = json.loads(row["metadata_json"])
        return ArtifactRecord(
            schema_version=row["schema_version"] or "harness.artifact/v1",
            id=row["id"],
            run_id=row["run_id"],
            session_id=row["session_id"] if "session_id" in row.keys() else None,
            kind=row["kind"],
            path=Path(row["path"]),
            created_at=parse_dt(row["created_at"]),
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            producer=row["producer"],
            redaction_state=row["redaction_state"] or "unknown",
            evidence_status=row["evidence_status"] or "unknown",
            metadata=metadata,
            provenance=artifact_provenance_from_metadata(
                artifact_id=row["id"],
                run_id=row["run_id"],
                kind=row["kind"],
                producer=row["producer"],
                sha256=row["sha256"],
                redaction_state=row["redaction_state"] or "unknown",
                metadata=metadata,
                created_at=parse_dt(row["created_at"]),
            ),
        )

    def _row_to_run_baseline(self, row: sqlite3.Row) -> RunBaselineRecord:
        return RunBaselineRecord(
            name=row["name"],
            run_id=row["run_id"],
            created_at=parse_dt(row["created_at"]),
            evidence_sha256=row["evidence_sha256"],
            snapshot=json.loads(row["snapshot_json"]),
        )

    def _row_to_memory_record(self, row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"],
            scope_type=MemoryScopeType(row["scope_type"]),
            scope_id=row["scope_id"],
            source_kind=MemorySourceKind(row["source_kind"]),
            source_id=row["source_id"],
            source_artifact_id=row["source_artifact_id"],
            summary=row["summary"],
            redaction_state=MemoryRedactionState(row["redaction_state"]),
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            created_at=parse_dt(row["created_at"]),
            updated_at=parse_dt(row["updated_at"]),
            lineage=json.loads(row["lineage_json"]),
        )

    def _row_to_context_chunk(self, row: sqlite3.Row) -> Any:
        from harness.context_chunks import ContextChunk

        return ContextChunk(
            id=row["id"],
            schema_version=row["schema_version"],
            source_kind=ContextSourceKind(row["source_kind"]),
            trust_level=ContextTrustLevel(row["trust_level"]),
            path=row["path"],
            source_id=row["source_id"],
            artifact_id=row["artifact_id"],
            memory_id=row["memory_id"],
            start_line=row["start_line"],
            end_line=row["end_line"],
            sha256=row["sha256"],
            size_bytes=row["size_bytes"],
            token_count=row["token_count"],
            tokenizer=row["tokenizer"],
            chunk_scheme=row["chunk_scheme"],
            text_preview=row["text_preview"],
            redaction_state=row["redaction_state"],
            warnings=json.loads(row["warnings_json"]),
            metadata=json.loads(row["metadata_json"]),
        )

    def _row_to_context_vector(self, row: sqlite3.Row) -> Any:
        from harness.context_vector import VectorRecord

        return VectorRecord(
            id=row["id"],
            schema_version=row["schema_version"],
            chunk_id=row["chunk_id"],
            embedding_provider_id=row["embedding_provider_id"],
            dimension=row["dimension"],
            quantization=row["quantization"],
            source_sha256=row["source_sha256"],
            vector=json.loads(row["vector_json"]),
            metadata=json.loads(row["metadata_json"]),
        )

    def _row_to_run(self, row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=row["id"],
            goal=row["goal"],
            task_type=row["task_type"],
            status=row["status"],
            project_root=Path(row["project_root"]),
            created_at=parse_dt(row["created_at"]),
            updated_at=parse_dt(row["updated_at"]),
            backend_name=row["backend_name"],
            backend_kind=row["backend_kind"],
            billing_mode=row["billing_mode"],
            execution_location=row["execution_location"],
            data_boundary=row["data_boundary"],
            allow_network=bool(row["allow_network"]) if row["allow_network"] is not None else None,
            approval_id=row["approval_id"] if "approval_id" in row.keys() else None,
            task_id=row["task_id"] if "task_id" in row.keys() else None,
            objective_id=row["objective_id"] if "objective_id" in row.keys() else None,
            session_id=row["session_id"] if "session_id" in row.keys() else None,
        )

    def _row_to_session(self, row: sqlite3.Row) -> SessionSpec:
        return SessionSpec(
            id=row["id"],
            project_path=Path(row["project_path"]),
            title=row["title"] if "title" in row.keys() else None,
            parent_session_id=row["parent_session_id"] if "parent_session_id" in row.keys() else None,
            forked_from_message_id=row["forked_from_message_id"] if "forked_from_message_id" in row.keys() else None,
            objective_id=row["objective_id"],
            active_task_id=row["active_task_id"],
            active_run_id=row["active_run_id"],
            workbench_id=row["workbench_id"],
            agent_id=row["agent_id"],
            provider_id=row["provider_id"] if "provider_id" in row.keys() else None,
            model_id=row["model_id"] if "model_id" in row.keys() else None,
            model_variant=row["model_variant"] if "model_variant" in row.keys() else None,
            raw_model_ref=row["raw_model_ref"] if "raw_model_ref" in row.keys() else None,
            mode=row["mode"],
            intent=row["intent"],
            status=SessionStatus(row["status"]),
            summary=row["summary"] if "summary" in row.keys() else None,
            token_input=row["token_input"] if "token_input" in row.keys() and row["token_input"] is not None else 0,
            token_output=row["token_output"] if "token_output" in row.keys() and row["token_output"] is not None else 0,
            token_reasoning=row["token_reasoning"]
            if "token_reasoning" in row.keys() and row["token_reasoning"] is not None
            else 0,
            token_cache_read=row["token_cache_read"]
            if "token_cache_read" in row.keys() and row["token_cache_read"] is not None
            else 0,
            token_cache_write=row["token_cache_write"]
            if "token_cache_write" in row.keys() and row["token_cache_write"] is not None
            else 0,
            estimated_cost_usd=row["estimated_cost_usd"]
            if "estimated_cost_usd" in row.keys() and row["estimated_cost_usd"]
            else None,
            ui_preferences=json.loads(row["ui_preferences_json"] or "{}")
            if "ui_preferences_json" in row.keys()
            else {},
            created_at=parse_dt(row["created_at"]),
            updated_at=parse_dt(row["updated_at"]),
            archived_at=parse_dt(row["archived_at"])
            if "archived_at" in row.keys() and row["archived_at"]
            else None,
            metadata=json.loads(row["metadata_json"]),
        )

    def _row_to_session_todo(self, row: sqlite3.Row) -> SessionTodoRecord:
        return SessionTodoRecord(
            id=row["id"],
            session_id=row["session_id"],
            content=row["content"],
            status=row["status"],
            priority=row["priority"],
            source_message_id=row["source_message_id"],
            created_at=parse_dt(row["created_at"]),
            updated_at=parse_dt(row["updated_at"]),
        )

    def _row_to_session_permission(self, row: sqlite3.Row) -> SessionPermissionRequest:
        return SessionPermissionRequest(
            id=row["id"],
            session_id=row["session_id"],
            run_id=row["run_id"],
            tool_id=row["tool_id"],
            normalized_action=row["normalized_action"],
            normalized_target_pattern=row["normalized_target_pattern"],
            boundary_kind=SessionPermissionBoundaryKind(row["boundary_kind"]),
            risk=row["risk"],
            status=SessionPermissionStatus(row["status"]),
            scope=SessionPermissionScope(row["scope"]),
            source=SessionPermissionSource(row["source"]),
            revocable=bool(row["revocable"]),
            requested_at=parse_dt(row["requested_at"]),
            resolved_at=parse_dt(row["resolved_at"]) if row["resolved_at"] else None,
            expires_at=parse_dt(row["expires_at"]),
            policy_reasons=json.loads(row["policy_reasons_json"] or "[]"),
        )

    def _row_to_stored_event(self, row: sqlite3.Row) -> StoredEventRecord:
        return StoredEventRecord(
            id=row["id"],
            stream_type=EventStreamType(row["stream_type"]),
            stream_id=row["stream_id"],
            seq=row["seq"],
            kind=row["kind"],
            visibility=EventVisibility(row["visibility"]),
            redaction_state=RedactionState(row["redaction_state"]),
            session_id=row["session_id"],
            message_id=row["message_id"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            artifact_id=row["artifact_id"],
            actor=json.loads(row["actor_json"] or "{}"),
            correlation_id=row["correlation_id"],
            causation_id=row["causation_id"],
            payload=json.loads(row["payload_json"] or "{}"),
            artifact_refs=json.loads(row["artifact_refs_json"] or "[]"),
            created_at=parse_dt(row["created_at"]),
        )

    def _row_to_task(self, row: sqlite3.Row) -> TaskRecord:
        depends_on = set(json.loads(row["depends_on_json"]))
        with self.connect() as conn:
            dependency_rows = conn.execute(
                """
                SELECT upstream_task_id
                FROM task_dependencies
                WHERE downstream_task_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (row["id"],),
            ).fetchall()
        depends_on.update(dependency["upstream_task_id"] for dependency in dependency_rows)
        return TaskRecord(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            status=normalize_task_status(row["status"]),
            project_root=Path(row["project_root"]),
            created_at=parse_dt(row["created_at"]),
            updated_at=parse_dt(row["updated_at"]),
            priority=row["priority"],
            objective_id=row["objective_id"] if "objective_id" in row.keys() else None,
            workbench_id=row["workbench_id"],
            agent_id=row["agent_id"],
            spec_source_kind=row["spec_source_kind"],
            spec_source_path=Path(row["spec_source_path"]) if row["spec_source_path"] else None,
            depends_on=sorted(depends_on),
            idempotency_key=row["idempotency_key"] if "idempotency_key" in row.keys() else None,
            required_approvals=json.loads(row["required_approvals_json"])
            if "required_approvals_json" in row.keys() and row["required_approvals_json"]
            else [],
            approval_state=row["approval_state"] if "approval_state" in row.keys() else None,
            run_id=row["run_id"],
            session_id=row["session_id"] if "session_id" in row.keys() else None,
            metadata=json.loads(row["metadata_json"]),
        )

    def _row_to_project_agent(self, row: sqlite3.Row) -> ProjectAgentRecord:
        return ProjectAgentRecord(
            agent_id=row["agent_id"],
            workbench_id=row["workbench_id"],
            project_root=Path(row["project_root"]),
            imported_at=parse_dt(row["imported_at"]),
            source_path=Path(row["source_path"]),
            content_sha256=row["content_sha256"],
            agent=json.loads(row["agent_json"]),
            profiles=json.loads(row["profiles_json"]),
        )

    def _project_agent_registry(self, record: ProjectAgentRecord) -> SpecRegistry:
        builtin = builtin_spec_registry()
        agent = AgentSpec.model_validate(record.agent)
        profiles = [AgentProfileSpec.model_validate(profile) for profile in record.profiles]
        return SpecRegistry(
            model_profiles=dict(builtin.model_profiles),
            tool_policies=dict(builtin.tool_policies),
            memory_scopes=dict(builtin.memory_scopes),
            agents={**builtin.agents, record.agent_id: agent},
            agent_profiles={**builtin.agent_profiles, **{profile.id: profile for profile in profiles}},
            workbenches=dict(builtin.workbenches),
        )

    def _row_to_task_dependency(self, row: sqlite3.Row) -> TaskDependency:
        return TaskDependency(
            id=row["id"],
            upstream_task_id=row["upstream_task_id"],
            downstream_task_id=row["downstream_task_id"],
            dependency_type=TaskDependencyType(row["dependency_type"]),
            required_artifact_kind=row["required_artifact_kind"],
            created_at=parse_dt(row["created_at"]),
        )

    def _row_to_task_attempt(self, row: sqlite3.Row) -> TaskAttempt:
        return TaskAttempt(
            id=row["id"],
            task_id=row["task_id"],
            attempt_number=row["attempt_number"],
            status=normalize_task_status(row["status"]),
            lease_id=row["lease_id"],
            run_id=row["run_id"],
            created_at=parse_dt(row["created_at"]),
            started_at=parse_dt(row["started_at"]) if row["started_at"] else None,
            finished_at=parse_dt(row["finished_at"]) if row["finished_at"] else None,
            failure_code=row["failure_code"],
            failure_message=row["failure_message"],
            metadata=json.loads(row["metadata_json"]),
        )

    def _row_to_task_lease(self, row: sqlite3.Row) -> TaskLease:
        return TaskLease(
            id=row["id"],
            task_id=row["task_id"],
            attempt_id=row["attempt_id"],
            owner=row["owner"],
            status=TaskLeaseStatus(row["status"]),
            acquired_at=parse_dt(row["acquired_at"]),
            expires_at=parse_dt(row["expires_at"]),
            heartbeat_at=parse_dt(row["heartbeat_at"]) if row["heartbeat_at"] else None,
            released_at=parse_dt(row["released_at"]) if row["released_at"] else None,
            metadata=json.loads(row["metadata_json"]),
        )

    def _row_to_daemon(self, row: sqlite3.Row) -> DaemonRecord:
        return DaemonRecord(
            id=row["id"],
            owner=row["owner"],
            status=DaemonStatus(row["status"]),
            pid=row["pid"],
            project_root=Path(row["project_root"]),
            started_at=parse_dt(row["started_at"]),
            heartbeat_at=parse_dt(row["heartbeat_at"]),
            stopped_at=parse_dt(row["stopped_at"]) if row["stopped_at"] else None,
            metadata=json.loads(row["metadata_json"]),
        )

    def _row_to_daemon_event(self, row: sqlite3.Row) -> DaemonEvent:
        return DaemonEvent(
            id=row["id"],
            daemon_id=row["daemon_id"],
            event_type=row["event_type"],
            message=row["message"],
            created_at=parse_dt(row["created_at"]),
            metadata=json.loads(row["metadata_json"]),
        )

    def _row_to_kill_switch(self, row: sqlite3.Row) -> KillSwitchRecord:
        return KillSwitchRecord(
            id=row["id"],
            target_kind=KillSwitchTargetKind(row["target_kind"]),
            target_id=row["target_id"],
            disabled=bool(row["disabled"]),
            reason=row["reason"],
            actor=row["actor"],
            created_at=parse_dt(row["created_at"]),
            updated_at=parse_dt(row["updated_at"]),
            metadata=json.loads(row["metadata_json"]),
        )

    def _row_to_objective(self, row: sqlite3.Row) -> ObjectiveRecord:
        return ObjectiveRecord(
            id=row["id"],
            title=row["title"],
            description=row["description"],
            status=ObjectiveStatus(row["status"]),
            project_root=Path(row["project_root"]),
            created_at=parse_dt(row["created_at"]),
            updated_at=parse_dt(row["updated_at"]),
            priority=row["priority"],
            workbench_id=row["workbench_id"],
            session_id=row["session_id"] if "session_id" in row.keys() else None,
            metadata=json.loads(row["metadata_json"]),
        )

    def _row_to_task_transition(self, row: sqlite3.Row) -> TaskTransitionRecord:
        return TaskTransitionRecord(
            id=row["id"],
            task_id=row["task_id"],
            from_status=normalize_task_status(row["from_status"]) if row["from_status"] else None,
            to_status=normalize_task_status(row["to_status"]),
            reason=row["reason"],
            actor=row["actor"],
            created_at=parse_dt(row["created_at"]),
            metadata=json.loads(row["metadata_json"]),
        )


def _execution_adapter_descriptor_for_run_record(store: SQLiteStore, run: RunRecord):
    if run.task_id is not None:
        try:
            task = store.get_task(run.task_id)
        except KeyError:
            task = None
        adapter_id = task.metadata.get("execution_adapter") if task is not None else None
        if isinstance(adapter_id, str) and adapter_id.strip():
            from harness.execution import get_execution_adapter_descriptor

            descriptor = get_execution_adapter_descriptor(adapter_id)
            if descriptor is not None:
                return descriptor
    return None


def _delegate_budget_for_run_descriptor(descriptor) -> dict[str, Any] | None:
    if descriptor is None:
        return None
    from harness.delegate_budgets import adapter_delegate_budget_projection

    return adapter_delegate_budget_projection(descriptor)


def _legacy_sandbox_profile_id_for_task_type(task_type: str | None) -> str | None:
    mapping = {
        DRY_RUN_TASK_TYPE: "none",
        READ_ONLY_TASK_TYPE: "read_only_codex",
        "repo_planning": "read_only_codex",
        "codex_code_edit": "isolated_workspace_codex",
        "docker_run_tests": "docker_test_sandbox",
    }
    return mapping.get(task_type or "")


def _validate_registered_execution_task_payload(
    metadata: dict[str, Any],
    *,
    agent_id: str | None,
    depends_on: list[str],
) -> None:
    has_adapter_metadata = "execution_adapter" in metadata or "task_type" in metadata
    if not has_adapter_metadata:
        return
    execution_adapter = metadata.get("execution_adapter")
    task_type = metadata.get("task_type")
    if not isinstance(execution_adapter, str) or not execution_adapter.strip():
        raise ValueError("Task execution metadata requires execution_adapter.")
    if not isinstance(task_type, str) or not task_type.strip():
        raise ValueError("Task execution metadata requires task_type.")
    from harness.execution import validate_execution_task_payload

    reasons = validate_execution_task_payload(
        execution_adapter=execution_adapter,
        task_type=task_type,
        metadata=metadata,
        agent_id=agent_id,
        depends_on=depends_on,
    )
    if reasons:
        raise ValueError("Invalid execution task metadata: " + " ".join(reasons))


def _redacted_artifact_path(path: Path) -> Path:
    suffix = "".join(path.suffixes)
    if suffix:
        base = path.name[: -len(suffix)]
        candidate = path.with_name(f"{base}.redacted{suffix}")
    else:
        candidate = path.with_name(f"{path.name}.redacted")
    counter = 2
    while candidate.exists():
        if suffix:
            candidate = path.with_name(f"{base}.redacted_{counter}{suffix}")
        else:
            candidate = path.with_name(f"{path.name}.redacted_{counter}")
        counter += 1
    return candidate
