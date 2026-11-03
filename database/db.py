"""Durable metadata connections; SQLite and PostgreSQL are explicit backends."""
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.settings import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents(
 document_id TEXT PRIMARY KEY, title TEXT, file_name TEXT,
 file_type TEXT, version TEXT DEFAULT 'v1', content_hash TEXT,
 status TEXT DEFAULT 'ACTIVE', allowed_groups TEXT DEFAULT '["all"]');
CREATE TABLE IF NOT EXISTS chunks(
 chunk_id TEXT PRIMARY KEY, document_id TEXT, text TEXT,
 title TEXT, heading_path TEXT, page_number INTEGER,
 parent_chunk_id TEXT, part_numbers TEXT DEFAULT '[]',
 error_codes TEXT DEFAULT '[]', allowed_groups TEXT DEFAULT '["all"]',
 document_version TEXT DEFAULT 'v1', embedding TEXT DEFAULT NULL);
CREATE TABLE IF NOT EXISTS ingestion_jobs(
 job_id TEXT PRIMARY KEY, document_id TEXT, file_name TEXT, file_path TEXT,
 file_type TEXT, content_hash TEXT, changed INTEGER DEFAULT 0,
 status TEXT DEFAULT 'PENDING', stage TEXT DEFAULT 'COLLECT',
 error TEXT DEFAULT NULL, attempts INTEGER DEFAULT 0,
 created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '');
"""

ADDITIONS = {
    "documents": {"source_uri": "TEXT", "markdown_path": "TEXT", "original_path": "TEXT", "embedding_version": "TEXT DEFAULT 'legacy'",
                  "category": "TEXT DEFAULT '기타'", "metadata_version": "TEXT DEFAULT 'v0'",
                  "trouble_json": "TEXT DEFAULT '{}'", "outcome": "TEXT DEFAULT 'unknown'"},
    "chunks": {"embedding": "TEXT", "embedding_version": "TEXT DEFAULT 'legacy'",
               "chunk_index": "INTEGER DEFAULT 0", "slide_number": "INTEGER", "sheet_name": "TEXT",
               "role": "TEXT DEFAULT 'other'", "equipment": "TEXT DEFAULT '[]'"},
    "ingestion_jobs": {"allowed_groups": "TEXT", "input_path": "TEXT", "source_key": "TEXT", "processor_version": "TEXT DEFAULT 'legacy'",
                       "claim_token": "TEXT", "lease_expires_at": "TEXT"},
}


@dataclass(frozen=True)
class BackendCapabilities:
    backend: str
    native_vectors: bool
    distributed_claims: bool


def capabilities(conn):
    backend = getattr(conn, "backend", "sqlite")
    return BackendCapabilities(backend, backend == "sqlite", backend == "postgresql")


def migrate(conn):
    postgres = capabilities(conn).backend == "postgresql"
    for table, columns in ADDITIONS.items():
        if postgres:
            present = {r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?", (table,))}
        else:
            present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        if postgres and table == "ingestion_jobs" and "rowid" not in present:
            conn.execute("ALTER TABLE ingestion_jobs ADD COLUMN rowid BIGSERIAL UNIQUE")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS document_versions(
      document_id TEXT, version TEXT, original_path TEXT, markdown_path TEXT,
      allowed_groups TEXT, content_hash TEXT, PRIMARY KEY(document_id,version));
    CREATE TABLE IF NOT EXISTS document_edges(
      from_doc TEXT, to_doc TEXT, kind TEXT,
      PRIMARY KEY(from_doc,to_doc,kind));
    CREATE INDEX IF NOT EXISTS edge_from ON document_edges(from_doc);
    CREATE INDEX IF NOT EXISTS edge_to ON document_edges(to_doc);
    CREATE INDEX IF NOT EXISTS job_source_key ON ingestion_jobs(source_key);
    CREATE INDEX IF NOT EXISTS job_document_status ON ingestion_jobs(document_id,status);
    """)
    conn.commit()


def get_conn(db_path=None):
    # Loading settings also applies the shared dotenv policy before backend selection.
    settings = get_settings()
    backend = os.getenv("RAG_DB_BACKEND", "sqlite").strip().lower()
    if backend not in ("sqlite", "postgresql"):
        raise ValueError("RAG_DB_BACKEND must be sqlite or postgresql")
    if backend == "postgresql":
        if os.getenv("RAG_SEARCH_BACKEND", "sqlite").strip().lower() != "opensearch":
            raise ValueError("PostgreSQL metadata requires RAG_SEARCH_BACKEND=opensearch")
        dsn = os.getenv("RAG_POSTGRES_DSN") or os.getenv("DATABASE_URL")
        if not dsn:
            raise ValueError("PostgreSQL requires DATABASE_URL or RAG_POSTGRES_DSN")
        from database.postgres import connect
        conn = connect(dsn)
    else:
        db_path = db_path or settings.db
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
    try:
        if backend == "postgresql":
            conn.execute("BEGIN IMMEDIATE")
        else:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        migrate(conn)
        if capabilities(conn).native_vectors:
            from database.vector_store import initialize
            initialize(conn)
        if backend == "postgresql":
            conn.execute("BEGIN IMMEDIATE")
        from retrieval.opensearch_store import initialize_outbox
        initialize_outbox(conn)
        conn.commit()
        return conn
    except Exception:
        conn.close()
        raise