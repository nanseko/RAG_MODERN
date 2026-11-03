"""Native worker contracts against isolated SQL and small text documents."""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

from database.db import get_conn
from ingestion.collector import enqueue_file


def test_native_backend_is_explicit_and_async(monkeypatch):
    from app.operations import queue_selected, selected_backends
    monkeypatch.setenv("RAG_TASK_BACKEND", "native")
    assert selected_backends()["RAG_TASK_BACKEND"] == "native"
    assert queue_selected()


def native_modules():
    assert importlib.util.find_spec("workers.native_queue"), "native SQL queue is required"
    assert importlib.util.find_spec("workers.native_worker"), "native worker is required"
    from workers import native_queue, native_worker
    return native_queue, native_worker


def test_scan_requests_survive_connections_and_coalesce(tmp_path):
    queue, _ = native_modules()
    conn = get_conn()
    queue.initialize(conn)
    task_id = queue.enqueue_command(conn, "scan", {"docs_dir": str(tmp_path)})
    assert queue.enqueue_command(conn, "scan", {"docs_dir": str(tmp_path)}) == task_id
    conn.close()
    conn = get_conn()
    try:
        command = queue.claim_command(conn, now=100)
        assert command["task_id"] == task_id and command["attempts"] == 1
        assert json.loads(command["payload"])["docs_dir"] == str(tmp_path)
        assert queue.claim_command(conn, now=101) is None
        replacement = queue.claim_command(conn, now=200)
        assert replacement["task_id"] == task_id and replacement["claim_token"] != command["claim_token"]
        assert not queue.finish_command(conn, command, result={"status": "DONE"})
        assert queue.finish_command(conn, replacement, result={"status": "DONE"})
    finally:
        conn.close()


def test_worker_heartbeat_requires_fresh_live_owner():
    queue, _ = native_modules()
    conn = get_conn()
    try:
        queue.initialize(conn)
        with pytest.raises(RuntimeError, match="worker"):
            queue.worker_health(conn, now=100)
        assert queue.acquire_worker(conn, "one", now=100)
        assert not queue.acquire_worker(conn, "two", now=101)
        assert queue.worker_health(conn, now=101)["workers"] == 1
        with pytest.raises(RuntimeError, match="worker"):
            queue.worker_health(conn, now=200)
        assert queue.acquire_worker(conn, "two", now=200)
        assert not queue.heartbeat(conn, "one", now=201)
        assert queue.heartbeat(conn, "two", now=201)
    finally:
        conn.close()


def test_worker_processes_sql_jobs_and_durable_scan_without_broker(tmp_path, monkeypatch):
    queue, module = native_modules()
    monkeypatch.setenv("USE_QWEN", "0")
    root = tmp_path / "docs"
    root.mkdir()
    monkeypatch.setenv("RAG_DOCS_DIR", str(root))
    (root / "report.md").write_text("native durable test report", encoding="utf8")
    conn = get_conn()
    queue.initialize(conn)
    task_id = queue.enqueue_command(conn, "scan", {"docs_dir": str(root)})
    conn.close()
    worker = module.NativeWorker()
    try:
        assert worker.acquire()
        outcome = worker.tick()
        assert outcome["processed"] == 1
        conn = get_conn()
        try:
            assert conn.execute("SELECT status FROM ingestion_jobs").fetchone()[0] == "DONE"
            assert conn.execute("SELECT status FROM native_commands WHERE task_id=?", (task_id,)).fetchone()[0] == "DONE"
            assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] > 0
        finally:
            conn.close()
    finally:
        worker.close()


def test_worker_recovers_expired_jobs_and_bounds_retry_attempts(tmp_path, monkeypatch):
    _, module = native_modules()
    monkeypatch.setenv("USE_QWEN", "0")
    source = tmp_path / "missing.md"
    source.write_text("temporary", encoding="utf8")
    conn = get_conn()
    job = enqueue_file(conn, source)
    conn.execute("UPDATE ingestion_jobs SET status='RUNNING', attempts=1, lease_expires_at='2000-01-01T00:00:00+00:00' WHERE job_id=?", (job["job_id"],))
    conn.commit()
    conn.close()
    source.unlink()
    worker = module.NativeWorker(retry_delay=0)
    try:
        assert worker.acquire()
        assert worker.tick()["recovered"] == 1
        worker.tick()
        assert worker.tick()["processed"] == 0
        conn = get_conn()
        try:
            row = conn.execute("SELECT status,attempts FROM ingestion_jobs WHERE job_id=?", (job["job_id"],)).fetchone()
            assert row["status"] == "FAILED" and row["attempts"] == 3
        finally:
            conn.close()
    finally:
        worker.close()


def test_api_native_upload_is_durable_and_readiness_requires_worker(monkeypatch):
    native_modules()
    from fastapi.testclient import TestClient
    from app.main import app
    monkeypatch.setenv("RAG_TASK_BACKEND", "native")
    client = TestClient(app)
    response = client.post("/api/v1/documents", files={"f": ("pending.md", b"durable native upload")})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "QUEUED" and body["task_id"] == body["job_id"]
    conn = get_conn()
    try:
        assert conn.execute("SELECT status FROM ingestion_jobs WHERE job_id=?", (body["job_id"],)).fetchone()[0] == "PENDING"
    finally:
        conn.close()
    ready = client.get("/ready")
    assert ready.status_code == 503 and ready.json()["services"]["queue"]["status"] == "not_ready"


