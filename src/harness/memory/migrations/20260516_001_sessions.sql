CREATE TABLE IF NOT EXISTS schema_migrations (
  id TEXT PRIMARY KEY,
  checksum TEXT NOT NULL,
  applied_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  goal TEXT,
  task_type TEXT,
  status TEXT NOT NULL,
  project_root TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  backend_name TEXT,
  backend_kind TEXT,
  billing_mode TEXT,
  execution_location TEXT,
  data_boundary TEXT,
  allow_network INTEGER,
  approval_id TEXT,
  task_id TEXT,
  objective_id TEXT,
  session_id TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  level TEXT NOT NULL,
  event_type TEXT NOT NULL,
  message TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  session_id TEXT,
  schema_version TEXT,
  seq INTEGER,
  task_id TEXT,
  trace_id TEXT,
  visibility TEXT,
  redaction_state TEXT,
  FOREIGN KEY(run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  path TEXT NOT NULL,
  created_at TEXT NOT NULL,
  schema_version TEXT,
  sha256 TEXT,
  size_bytes INTEGER,
  producer TEXT,
  redaction_state TEXT,
  evidence_status TEXT,
  metadata_json TEXT NOT NULL,
  session_id TEXT,
  FOREIGN KEY(run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS backend_snapshots (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  backend_name TEXT NOT NULL,
  backend_kind TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  capabilities_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  status TEXT NOT NULL,
  project_root TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  objective_id TEXT,
  workbench_id TEXT,
  agent_id TEXT,
  spec_source_kind TEXT,
  spec_source_path TEXT,
  depends_on_json TEXT NOT NULL,
  idempotency_key TEXT,
  required_approvals_json TEXT,
  approval_state TEXT,
  run_id TEXT,
  session_id TEXT,
  metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_agents (
  agent_id TEXT PRIMARY KEY,
  workbench_id TEXT NOT NULL,
  project_root TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  source_path TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  agent_json TEXT NOT NULL,
  profiles_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS objectives (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  status TEXT NOT NULL,
  project_root TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  workbench_id TEXT,
  session_id TEXT,
  metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  project_path TEXT NOT NULL,
  title TEXT,
  parent_session_id TEXT,
  forked_from_message_id TEXT,
  objective_id TEXT,
  active_task_id TEXT,
  active_run_id TEXT,
  workbench_id TEXT,
  agent_id TEXT,
  provider_id TEXT,
  model_id TEXT,
  model_variant TEXT,
  raw_model_ref TEXT,
  mode TEXT,
  intent TEXT,
  status TEXT NOT NULL,
  summary TEXT,
  token_input INTEGER NOT NULL DEFAULT 0,
  token_output INTEGER NOT NULL DEFAULT 0,
  token_reasoning INTEGER NOT NULL DEFAULT 0,
  token_cache_read INTEGER NOT NULL DEFAULT 0,
  token_cache_write INTEGER NOT NULL DEFAULT 0,
  estimated_cost_usd TEXT,
  ui_preferences_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  archived_at TEXT,
  metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_messages (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  parent_message_id TEXT,
  role TEXT NOT NULL,
  agent_id TEXT,
  run_id TEXT,
  objective_id TEXT,
  mutation_reversibility TEXT NOT NULL DEFAULT 'none',
  content_preview TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS session_parts (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  text TEXT,
  artifact_id TEXT,
  run_id TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  redaction_state TEXT NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY(session_id) REFERENCES sessions(id),
  FOREIGN KEY(message_id) REFERENCES session_messages(id)
);

CREATE TABLE IF NOT EXISTS session_run_links (
  session_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  message_id TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY(session_id, run_id)
);

CREATE TABLE IF NOT EXISTS event_store (
  id TEXT PRIMARY KEY,
  stream_type TEXT NOT NULL,
  stream_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,
  visibility TEXT NOT NULL,
  redaction_state TEXT NOT NULL,
  session_id TEXT,
  message_id TEXT,
  run_id TEXT,
  task_id TEXT,
  artifact_id TEXT,
  actor_json TEXT NOT NULL DEFAULT '{}',
  correlation_id TEXT,
  causation_id TEXT,
  payload_json TEXT NOT NULL,
  artifact_refs_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  UNIQUE(stream_type, stream_id, seq)
);

CREATE TABLE IF NOT EXISTS session_todos (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  content TEXT NOT NULL,
  status TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  source_message_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS session_permissions (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  run_id TEXT,
  tool_id TEXT NOT NULL,
  normalized_action TEXT NOT NULL,
  normalized_target_pattern TEXT NOT NULL,
  boundary_kind TEXT NOT NULL,
  risk TEXT NOT NULL,
  status TEXT NOT NULL,
  scope TEXT NOT NULL,
  source TEXT NOT NULL,
  revocable INTEGER NOT NULL DEFAULT 1,
  policy_reasons_json TEXT NOT NULL DEFAULT '[]',
  requested_at TEXT NOT NULL,
  resolved_at TEXT,
  expires_at TEXT NOT NULL,
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS provider_model_catalog_cache (
  id TEXT PRIMARY KEY,
  catalog_kind TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  backend_id TEXT NOT NULL,
  model_id TEXT,
  model_profile_id TEXT,
  raw_model_ref TEXT,
  payload_json TEXT NOT NULL,
  refreshed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_dependencies (
  id TEXT PRIMARY KEY,
  upstream_task_id TEXT NOT NULL,
  downstream_task_id TEXT NOT NULL,
  dependency_type TEXT NOT NULL,
  required_artifact_kind TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(upstream_task_id) REFERENCES tasks(id),
  FOREIGN KEY(downstream_task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS task_attempts (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  attempt_number INTEGER NOT NULL,
  status TEXT NOT NULL,
  lease_id TEXT,
  run_id TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  failure_code TEXT,
  failure_message TEXT,
  metadata_json TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(id),
  FOREIGN KEY(run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS task_leases (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  attempt_id TEXT,
  owner TEXT NOT NULL,
  status TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  heartbeat_at TEXT,
  released_at TEXT,
  metadata_json TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(id),
  FOREIGN KEY(attempt_id) REFERENCES task_attempts(id)
);

CREATE TABLE IF NOT EXISTS task_transitions (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  from_status TEXT,
  to_status TEXT NOT NULL,
  reason TEXT NOT NULL,
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS run_baselines (
  name TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  evidence_sha256 TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  FOREIGN KEY(run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS daemon_records (
  id TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  status TEXT NOT NULL,
  pid INTEGER,
  project_root TEXT NOT NULL,
  started_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  stopped_at TEXT,
  metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daemon_events (
  id TEXT PRIMARY KEY,
  daemon_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  FOREIGN KEY(daemon_id) REFERENCES daemon_records(id)
);

CREATE TABLE IF NOT EXISTS memory_records (
  id TEXT PRIMARY KEY,
  scope_type TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_id TEXT,
  source_artifact_id TEXT,
  summary TEXT NOT NULL,
  redaction_state TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  lineage_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_controls (
  id TEXT PRIMARY KEY,
  target_kind TEXT NOT NULL,
  target_id TEXT NOT NULL,
  disabled INTEGER NOT NULL,
  reason TEXT NOT NULL,
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  UNIQUE(target_kind, target_id)
);

CREATE TABLE IF NOT EXISTS execution_breaker_resets (
  id TEXT PRIMARY KEY,
  adapter_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_priority_created
  ON tasks(status, priority DESC, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_tasks_objective_status
  ON tasks(objective_id, status);

CREATE INDEX IF NOT EXISTS idx_sessions_updated
  ON sessions(updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_sessions_status_updated
  ON sessions(status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_session_messages_session_created
  ON session_messages(session_id, created_at);

CREATE INDEX IF NOT EXISTS idx_session_parts_message_ordinal
  ON session_parts(message_id, ordinal);

CREATE INDEX IF NOT EXISTS idx_event_store_stream_seq
  ON event_store(stream_type, stream_id, seq);

CREATE INDEX IF NOT EXISTS idx_event_store_session_seq
  ON event_store(session_id, seq);

CREATE INDEX IF NOT EXISTS idx_event_store_run_seq
  ON event_store(run_id, seq);

CREATE INDEX IF NOT EXISTS idx_session_run_links_run
  ON session_run_links(run_id);

CREATE INDEX IF NOT EXISTS idx_session_todos_session_status
  ON session_todos(session_id, status);

CREATE INDEX IF NOT EXISTS idx_session_permissions_session_status
  ON session_permissions(session_id, status);

CREATE INDEX IF NOT EXISTS idx_session_permissions_subject_status
  ON session_permissions(tool_id, normalized_action, boundary_kind, status);

CREATE INDEX IF NOT EXISTS idx_provider_model_catalog_kind_provider
  ON provider_model_catalog_cache(catalog_kind, provider_id);

CREATE INDEX IF NOT EXISTS idx_provider_model_catalog_raw_ref
  ON provider_model_catalog_cache(raw_model_ref);

CREATE INDEX IF NOT EXISTS idx_project_agents_workbench
  ON project_agents(workbench_id, imported_at ASC);

CREATE INDEX IF NOT EXISTS idx_task_dependencies_downstream
  ON task_dependencies(downstream_task_id);

CREATE INDEX IF NOT EXISTS idx_task_dependencies_upstream
  ON task_dependencies(upstream_task_id);

CREATE INDEX IF NOT EXISTS idx_execution_controls_target
  ON execution_controls(target_kind, target_id);

CREATE INDEX IF NOT EXISTS idx_execution_breaker_resets_adapter
  ON execution_breaker_resets(adapter_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_task_attempts_task
  ON task_attempts(task_id, attempt_number);

CREATE INDEX IF NOT EXISTS idx_task_leases_task_status
  ON task_leases(task_id, status);

CREATE INDEX IF NOT EXISTS idx_task_transitions_task_created
  ON task_transitions(task_id, created_at);

CREATE INDEX IF NOT EXISTS idx_run_baselines_run
  ON run_baselines(run_id);

CREATE INDEX IF NOT EXISTS idx_sessions_status_updated
  ON sessions(status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_daemon_records_status_heartbeat
  ON daemon_records(status, heartbeat_at DESC);

CREATE INDEX IF NOT EXISTS idx_daemon_events_daemon_created
  ON daemon_events(daemon_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_records_scope_created
  ON memory_records(scope_type, scope_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_records_redaction_updated
  ON memory_records(redaction_state, updated_at DESC);
