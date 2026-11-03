import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from database.db import get_conn
from ingestion.collector import enqueue_file
from ingestion.pipeline import run_job, scan_once


def test_abandoned_running_job_is_reclaimed(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_QWEN", "0")
    docs = tmp_path / "docs"
    docs.mkdir()
    p = docs / "report.md"
    p.write_text("시험 기록", encoding="utf8")
    conn = get_conn(str(tmp_path / "t.sqlite3"))
    job = enqueue_file(conn, p)
    conn.execute("UPDATE ingestion_jobs SET status='RUNNING',attempts=1,updated_at='2000-01-01T00:00:00+00:00'")
    conn.commit()
    assert scan_once(str(docs), str(tmp_path / "t.sqlite3"))["done"] == 1
    conn.close()


def test_failed_upload_keeps_previous_original(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_QWEN", "0")
    with TestClient(app) as c:
        first = c.post("/api/v1/documents", files={"f": ("report.md", b"old original")}).json()
        did = first["document_id"]
        result = c.post("/api/v1/documents", files={"f": ("report.md", b"")}).json()
        assert result["status"] == "FAILED"
        assert c.get(f"/api/v1/documents/{did}/download").content == b"old original"
        from app.settings import get_settings
        assert (Path(get_settings().docs) / "report.md").read_bytes() == b"old original"


def test_omitted_upload_acl_preserves_existing_acl(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_QWEN", "0")
    monkeypatch.setenv("RAG_USER_GROUPS", "team")
    with TestClient(app) as c:
        first = c.post("/api/v1/documents", files={"f": ("report.md", b"private original")},
                       data={"user_groups": "team"}).json()
        result = c.post("/api/v1/documents", files={"f": ("report.md", b"changed private")}).json()
        assert result["status"] == "DONE"
    conn = get_conn()
    assert json.loads(conn.execute("SELECT allowed_groups FROM documents WHERE document_id=?",
                                   (first["document_id"],)).fetchone()[0]) == ["team"]
    conn.close()


def test_newer_job_prevents_older_version_committing(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_QWEN", "0")
    p = tmp_path / "report.md"
    p.write_text("old", encoding="utf8")
    c = get_conn(str(tmp_path / "t.sqlite3"))
    first = enqueue_file(c, p)
    run_job(c, first["job_id"])
    p.write_text("older update", encoding="utf8")
    older = enqueue_file(c, p)
    p.write_text("newest update", encoding="utf8")
    newer = enqueue_file(c, p)
    assert run_job(c, newer["job_id"])["status"] == "DONE"
    result = run_job(c, older["job_id"])
    assert result["status"] != "DONE"
    assert c.execute("SELECT text FROM chunks").fetchone()[0] == "newest update"
    c.close()


def test_original_download_matches_active_version_after_external_change(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_QWEN", "0")
    with TestClient(app) as c:
        first = c.post("/api/v1/documents", files={"f": ("report.md", b"active original")}).json()
        from app.settings import get_settings
        (Path(get_settings().docs) / "report.md").write_bytes(b"not yet indexed update")
        assert c.get(f"/api/v1/documents/{first['document_id']}/download").content == b"active original"
