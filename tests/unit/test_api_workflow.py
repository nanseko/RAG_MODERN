import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from database.db import get_conn


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_QWEN", "0")
    monkeypatch.setenv("RAG_DB_PATH", str(tmp_path / "rag.sqlite3"))
    monkeypatch.setenv("RAG_DOCS_DIR", str(tmp_path / "docs"))
    monkeypatch.setenv("RAG_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("RAG_SCAN_INTERVAL_MIN", "0")
    monkeypatch.setenv("RAG_AUTOSCAN", "0")
    with TestClient(app) as client:
        yield client, tmp_path


def test_upload_and_query_use_the_configured_database(api):
    c, tmp = api
    uploaded = c.post("/api/v1/documents", files={"f": ("sample.md", "고온시험 출력 감소 기록".encode(), "text/markdown")})
    assert uploaded.status_code == 200
    assert uploaded.json()["status"] == "DONE"
    assert (tmp / "docs/sample.md").is_file()
    result = c.post("/api/v1/query", json={"query": "고온시험 출력"})
    assert result.status_code == 200
    assert result.json()["sources"][0]["document_id"] == uploaded.json()["document_id"]


@pytest.mark.parametrize("filename", ["../escape.md", "..\\escape.md"])
def test_upload_cannot_write_outside_docs(api, filename):
    c, tmp = api
    result = c.post("/api/v1/documents", files={"f": (filename, b"bad")})
    assert result.status_code == 400
    assert not (tmp / "escape.md").exists()


def test_unsupported_upload_is_rejected(api):
    c, _ = api
    assert c.post("/api/v1/documents", files={"f": ("a.exe", b"bad")}).status_code == 415


def test_source_original_and_markdown_can_be_downloaded(api):
    c, _ = api
    uploaded = c.post("/api/v1/documents", files={"f": ("sample.md", "고온시험 기록".encode())}).json()
    did = uploaded["document_id"]
    original = c.get(f"/api/v1/documents/{did}/download")
    markdown = c.get(f"/api/v1/documents/{did}/markdown")
    assert original.status_code == markdown.status_code == 200
    assert "고온시험 기록" in original.text and "고온시험 기록" in markdown.text


def test_blank_question_is_rejected(api):
    c, _ = api
    assert c.post("/api/v1/query", json={"query": "  "}).status_code == 422


def test_llm_timeout_returns_clear_504(api, monkeypatch):
    import api.routes.query as routes
    c, _ = api
    def fail(*args, **kwargs):
        raise httpx.ReadTimeout("LLM timeout")
    monkeypatch.setattr(routes, "run_rag", fail)
    result = c.post("/api/v1/query", json={"query": "고온시험"})
    assert result.status_code == 504
    assert "detail" in result.json()


def test_client_cannot_grant_itself_private_groups(api):
    import json
    from ingestion.indexer import index_document
    c, tmp = api
    conn = get_conn(str(tmp / "rag.sqlite3"))
    index_document(conn, {"title": "비공개", "allowed_groups": ["secret"]},
                   [{"text": "고온시험 비공개 기록"}])
    conn.close()
    out = c.post("/api/v1/query", json={"query": "고온시험", "user_groups": ["secret"]}).json()
    assert out["sources"] == []


def test_status_exposes_pending_failure_without_document(api):
    c, tmp = api
    (tmp / "docs").mkdir(exist_ok=True)
    (tmp / "docs/empty.txt").write_text("", encoding="utf8")
    scanned = c.post("/api/v1/scan").json()
    did = scanned["results"][0]["document_id"]
    status = c.get(f"/api/v1/documents/{did}").json()
    assert status["status"] == "FAILED"
    assert status["error"]


def test_periodic_worker_survives_scan_exception(monkeypatch):
    import ingestion.scheduler as scheduler
    import threading
    calls = []
    finished = threading.Event()
    def scan():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("temporary scan error")
        finished.set()
        return {"done": 0, "failed": 0}
    monkeypatch.setattr(scheduler, "scan_once", scan)
    worker = scheduler.DocumentScanner(interval_seconds=0.01)
    worker.start()
    try:
        assert finished.wait(2)
        assert len(calls) >= 2
    finally:
        worker.stop()
    assert not worker.thread.is_alive()
