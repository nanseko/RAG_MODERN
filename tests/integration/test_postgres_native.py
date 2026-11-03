"""Opt-in PostgreSQL protocol tests; create and remove only a random scratch schema.

Run with RAG_TEST_POSTGRES_DSN pointing at an authorized disposable server.
The company's existing tables, IDs, indexes and corpus are never modified.
"""
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from database.db import capabilities, get_conn
from database.jobs import claim_job, reclaim_expired, update_claim
from ingestion.collector import enqueue_file
from ingestion.indexer import index_document


@pytest.fixture
def postgres_runtime(tmp_path, monkeypatch):
    dsn = os.getenv("RAG_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("RAG_TEST_POSTGRES_DSN is not configured")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    schema = "rag_test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    monkeypatch.setenv("RAG_DB_BACKEND", "postgresql")
    monkeypatch.setenv("RAG_SEARCH_BACKEND", "opensearch")
    monkeypatch.setenv("RAG_POSTGRES_DSN", make_conninfo(dsn, options=f"-c search_path={schema}"))
    monkeypatch.setenv("USE_QWEN", "0")
    try:
        yield tmp_path
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def test_postgres_metadata_versions_embeddings_and_savepoint_rollback(postgres_runtime):
    conn = get_conn()
    try:
        assert capabilities(conn).backend == "postgresql" and not capabilities(conn).native_vectors
        vector = [1.0] + [0.0] * 1023
        did = index_document(conn, {"document_id": "scratch_doc", "title": "v1", "version": "v1", "allowed_groups": ["team"]},
                             [{"text": "isolated document", "page_number": 2}], vectors=[vector])
        row = conn.execute("SELECT * FROM documents WHERE document_id=?", (did,)).fetchone()
        assert row["version"] == "v1" and dict(row)["allowed_groups"] == '["team"]'
        assert conn.execute("SELECT embedding FROM chunks WHERE document_id=?", (did,)).fetchone()[0].startswith("[1.0,")
        assert conn.execute("SELECT COUNT(*) FROM search_outbox").fetchone()[0] == 1
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("SAVEPOINT probe")
        conn.execute("UPDATE documents SET title=? WHERE document_id=?", ("rolled back", did))
        conn.execute("ROLLBACK TO SAVEPOINT probe")
        conn.execute("RELEASE SAVEPOINT probe")
        conn.commit()
        assert conn.execute("SELECT title FROM documents WHERE document_id=?", (did,)).fetchone()[0] == "v1"
        index_document(conn, {"document_id": did, "title": "v2", "version": "v2", "allowed_groups": ["team"]},
                       [{"text": "new isolated document"}], vectors=[None])
        assert conn.execute("SELECT COUNT(*) FROM chunks WHERE document_id=?", (did,)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM document_versions WHERE document_id=?", (did,)).fetchone()[0] == 2
        # Percent literals and bound question marks must retain their exact values.
        assert tuple(conn.execute("SELECT '?' AS q,? AS bound,'100%' AS pct", ("?%",)).fetchone()) == ("?", "?%", "100%")
    finally:
        conn.close()


def test_postgres_initialization_and_claim_are_safe_across_connections(postgres_runtime):
    path = postgres_runtime / "report.md"
    path.write_text("one", encoding="utf8")
    barrier = threading.Barrier(4)
    def enqueue():
        barrier.wait()
        conn = get_conn()
        try:
            return enqueue_file(conn, path)
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda unused: enqueue(), range(4)))
    jobs = [job for job in results if job]
    assert len(jobs) == 1
    conn = get_conn()
    first = jobs[0]
    path.write_text("two", encoding="utf8")
    second = enqueue_file(conn, path)
    # Force the old job back to pending to challenge cross-job exclusion.
    conn.execute("UPDATE ingestion_jobs SET status='PENDING' WHERE job_id=?", (first["job_id"],))
    conn.commit()
    claim_barrier = threading.Barrier(2)
    def claim(job):
        other = get_conn()
        try:
            claim_barrier.wait()
            return (job["job_id"], claim_job(other, job["job_id"]))
        finally:
            other.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, (first, second)))
    winners = [(jid, token) for jid, token in claims if token]
    assert len(winners) == 1
    jid, old = winners[0]
    conn.execute("UPDATE ingestion_jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE job_id=?", (jid,))
    conn.commit()
    assert reclaim_expired(conn) == 1
    new = claim_job(conn, jid)
    assert new and old != new
    assert not update_claim(conn, jid, old, status="FAILED", error="stale worker")
    assert update_claim(conn, jid, new, status="DONE", stage="ACTIVE")
    conn.close()

def test_postgres_graph_load_rows_uses_portable_chunk_order(postgres_runtime):
    from rag.graph import _load_rows
    conn = get_conn()
    try:
        index_document(conn, {"document_id": "scratch_load", "title": "load", "allowed_groups": ["team"]}, [{"text": "portable graph evidence"}])
        rows = _load_rows(conn)
        assert rows and rows[0]["document_id"] == "scratch_load"
        assert rows[0]["text"] == "portable graph evidence"
    finally:
        conn.close()
