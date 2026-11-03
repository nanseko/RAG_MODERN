"""Persistent float32 vectors and exact, ACL-filtered SQL cosine search."""
import json
import math

DIMENSION = 1024
EMBEDDING_VERSION = "qwen3-last-token-v2"


def initialize(conn):
    import sqlite_vec
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS chunk_vectors(
      chunk_id TEXT PRIMARY KEY, document_id TEXT NOT NULL,
      embedding BLOB NOT NULL, embedding_version TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS vector_document ON chunk_vectors(document_id);
    CREATE TRIGGER IF NOT EXISTS chunk_vector_delete AFTER DELETE ON chunks
      BEGIN DELETE FROM chunk_vectors WHERE chunk_id=OLD.chunk_id; END;
    """)


def validate_vector(vector):
    if not isinstance(vector, list) or len(vector) != DIMENSION:
        raise ValueError("embedding must contain 1024 numbers")
    if any(not isinstance(x, (float, int)) or not math.isfinite(x) for x in vector):
        raise ValueError("embedding contains invalid numbers")
    if sum(x * x for x in vector) < 1e-12:
        raise ValueError("zero embedding is invalid")


def put(conn, chunk_id, document_id, vector, version=EMBEDDING_VERSION):
    from sqlite_vec import serialize_float32
    validate_vector(vector)
    conn.execute("INSERT OR REPLACE INTO chunk_vectors VALUES(?,?,?,?)",
                 (chunk_id, document_id, serialize_float32(vector), version))


def search(conn, query_vector, allowed_chunk_ids, k=50):
    """Filter BEFORE top-k. The caller supplies ACL-permitted, non-rejected IDs."""
    from sqlite_vec import serialize_float32
    validate_vector(query_vector)
    if not allowed_chunk_ids:
        return []
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS permitted_vectors(chunk_id TEXT PRIMARY KEY)")
    conn.execute("DELETE FROM permitted_vectors")
    conn.executemany("INSERT OR IGNORE INTO permitted_vectors VALUES(?)",
                     ((cid,) for cid in allowed_chunk_ids))
    rows = conn.execute(
        "SELECT v.chunk_id,1.0-vec_distance_cosine(v.embedding,?) AS score "
        "FROM chunk_vectors v JOIN permitted_vectors p ON p.chunk_id=v.chunk_id "
        "WHERE v.embedding_version=? ORDER BY score DESC, v.chunk_id LIMIT ?",
        (serialize_float32(query_vector), EMBEDDING_VERSION, k)).fetchall()
    return [(r[0], float(r[1])) for r in rows]
