CREATE TABLE IF NOT EXISTS context_vectors (
  id TEXT PRIMARY KEY,
  schema_version TEXT NOT NULL,
  chunk_id TEXT NOT NULL,
  embedding_provider_id TEXT NOT NULL,
  dimension INTEGER NOT NULL,
  quantization TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  vector_json TEXT NOT NULL,
  metadata_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(chunk_id) REFERENCES context_chunks(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_context_vectors_chunk_provider
  ON context_vectors(chunk_id, embedding_provider_id);

CREATE INDEX IF NOT EXISTS idx_context_vectors_provider_hash
  ON context_vectors(embedding_provider_id, source_sha256);
