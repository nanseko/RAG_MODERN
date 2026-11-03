"""The LLM may refine search words but must not replace confirmed user observations."""
from fastapi.testclient import TestClient
from app.main import app

def test_query_api_forwards_search_words_separately_from_original_question(monkeypatch):
    import api.routes.query as routes
    def workflow(question, groups, conn, **kwargs):
        assert question == "송신 모듈이 고온에서 3dB 감소합니다. 원인은?"
        assert kwargs["search_query"] == "송신 모듈 온도 출력 감소"
        return {"answer": "근거 부족", "sources": [], "request_id": "intent",
                "retrieval_count": 0, "cycles": 1, "llm_mode": "mock",
                "answer_status": "no_evidence"}
    monkeypatch.setattr(routes, "run_rag", workflow)
    with TestClient(app) as api:
        response = api.post("/api/v1/query", json={
            "query": "송신 모듈이 고온에서 3dB 감소합니다. 원인은?",
            "search_query": "송신 모듈 온도 출력 감소"})
    assert response.status_code == 200
