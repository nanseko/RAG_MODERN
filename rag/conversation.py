"""Analyze symptoms before retrieval; live failures never become mock decisions."""
from ingestion.metadata import extract_identifiers

SEARCH_NOTICE = "사내 RAG에서 관련된 이슈 사항이 있었는지 찾아볼게요."
SYMPTOMS = ("출력", "저하", "감소", "오류", "발생", "고장", "이상", "열", "소음",
            "진동", "불량", "끊김", "과열", "누설", "파손", "오동작", "알람", "경보")
CLARIFY_MSG = ("현상을 좀 더 확인할게요. 어느 장비/모듈에서 발생했나요? "
               "부품번호나 오류코드, 발생 조건을 함께 적어주세요.")

def _user_texts(question, history):
    parts = [m.get("content", "") for m in (history or []) if m.get("role") == "user"]
    if not parts or parts[-1] != question:
        parts.append(question or "")
    return "\n".join(p for p in parts if p)

def _slots_ready(text):
    ids = extract_identifiers(text)
    has_symptom = any(s in text for s in SYMPTOMS)
    has_target = bool(ids["part_numbers"] or ids["error_codes"] or ids["equipment"]) or any(
        word in text for word in ("장비", "모듈", "송신", "수신", "펌프", "모터", "센서"))
    return has_symptom and (has_target or len(text) >= 30)

def validate_analysis(value):
    """Fail closed: malformed LLM/UI contracts must not start retrieval."""
    if not isinstance(value, dict) or value.get("type") not in ("clarify", "ready"):
        raise RuntimeError("LLM 현상 분석 상태가 올바르지 않습니다")
    out = dict(value)
    message = out.get("message")
    if not isinstance(message, str) or not message.strip() or len(message) > 4000:
        raise RuntimeError("LLM 현상 분석 메시지가 올바르지 않습니다")
    analysis = out.get("analysis", "")
    if not isinstance(analysis, str) or len(analysis) > 4000:
        raise RuntimeError("LLM 현상 분석 요약이 올바르지 않습니다")
    if out["type"] == "ready":
        query = out.get("refined_query")
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise RuntimeError("LLM 검색 질문이 올바르지 않습니다")
        out["refined_query"] = query.strip()
        out["message"] = SEARCH_NOTICE
    return out

def analyze(question, history, client):
    fn = getattr(client, "analyze", None)
    if callable(fn):
        return validate_analysis(fn(question, history))
    text = _user_texts(question, history)
    if _slots_ready(text):
        return {"type": "ready", "message": SEARCH_NOTICE, "analysis": "",
                "refined_query": text.replace("\n", " ")}
    return {"type": "clarify", "message": CLARIFY_MSG, "analysis": ""}
