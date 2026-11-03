"""Opt-in internal Redis transport smoke test on an isolated SQL corpus/queue."""
import os
import uuid

import pytest

from database.db import get_conn
from ingestion.collector import enqueue_file


def test_internal_redis_delivers_job_and_json_result(tmp_path, monkeypatch):
    redis_url = os.getenv("RAG_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("RAG_TEST_REDIS_URL is not configured; eager tests do not verify Redis transport")
    monkeypatch.setenv("RAG_REDIS_URL", redis_url)
    monkeypatch.setenv("RAG_REDIS_RESULT_URL", redis_url)
    monkeypatch.setenv("RAG_DB_BACKEND", "sqlite")
    monkeypatch.setenv("RAG_SEARCH_BACKEND", "sqlite")
    monkeypatch.setenv("USE_QWEN", "0")
    from celery.contrib.testing.worker import start_worker
    from workers.celery_app import create_celery_app, celery_app
    from workers.tasks import run_job_task
    configured = create_celery_app()
    queue = "rag_test_" + uuid.uuid4().hex
    # Prefix every transport key; never consume the operating ingestion queue.
    celery_app.conf.update(broker_url=configured.conf.broker_url, result_backend=configured.conf.result_backend,
                          task_default_queue=queue, task_always_eager=False,
                          broker_transport_options={"global_keyprefix": queue + ":", "visibility_timeout": 60},
                          result_backend_transport_options={"global_keyprefix": queue + ":"})
    path = tmp_path / "report.md"
    path.write_text("internal Redis isolated document", encoding="utf8")
    conn = get_conn()
    job = enqueue_file(conn, path)
    conn.close()
    with start_worker(celery_app, pool="solo", concurrency=1, queues=[queue], perform_ping_check=False):
        result = run_job_task.apply_async(args=[job["job_id"]], queue=queue)
        try:
            payload = result.get(timeout=30)
            assert payload["status"] == "DONE" and payload["job_id"] == job["job_id"]
        finally:
            result.forget()
    conn = get_conn()
    assert conn.execute("SELECT status FROM ingestion_jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0] == "DONE"
    conn.close()