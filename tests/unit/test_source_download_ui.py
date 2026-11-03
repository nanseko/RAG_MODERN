"""Verify visible, unique downloads and authenticated version retention in the real UI."""
import json
from pathlib import Path

import requests
from streamlit.testing.v1 import AppTest


def app_test(monkeypatch, token=""):
    monkeypatch.setenv("RAG_API_TOKEN", token)
    seen = []
    source = {"sid": "SOURCE_01", "document_id": "one", "title": "report.docx", "version": "v3",
              "content": "냉각 후 회복", "pages": [4],
              "download_url": "/api/v1/documents/one/download?version=v3",
              "markdown_url": "/api/v1/documents/one/markdown?version=v3"}
    def http(method, url, **kwargs):
        seen.append((method, url, kwargs))
        response = requests.Response()
        response.status_code = 200
        if "/download?" in url or "/markdown?" in url:
            response._content = b"version three"
            response.headers["Content-Type"] = "application/octet-stream"
        else:
            if url.endswith("/health"):
                body = {"llm_mode": "mock", "embedding_mode": "qwen"}
            elif url.endswith("/chat"):
                body = {"type": "ready", "message": "검색을 시작합니다.", "analysis": "",
                        "refined_query": kwargs["json"]["query"]}
            else:
                body = {"answer": "냉각 확인", "answer_status": "answered", "llm_mode": "mock",
                        "answer_review_status": "mock", "retrieval_count": 1, "cycles": 1,
                        "total_latency": 1, "rejected_count": 0, "sources": [source, source],
                        "request_id": "request-one"}
            response._content = json.dumps(body, ensure_ascii=False).encode()
        return response
    monkeypatch.setattr(requests, "request", http)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "ui/streamlit_app.py")).run()
    app.text_input[0].set_value("출력 저하 해결?")
    next(b for b in app.button if b.label == "질문하기").click()
    app.run()
    return app, seen


def test_unique_download_urls_visible_without_opening_evidence(monkeypatch):
    app, _ = app_test(monkeypatch)
    assert not app.exception
    links = app.get("link_button")
    assert len(links) == 2
    assert {link.proto.url for link in links} == {
        "http://localhost:8000/api/v1/documents/one/download?version=v3",
        "http://localhost:8000/api/v1/documents/one/markdown?version=v3"}
    assert all(not expander.get("link_button") for expander in app.expander)
    assert sum("report.docx" in item.value for item in app.markdown) == 1


def test_authenticated_download_retains_cited_version_and_survives_rerun(monkeypatch):
    app, seen = app_test(monkeypatch, token="local-test-key")
    assert not app.exception
    prepare = next(b for b in app.button if b.label == "다운로드 준비")
    prepare.click()
    app.run()
    assert not app.exception
    downloads = app.get("download_button")
    assert len(downloads) == 2
    requests_seen = [(url, kw) for method, url, kw in seen if "/documents/one/" in url]
    assert len(requests_seen) == 2
    assert all(url.endswith("?version=v3") for url, _ in requests_seen)
    assert all(kw["headers"]["Authorization"] == "Bearer local-test-key" for _, kw in requests_seen)
    app.run()
    assert not app.exception
    assert len(app.get("download_button")) == 2
