"""Operational regressions use temporary SQL/corpus and mocked services only."""
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from database.db import get_conn
from ingestion.collector import enqueue_file


def seed(tmp_path, monkeypatch, failed=False):
    monkeypatch.setenv("USE_QWEN", "0")
    root = tmp_path / "docs"
    root.mkdir()
    monkeypatch.setenv("RAG_DOCS_DIR", str(root))
    p = root / "one.md"
    p.write_text("test source", encoding="utf8")
    conn = get_conn()
    job = enqueue_file(conn, p)
    conn.execute("INSERT INTO documents(document_id,file_name,source_uri,status,allowed_groups) VALUES(?,?,?,'ACTIVE','[\"all\"]')", (job["document_id"], p.name, str(p)))
    conn.execute("UPDATE ingestion_jobs SET status=?,stage='FAILED' WHERE job_id=?", ("FAILED" if failed else "DONE", job["job_id"]))
    conn.commit()
    conn.close()
    return job


@pytest.mark.parametrize("action", ["retry", "reindex"])
def test_retry_and_reindex_respect_celery(tmp_path, monkeypatch, action):
    import api.routes.documents as routes
    job = seed(tmp_path, monkeypatch, failed=action == "retry")
    monkeypatch.setenv("RAG_TASK_BACKEND", "celery")
    monkeypatch.setattr(routes.workers.tasks, "dispatch_job", lambda *a, **kw: "queued-task")
    monkeypatch.setattr(routes, "run_job", lambda *a, **kw: pytest.fail("inline ingestion in Celery mode"))
    path = f"/api/v1/jobs/{job['job_id']}/retry" if action == "retry" else f"/api/v1/documents/{job['document_id']}/reindex"
    out = TestClient(app).post(path)
    assert out.status_code == 200
    assert out.json()["status"] == "QUEUED"
    assert out.json()["task_id"] == "queued-task"


def test_scheduler_dispatches_celery(monkeypatch):
    import ingestion.scheduler as scheduler
    import workers.tasks as tasks
    monkeypatch.setenv("RAG_TASK_BACKEND", "celery")
    monkeypatch.setattr(tasks, "dispatch_scan", lambda: "scan-task")
    monkeypatch.setattr(scheduler, "scan_once", lambda: pytest.fail("inline scheduled ingestion"))
    assert scheduler.DocumentScanner(0).scan() == {"status": "QUEUED", "task_id": "scan-task"}


def test_delete_queues_transactional_search_tombstone(tmp_path, monkeypatch):
    job = seed(tmp_path, monkeypatch)
    response = TestClient(app).delete(f"/api/v1/documents/{job['document_id']}")
    assert response.status_code == 200
    conn = get_conn()
    try:
        row = conn.execute("SELECT operation FROM search_outbox WHERE document_id=?", (job["document_id"],)).fetchone()
        assert row and row[0] == "delete"
    finally:
        conn.close()


def test_status_reports_latest_job_separately_from_active_document(tmp_path, monkeypatch):
    job = seed(tmp_path, monkeypatch, failed=True)
    out = TestClient(app).get(f"/api/v1/documents/{job['document_id']}").json()
    assert out["status"] == "ACTIVE"
    assert out["job_status"] == "FAILED" and out["job_id"] == job["job_id"]


def test_health_reports_selected_backends(monkeypatch):
    monkeypatch.setenv("RAG_DB_BACKEND", "postgresql")
    monkeypatch.setenv("RAG_SEARCH_BACKEND", "opensearch")
    monkeypatch.setenv("RAG_TASK_BACKEND", "celery")
    out = TestClient(app).get("/health").json()
    assert out["vector_store"] == "opensearch"
    assert out["database_backend"] == "postgresql" and out["task_backend"] == "celery"


def test_readiness_rejects_selected_search_failure(monkeypatch):
    from retrieval.opensearch_store import OpenSearchStore
    monkeypatch.setenv("RAG_SEARCH_BACKEND", "opensearch")
    monkeypatch.setattr(OpenSearchStore, "health", lambda self: {"healthy": False, "status": "red"})
    out = TestClient(app).get("/ready")
    assert out.status_code == 503
    assert out.json()["services"]["search"]["status"] == "not_ready"


def test_invalid_task_backend_is_not_silently_inline(monkeypatch):
    import api.routes.documents as routes
    monkeypatch.setenv("RAG_TASK_BACKEND", "celrey")
    with pytest.raises(ValueError, match="RAG_TASK_BACKEND"):
        routes._queue_backend()


def test_metrics_http_counter_changes_after_requests():
    client = TestClient(app)
    def count():
        text = client.get("/metrics").text
        return int(re.search(r'rag_http_request\{route="all"\} (\d+)', text)[1])
    before = count()
    client.get("/")
    client.get("/health")
    assert count() >= before + 2


