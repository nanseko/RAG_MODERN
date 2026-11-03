import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.settings import get_settings
from database.db import get_conn
from ingestion.collector import enqueue_file, scan_dir
from ingestion.pipeline import run_job, scan_once
from ingestion.indexer import index_document
from rag.graph import run_rag


def test_scan_does_not_disclose_private_processing_results(monkeypatch):
    monkeypatch.setenv("USE_QWEN", "0")
    monkeypatch.setenv("RAG_USER_GROUPS", "team")
    with TestClient(app) as c:
        assert c.post("/api/v1/documents", files={"f": ("secret.md", b"private")},
                      data={"user_groups": "team"}).json()["status"] == "DONE"
        p = Path(get_settings().docs) / "secret.md"
        p.write_bytes(b"private updated")
        monkeypatch.setenv("RAG_USER_GROUPS", "all")
        out = c.post("/api/v1/scan").json()
        assert "secret.md" not in json.dumps(out)
        assert out["results"] == []


def test_windows_case_rename_preserves_source_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_QWEN", "0")
    p = tmp_path / "Case.md"
    p.write_bytes(b"first")
    conn = get_conn()
    first = enqueue_file(conn, p)
    run_job(conn, first["job_id"])
    changed = tmp_path / "CASE.md"
    p.rename(changed)
    changed.write_bytes(b"second")
    second = enqueue_file(conn, changed)
    assert second["document_id"] == first["document_id"]
    conn.close()


def test_one_unreadable_source_does_not_block_valid_documents(tmp_path, monkeypatch):
    import ingestion.collector as collector
    monkeypatch.setenv("USE_QWEN", "0")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "locked.md").write_bytes(b"locked")
    (docs / "valid.md").write_bytes(b"valid")
    original = collector._hash
    def shared_violation(path):
        if Path(path).name == "locked.md":
            raise PermissionError("Windows sharing violation")
        return original(path)
    monkeypatch.setattr(collector, "_hash", shared_violation)
    out = scan_once(str(docs), str(tmp_path / "t.sqlite3"))
    assert out["done"] == 1
    assert out["failed"] == 1


def test_every_source_shaped_marker_is_validated(monkeypatch):
    monkeypatch.setenv("USE_QWEN", "0")
    conn = get_conn()
    index_document(conn, {"title": "test"}, [{"text": "관련 자료"}])
    class Client:
        def judge(self, question, text):
            return True
        def generate(self, question, sources):
            return "Grounded [SOURCE_01] and invented [SOURCE_FAKE]"
    out = run_rag("질문", ["all"], conn, client=Client(), target_min=1)
    assert out["answer_status"] == "citation_error"
    conn.close()


def test_versioned_download_keeps_the_cited_original(monkeypatch):
    monkeypatch.setenv("USE_QWEN", "0")
    with TestClient(app) as c:
        first = c.post("/api/v1/documents", files={"f": ("report.md", b"version one")}).json()
        did = first["document_id"]
        second = c.post("/api/v1/documents", files={"f": ("report.md", b"version two")}).json()
        assert second["version"] == "v2"
        assert c.get(f"/api/v1/documents/{did}/download?version=v1").content == b"version one"
        assert c.get(f"/api/v1/documents/{did}/download?version=v2").content == b"version two"


def test_failed_initial_job_can_be_explicitly_retried(monkeypatch):
    monkeypatch.setenv("USE_QWEN", "0")
    with TestClient(app) as c:
        failed = c.post("/api/v1/documents", files={"f": ("empty.md", b"")}).json()
        conn = get_conn()
        conn.execute("UPDATE ingestion_jobs SET attempts=3 WHERE job_id=?", (failed["job_id"],))
        conn.commit()
        conn.close()
        out = c.post(f"/api/v1/jobs/{failed['job_id']}/retry")
        assert out.status_code == 200
        assert out.json()["status"] == "FAILED"
        assert out.json()["error"] != "max retries"
