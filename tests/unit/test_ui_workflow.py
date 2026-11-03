"""Exercise the actual Streamlit script and verify answers survive reruns."""
import json

import requests
from streamlit.testing.v1 import AppTest


def test_answer_and_sources_survive_streamlit_rerun(monkeypatch):
    def http(method, url, **kwargs):
        if url.endswith("/health"):
            body = {"llm_mode": "mock", "embedding_mode": "qwen"}
        elif url.endswith("/chat"):
            body = {"type": "ready", "message": "사내 검색을 시작합니다.", "analysis": "",
                    "refined_query": kwargs["json"]["query"]}
        else:
            body = {"answer": "확인된 시험 결과", "answer_status": "answered", "llm_mode": "mock",
                    "retrieval_count": 1, "cycles": 2, "total_latency": 1, "rejected_count": 1,
                    "sources": [{"sid": "SOURCE_01", "document_id": "d1", "title": "시험자료.md",
                                 "version": "v2", "content": "출력 감소 기록", "pages": [1],
                                 "download_url": "/api/v1/documents/d1/download",
                                 "markdown_url": "/api/v1/documents/d1/markdown"}]}
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(body, ensure_ascii=False).encode()
        return response
    monkeypatch.setattr(requests, "request", http)
    from pathlib import Path
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "ui/streamlit_app.py")).run()
    assert not app.exception
    app.text_input[0].set_value("고온시험")
    next(b for b in app.button if b.label == "질문하기").click()
    app.run()
    assert not app.exception
    assert any("확인된 시험 결과" in item.value for item in app.markdown)
    app.run()
    assert any("시험자료.md" in item.value for item in app.markdown)
    assert any("확인된 시험 결과" in item.value for item in app.markdown)