def test_queue_delivery_failure_preserves_durable_upload(tmp_path, monkeypatch):
    import workers.tasks as tasks
    monkeypatch.setenv("RAG_TASK_BACKEND", "celery")
    def unavailable(*args, **kwargs):
        raise ConnectionError("redis://secret-password@offline")
    monkeypatch.setattr(tasks, "dispatch_job", unavailable)
    out = TestClient(app).post("/api/v1/documents", files={"f": ("queued.md", b"durable")})
    assert out.status_code == 200 and out.json()["status"] == "PENDING"
    assert "secret-password" not in out.text
    conn = get_conn()
    try:
        row = conn.execute("SELECT status,input_path FROM ingestion_jobs WHERE job_id=?", (out.json()["job_id"],)).fetchone()
        from pathlib import Path
        assert row["status"] == "PENDING" and Path(row["input_path"]).read_bytes() == b"durable"
    finally:
        conn.close()


def test_delete_rolls_back_metadata_when_outbox_enqueue_fails(tmp_path, monkeypatch):
    import retrieval.opensearch_store as search
    job = seed(tmp_path, monkeypatch)
    def unavailable(*args):
        raise RuntimeError("outbox failure")
    monkeypatch.setattr(search, "enqueue_delete", unavailable)
    response = TestClient(app, raise_server_exceptions=False).delete(f"/api/v1/documents/{job['document_id']}")
    assert response.status_code == 500
    conn = get_conn()
    try:
        assert conn.execute("SELECT status FROM documents WHERE document_id=?", (job["document_id"],)).fetchone()[0] == "ACTIVE"
    finally:
        conn.close()


def test_readiness_checks_selected_queue_and_sanitizes_error(monkeypatch):
    import app.operations as operations
    monkeypatch.setenv("RAG_TASK_BACKEND", "celery")
    def unavailable():
        raise ConnectionError("redis://secret-password@offline")
    monkeypatch.setattr(operations, "probe_queue", unavailable)
    out = TestClient(app).get("/ready")
    assert out.status_code == 503
    assert out.json()["services"]["queue"]["error"] == "ConnectionError"
    assert "secret-password" not in out.text


def test_readiness_requires_compatible_search_index_without_creating_one(monkeypatch):
    from retrieval.opensearch_store import OpenSearchStore
    monkeypatch.setenv("RAG_SEARCH_BACKEND", "opensearch")
    monkeypatch.setattr(OpenSearchStore, "health", lambda self: {"healthy": True, "status": "green"})
    calls = []
    def request(self, method, path, **kwargs):
        calls.append((method, path))
        return None
    monkeypatch.setattr(OpenSearchStore, "_request", request)
    out = TestClient(app).get("/ready")
    assert out.status_code == 503
    assert calls == [("GET", "/rag-chunks-v1/_mapping")]


def test_failed_scan_does_not_report_old_success(monkeypatch):
    import ingestion.scheduler as scheduler
    scanner = scheduler.DocumentScanner(0)
    monkeypatch.setattr(scheduler, "scan_once", lambda: {"status": "DONE"})
    assert scanner.scan() == {"status": "DONE"}
    def unavailable():
        raise RuntimeError("failed")
    monkeypatch.setattr(scheduler, "scan_once", unavailable)
    assert scanner.scan() is None and scanner.last_error == "RuntimeError"


def test_readiness_fails_missing_selected_local_ocr_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_OCR_MODE", "local")
    monkeypatch.setenv("RAG_OCR_MODEL_DIR", str(tmp_path / "missing-model"))
    monkeypatch.setenv("RAG_OCR_PYTHON_PATH", str(tmp_path / "missing-python.exe"))
    out = TestClient(app).get("/ready")
    assert out.status_code == 503
    assert out.json()["services"]["ocr"]["status"] == "not_ready"


def test_readiness_rejects_invalid_ocr_mode(monkeypatch):
    monkeypatch.setenv("RAG_OCR_MODE", "locla")
    out = TestClient(app).get("/ready")
    assert out.status_code == 503
    assert out.json()["services"]["ocr"]["error"] == "OCRFailure"


def test_status_does_not_expose_private_update_job(tmp_path, monkeypatch):
    job = seed(tmp_path, monkeypatch, failed=True)
    conn = get_conn()
    conn.execute("UPDATE ingestion_jobs SET allowed_groups='[\"private\"]',error='private update text' WHERE job_id=?", (job["job_id"],))
    conn.commit()
    conn.close()
    out = TestClient(app).get(f"/api/v1/documents/{job['document_id']}")
    assert out.status_code == 200 and out.json()["status"] == "ACTIVE"
    assert "private update text" not in out.text
    assert "job_id" not in out.json()


def test_queue_probe_rejects_workers_without_ingestion_queue(monkeypatch):
    from types import SimpleNamespace
    import redis
    import workers.celery_app as queue
    from app.operations import probe_queue
    class Client:
        def ping(self):
            return True
        def close(self):
            pass
    monkeypatch.setattr(redis.Redis, "from_url", lambda *a, **kw: Client())
    inspect = SimpleNamespace(ping=lambda: {"unrelated": {"ok": "pong"}}, active_queues=lambda: {"unrelated": [{"name": "unrelated"}]})
    monkeypatch.setattr(queue.celery_app.control, "inspect", lambda **kw: inspect)
    with pytest.raises(RuntimeError, match="ingestion"):
        probe_queue()
