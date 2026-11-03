"""Backend and distributed-claim contracts, isolated from the company corpus."""
import importlib.util

import pytest

from database.db import get_conn


def test_database_exposes_explicit_backend_capabilities():
    from database import db
    assert hasattr(db, "capabilities"), "database backend capabilities must be explicit"
    conn = get_conn(":memory:")
    try:
        assert db.capabilities(conn).backend == "sqlite"
        assert db.capabilities(conn).native_vectors is True
    finally:
        conn.close()


def test_postgres_adapter_contract_exists():
    assert importlib.util.find_spec("database.postgres") is not None, "PostgreSQL adapter is required"


def test_claim_module_prevents_stale_worker_writes(tmp_path):
    assert importlib.util.find_spec("database.jobs") is not None, "durable job claims are required"
    from database.jobs import claim_job, reclaim_expired, update_claim
    from ingestion.collector import enqueue_file
    path = tmp_path / "report.md"
    path.write_text("test", encoding="utf8")
    conn = get_conn(":memory:")
    try:
        job = enqueue_file(conn, path)
        old = claim_job(conn, job["job_id"], 3)
        assert old and claim_job(conn, job["job_id"], 3) is None
        conn.execute("UPDATE ingestion_jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE job_id=?", (job["job_id"],))
        conn.commit()
        assert reclaim_expired(conn) == 1
        new = claim_job(conn, job["job_id"], 3)
        assert new and new != old
        assert update_claim(conn, job["job_id"], old, status="FAILED", error="old worker") is False
        assert update_claim(conn, job["job_id"], new, stage="NORMALIZE") is True
        row = conn.execute("SELECT status,stage,claim_token FROM ingestion_jobs WHERE job_id=?", (job["job_id"],)).fetchone()
        assert row["status"] == "RUNNING" and row["stage"] == "NORMALIZE" and row["claim_token"] == new
    finally:
        conn.close()


def test_celery_queue_module_exists():
    assert importlib.util.find_spec("workers.celery_app") is not None, "Redis/Celery worker application is required"
    assert importlib.util.find_spec("workers.tasks") is not None, "JSON worker tasks are required"

def test_postgres_qmark_translation_preserves_sql_literals_and_comments():
    from database.postgres import convert_sql
    actual = convert_sql("SELECT '?' AS text, ? AS value, '100%' AS pct -- ? comment\n/* ? */ WHERE title LIKE ?")
    assert actual == "SELECT '?' AS text, %s AS value, '100%%' AS pct -- ? comment\n/* ? */ WHERE title LIKE %s"


def test_postgres_replace_is_upsert_and_refuses_unknown_tables():
    from database.postgres import convert_sql
    sql = convert_sql("INSERT OR REPLACE INTO documents(document_id,title) VALUES(?,?)")
    assert sql == "INSERT INTO documents(document_id,title) VALUES(%s,%s) ON CONFLICT (document_id) DO UPDATE SET title=EXCLUDED.title"
    sql = convert_sql("INSERT OR REPLACE INTO document_versions VALUES(?,?,?,?,?,?)")
    assert "ON CONFLICT (document_id,version)" in sql
    with pytest.raises(ValueError, match="unsupported"):
        convert_sql("INSERT OR REPLACE INTO mystery VALUES(?)")
    with pytest.raises(ValueError, match="SQLite"):
        convert_sql("PRAGMA journal_mode=WAL")


def test_postgres_rows_support_integer_names_and_dict_conversion():
    from database.postgres import HybridRow
    row = HybridRow(("id", "title"), (7, "report"))
    assert row[0] == row["id"] == 7
    assert tuple(row) == (7, "report")
    assert dict(row) == {"id": 7, "title": "report"}


def test_claim_serializes_different_jobs_for_same_document(tmp_path):
    from database.jobs import claim_job
    from ingestion.collector import enqueue_file
    path = tmp_path / "report.md"
    path.write_text("v1", encoding="utf8")
    conn = get_conn(":memory:")
    try:
        first = enqueue_file(conn, path)
        token = claim_job(conn, first["job_id"], 3)
        path.write_text("v2", encoding="utf8")
        second = enqueue_file(conn, path)
        assert token and claim_job(conn, second["job_id"], 3) is None
    finally:
        conn.close()


def test_postgres_backend_requires_remote_search_and_dsn(monkeypatch):
    monkeypatch.setenv("RAG_DB_BACKEND", "postgresql")
    monkeypatch.setenv("RAG_SEARCH_BACKEND", "sqlite")
    with pytest.raises(ValueError, match="opensearch"):
        get_conn()
    monkeypatch.setenv("RAG_SEARCH_BACKEND", "opensearch")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("RAG_POSTGRES_DSN", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        get_conn()

def test_celery_uses_json_and_rejects_public_redis(monkeypatch):
    from workers.celery_app import create_celery_app
    monkeypatch.setenv("RAG_REDIS_URL", "redis://127.0.0.1:6379/0")
    app = create_celery_app()
    assert app.conf.task_serializer == app.conf.result_serializer == "json"
    assert app.conf.accept_content == ["json"]
    assert app.conf.task_acks_late and app.conf.worker_prefetch_multiplier == 1
    monkeypatch.setenv("RAG_REDIS_URL", "redis://8.8.8.8:6379/0")
    with pytest.raises(ValueError, match="internal"):
        create_celery_app()
    monkeypatch.setenv("RAG_REDIS_URL", "amqp://localhost")
    with pytest.raises(ValueError, match="Redis"):
        create_celery_app()


def test_celery_job_task_runs_json_payload_with_fresh_connection(tmp_path, monkeypatch):
    from workers.tasks import run_job_task
    from ingestion.collector import enqueue_file
    monkeypatch.setenv("USE_QWEN", "0")
    monkeypatch.setenv("RAG_SEARCH_BACKEND", "sqlite")
    path = tmp_path / "report.md"
    path.write_text("isolated worker document", encoding="utf8")
    conn = get_conn()
    job = enqueue_file(conn, path)
    conn.close()
    result = run_job_task.apply(args=[job["job_id"]], throw=True).result
    assert result["status"] == "DONE"
    import json
    assert json.loads(json.dumps(result))["job_id"] == job["job_id"]
    conn = get_conn()
    assert conn.execute("SELECT status FROM ingestion_jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0] == "DONE"
    conn.close()


def test_collector_supports_ocr_image_formats(tmp_path):
    from ingestion.collector import scan_dir
    docs = tmp_path / "images"
    docs.mkdir()
    for extension in ("png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"):
        (docs / f"page.{extension}").write_bytes(b"isolated image bytes")
    conn = get_conn(":memory:")
    try:
        assert len(scan_dir(conn, docs)) == 7
    finally:
        conn.close()