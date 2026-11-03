"""Run the real question UI: clarify first, clear stale results and reset cases."""
import json
from pathlib import Path
import requests
from streamlit.testing.v1 import AppTest

ANSWER = {"answer": "근거 있는 답변", "answer_status": "answered", "llm_mode": "mock",
          "retrieval_count": 0, "cycles": 1, "total_latency": 1,
          "rejected_count": 0, "sources": [], "request_id": "test"}
def ui(monkeypatch, replies):
    calls = []
    def http(method, url, **kw):
        calls.append((url, kw))
        if url.endswith("/health"):
            data = {"llm_mode": "mock", "embedding_mode": "qwen"}
        elif url.endswith("/chat"):
            data = replies.pop(0)
        elif url.endswith("/scan"):
            data = {"status": "QUEUED", "task_id": "queued-one"}
        else:
            data = ANSWER
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(data, ensure_ascii=False).encode()
        return response
    monkeypatch.setattr(requests, "request", http)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[2] / "ui/streamlit_app.py")).run()
    return app, calls

def submit(app, question):
    app.text_input[0].set_value(question)
    next(b for b in app.button if b.label == "질문하기").click()
    app.run()
    return app

def test_clarification_does_not_query_or_keep_old_answer(monkeypatch):
    app, calls = ui(monkeypatch, [{"type": "clarify", "message": "발생 조건이 무엇인가요?",
                                 "analysis": "온도에 따른 변화는 아직 확인되지 않았습니다."}])
    app.session_state["answer"] = ANSWER
    submit(app, "출력 저하")
    assert not app.exception
    assert not any(url.endswith("/query") for url, _ in calls)
    assert not any("근거 있는 답변" in item.value for item in app.markdown)
    assert any("온도에 따른 변화" in item.value for item in app.markdown)

def test_followup_sends_prior_context_once_and_searches_only_when_ready(monkeypatch):
    app, calls = ui(monkeypatch, [
        {"type": "clarify", "message": "온도 조건은?", "analysis": "송신 모듈 출력 저하를 확인했습니다."},
        {"type": "ready", "message": "사내 RAG에서 관련된 이슈 사항이 있었는지 찾아볼게요.",
         "analysis": "고온 80도에서 출력이 감소합니다.", "refined_query": "송신 모듈 80도 출력 저하"}])
    submit(app, "송신 모듈 출력 저하")
    submit(app, "80도입니다")
    assert not app.exception
    chats = [kw["json"] for url, kw in calls if url.endswith("/chat")]
    assert all(m["content"] != "송신 모듈 출력 저하" for m in chats[0]["history"])
    assert any(m["content"] == "송신 모듈 출력 저하" for m in chats[1]["history"])
    query = [kw["json"] for url, kw in calls if url.endswith("/query")]
    assert query == [{"query": "송신 모듈 출력 저하\n80도입니다",
                      "search_query": "송신 모듈 80도 출력 저하"}]
    assert any("고온 80도에서" in item.value for item in app.markdown)
    assert not any('class="library"' in item.value for item in app.markdown)

def test_malformed_chat_response_never_starts_search(monkeypatch):
    app, calls = ui(monkeypatch, [{"answer": "unexpected response"}])
    submit(app, "출력 저하")
    assert not app.exception
    assert not any(url.endswith("/query") for url, _ in calls)
    assert app.error

def test_new_case_clears_context_answer_and_cached_sources(monkeypatch):
    app, calls = ui(monkeypatch, [{"type": "clarify", "message": "어느 장비인가요?", "analysis": ""}])
    submit(app, "출력 저하")
    app.session_state["answer"] = ANSWER
    app.session_state["source_downloads"] = {"old": b"secret"}
    next(b for b in app.button if b.label == "새 현상 상담").click()
    app.run()
    assert not app.exception
    assert not app.session_state["chat_history"]
    assert not app.session_state["source_downloads"]
    assert "answer" not in app.session_state

def test_queued_preprocessing_displays_pending_instead_of_done(monkeypatch):
    app, _ = ui(monkeypatch, [])
    next(b for b in app.button if b.label == "전처리 실행").click()
    app.run()
    assert not app.exception
    assert any("대기열" in item.value for item in app.info)
