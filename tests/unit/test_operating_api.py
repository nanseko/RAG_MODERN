"""Operating configuration, queue dispatch and metadata ACL contracts."""
import json

from fastapi.testclient import TestClient

from app.main import app
from database.db import get_conn
from ingestion.indexer import index_document
from rag.graph import run_rag


def test_preprocess_dispatches_to_queue_without_inline_scan(monkeypatch):
    import api.routes.documents as routes
    import workers.tasks as tasks
    monkeypatch.setenv("USE_QWEN", "0")
    monkeypatch.setenv("RAG_TASK_BACKEND", "celery")
    monkeypatch.setattr(tasks, "dispatch_scan", lambda **kw: "task-one")
    def fail(*args, **kwargs):
        raise AssertionError("Inline scanner must not run in queue mode")
    monkeypatch.setattr(routes, "scan_once", fail)
    with TestClient(app) as client:
        response = client.post("/api/v1/preprocess")
        assert response.status_code == 200
        assert response.json()["status"] == "QUEUED"
        assert response.json()["task_id"] == "task-one"


def test_uploaded_job_dispatched_and_stays_pending(monkeypatch):
    import workers.tasks as tasks
    monkeypatch.setenv("USE_QWEN", "0")
    monkeypatch.setenv("RAG_TASK_BACKEND", "celery")
    calls = []
    monkeypatch.setattr(tasks, "dispatch_job", lambda job_id, **kw: calls.append(job_id) or "task-two")
    with TestClient(app) as client:
        out = client.post("/api/v1/documents", files={"f": ("test.md", b"queue source")}).json()
    assert out["status"] == "QUEUED" and out["task_id"] == "task-two"
    conn = get_conn()
    try:
        row = conn.execute("SELECT status FROM ingestion_jobs WHERE job_id=?", (out["job_id"],)).fetchone()
        assert row["status"] == "PENDING" and calls == [out["job_id"]]
    finally:
        conn.close()


def test_metrics_and_readiness_are_available_without_credentials_in_response():
    with TestClient(app) as client:
        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json()["services"]["database"]["status"] == "ready"
        metrics = client.get("/metrics")
        assert metrics.status_code == 200
        assert "rag_http_request" in metrics.text


def test_authoritative_document_acl_blocks_stale_public_chunks(monkeypatch):
    monkeypatch.setenv("USE_QWEN", "0")
    conn = get_conn()
    try:
        index_document(conn, {"document_id": "one", "title": "secret"}, [{"text": "고온 냉각 후 회복"}])
        conn.execute("UPDATE documents SET allowed_groups=? WHERE document_id='one'", (json.dumps(["private"]),))
        conn.commit()
        class Client:
            def judge(self, *args):
                return True
            def generate(self, *args):
                return "냉각 [SOURCE_01]"
        out = run_rag("고온 해결?", ["all"], conn, client=Client(), target_min=1)
        assert out["answer_status"] == "no_evidence" and out["sources"] == []
    finally:
        conn.close()
