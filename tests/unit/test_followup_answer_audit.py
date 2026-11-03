from tests.unit.test_conversation_ui_audit import ui, submit

def test_followup_analysis_receives_previous_solution_not_just_search_notice(monkeypatch):
    app, calls = ui(monkeypatch, [
        {"type": "ready", "message": "검색 시작", "analysis": "", "refined_query": "출력 저하"},
        {"type": "clarify", "message": "조치 후 출력이 어떻게 변했나요?", "analysis": ""}])
    submit(app, "송신 모듈 출력 저하")
    submit(app, "그 방법을 적용했어요")
    chats = [kw["json"] for url, kw in calls if url.endswith("/chat")]
    assert any(m["role"] == "assistant" and "근거 있는 답변" in m["content"]
               for m in chats[1]["history"])
