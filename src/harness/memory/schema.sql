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
  objective_id TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  level TEXT NOT NULL,
  event_type TEXT NOT NULL,
  message TEXT NOT NULL,
  payload_json TEXT NOT NULL,
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
  metadata_json TEXT NOT NULL
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

CREATE INDEX IF NOT EXISTS idx_tasks_status_priority_created
  ON tasks(status, priority DESC, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_tasks_objective_status
  ON tasks(objective_id, status);

CREATE INDEX IF NOT EXISTS idx_project_agents_workbench
  ON project_agents(workbench_id, imported_at ASC);

CREATE INDEX IF NOT EXISTS idx_task_dependencies_downstream
  ON task_dependencies(downstream_task_id);

CREATE INDEX IF NOT EXISTS idx_task_dependencies_upstream
  ON task_dependencies(upstream_task_id);

CREATE INDEX IF NOT EXISTS idx_task_attempts_task
  ON task_attempts(task_id, attempt_number);

CREATE INDEX IF NOT EXISTS idx_task_leases_task_status
  ON task_leases(task_id, status);

CREATE INDEX IF NOT EXISTS idx_task_transitions_task_created
  ON task_transitions(task_id, created_at);

CREATE INDEX IF NOT EXISTS idx_run_baselines_run
  ON run_baselines(run_id);

CREATE INDEX IF NOT EXISTS idx_daemon_records_status_heartbeat
  ON daemon_records(status, heartbeat_at DESC);

CREATE INDEX IF NOT EXISTS idx_daemon_events_daemon_created
  ON daemon_events(daemon_id, created_at DESC);
