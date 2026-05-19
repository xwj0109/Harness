CREATE TABLE IF NOT EXISTS context_chunks (
  id TEXT PRIMARY KEY,
  schema_version TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  trust_level TEXT NOT NULL,
  path TEXT,
  source_id TEXT,
  artifact_id TEXT,
  memory_id TEXT,
  start_line INTEGER,
  end_line INTEGER,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  token_count INTEGER,
  tokenizer TEXT,
  chunk_scheme TEXT NOT NULL,
  text_preview TEXT NOT NULL,
  redaction_state TEXT,
  warnings_json TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_context_chunks_source ON context_chunks(source_kind, path);
CREATE INDEX IF NOT EXISTS idx_context_chunks_hash ON context_chunks(sha256, chunk_scheme, tokenizer);

