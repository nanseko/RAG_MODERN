"""Unchanged files parsed with broken heading logic need explicit scan reprocessing."""
from database.db import get_conn
from ingestion.collector import enqueue_file
from ingestion.pipeline import run_job

def test_old_processor_version_requeues_unchanged_file(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_QWEN", "0")
    path = tmp_path / "fault.md"
    path.write_text("# 증상\n출력 감소\n# 원인\n팬 정지\n# 조치\n팬 교체", encoding="utf8")
    conn = get_conn()
    try:
        first = enqueue_file(conn, path)
        assert run_job(conn, first["job_id"])["status"] == "DONE"
        conn.execute("UPDATE ingestion_jobs SET processor_version='ingest-v3' WHERE job_id=?", (first["job_id"],))
        conn.commit()
        replacement = enqueue_file(conn, path)
        assert replacement is not None
        assert replacement["document_id"] == first["document_id"]
    finally:
        conn.close()