def test_native_path_imports_no_celery_or_redis_and_cli_exits_cleanly(tmp_path):
    native_modules()
    env = dict(os.environ, RAG_TASK_BACKEND="native", USE_QWEN="0", PYTHON_DOTENV_DISABLED="1")
    code = "from app.main import app; from workers.native_worker import main; import sys; assert not any(x == 'celery' or x.startswith('celery.') or x == 'redis' or x.startswith('redis.') for x in sys.modules); main(['--once'])"
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_periodic_scan_runs_without_startup_autoscan(tmp_path, monkeypatch):
    _, module = native_modules()
    monkeypatch.setenv("USE_QWEN", "0")
    monkeypatch.setenv("RAG_AUTOSCAN", "0")
    monkeypatch.setenv("RAG_SCAN_INTERVAL_MIN", "1")
    root = tmp_path / "periodic"
    root.mkdir()
    monkeypatch.setenv("RAG_DOCS_DIR", str(root))
    clock = [10]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    worker = module.NativeWorker()
    try:
        assert worker.acquire()
        assert worker.tick()["processed"] == 0
        (root / "later.md").write_text("periodic scan while API is offline", encoding="utf8")
        clock[0] = 71
        assert worker.tick()["processed"] == 1
    finally:
        worker.close()


def test_stop_file_retains_pending_jobs_and_marks_worker_unready(tmp_path):
    queue, module = native_modules()
    stop_file = tmp_path / "native.stop"
    source = tmp_path / "pending.md"
    source.write_text("never process during shutdown", encoding="utf8")
    conn = get_conn()
    job = enqueue_file(conn, source)
    conn.close()
    worker = module.NativeWorker(stop_file=stop_file)
    try:
        assert worker.acquire()
        stop_file.touch()
        assert worker.tick()["processed"] == 0
        conn = get_conn()
        try:
            with pytest.raises(RuntimeError, match="stopping"):
                queue.worker_health(conn)
            assert conn.execute("SELECT status FROM ingestion_jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0] == "PENDING"
        finally:
            conn.close()
    finally:
        worker.close()


def test_blocked_document_does_not_starve_another_document(tmp_path, monkeypatch):
    _, module = native_modules()
    from database.jobs import claim_job
    monkeypatch.setenv("USE_QWEN", "0")
    first = tmp_path / "first.md"
    first.write_text("old content", encoding="utf8")
    other = tmp_path / "other.md"
    other.write_text("independent report", encoding="utf8")
    conn = get_conn()
    active = enqueue_file(conn, first)
    assert claim_job(conn, active["job_id"])
    first.write_text("new content", encoding="utf8")
    blocked = enqueue_file(conn, first)
    independent = enqueue_file(conn, other)
    conn.close()
    worker = module.NativeWorker()
    try:
        assert worker.acquire()
        assert worker.tick()["processed"] == 1
        conn = get_conn()
        try:
            assert conn.execute("SELECT status FROM ingestion_jobs WHERE job_id=?", (blocked["job_id"],)).fetchone()[0] == "PENDING"
            assert conn.execute("SELECT status FROM ingestion_jobs WHERE job_id=?", (independent["job_id"],)).fetchone()[0] == "DONE"
        finally:
            conn.close()
    finally:
        worker.close()


def test_pipeline_reports_exact_acquired_claim_for_heartbeat(tmp_path, monkeypatch):
    from ingestion.pipeline import run_job
    monkeypatch.setenv("USE_QWEN", "0")
    source = tmp_path / "claim.md"
    source.write_text("claim observer text", encoding="utf8")
    conn = get_conn()
    job = enqueue_file(conn, source)
    observed = []
    def on_claim(token):
        row = conn.execute("SELECT status,claim_token FROM ingestion_jobs WHERE job_id=?", (job["job_id"],)).fetchone()
        assert row["status"] == "RUNNING" and row["claim_token"] == token
        observed.append(token)
    try:
        assert run_job(conn, job["job_id"], on_claim=on_claim)["status"] == "DONE"
        assert len(observed) == 1 and observed[0]
    finally:
        conn.close()


def test_heartbeat_cannot_renew_replaced_job_claim(tmp_path):
    _, module = native_modules()
    from database.jobs import claim_job, reclaim_expired
    source = tmp_path / "claim.md"
    source.write_text("job token fencing", encoding="utf8")
    conn = get_conn()
    try:
        job = enqueue_file(conn, source)
        old = claim_job(conn, job["job_id"])
        conn.execute("UPDATE ingestion_jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE job_id=?", (job["job_id"],))
        conn.commit()
        assert reclaim_expired(conn) == 1
        current = claim_job(conn, job["job_id"])
        marker = "2001-01-01T00:00:00+00:00"
        conn.execute("UPDATE ingestion_jobs SET lease_expires_at=? WHERE job_id=?", (marker, job["job_id"]))
        conn.commit()
        worker = module.NativeWorker()
        worker._active_job = (job["job_id"], old)
        worker._renew_leases(conn)
        assert conn.execute("SELECT lease_expires_at FROM ingestion_jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0] == marker
        worker._active_job = (job["job_id"], current)
        worker._renew_leases(conn)
        assert conn.execute("SELECT lease_expires_at FROM ingestion_jobs WHERE job_id=?", (job["job_id"],)).fetchone()[0] > marker
    finally:
        conn.close()
