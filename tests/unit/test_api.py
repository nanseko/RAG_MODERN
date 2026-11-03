from fastapi.testclient import TestClient
from app.main import app


def test_root_links_exist():
    c = TestClient(app)
    r = c.get("/")
    assert r.status_code == 200
    assert {"health", "docs", "query"} <= set(r.json())


def test_query_no_evidence_shape():
    c = TestClient(app)
    r = c.post("/api/v1/query", json={"query": "존재하지 않는 X 장비 2035년 시험결과"})
    assert r.status_code == 200
    body = r.json()
    assert {"answer", "sources", "request_id", "retrieval_count"} <= set(body)


def test_scan_endpoint_processes_file(tmp_path, monkeypatch):
    import os
    d = tmp_path / "docs"
    d.mkdir()
    with open(d / "s.md", "w", encoding="utf-8") as f:
        f.write("스캔 본문 기록")
    monkeypatch.setenv("RAG_DOCS_DIR", str(d))
    monkeypatch.setenv("RAG_DB_PATH", str(tmp_path / "s.sqlite3"))
    c = TestClient(app)
    r = c.post("/api/v1/scan")
    assert r.status_code == 200
    assert r.json()["done"] == 1
