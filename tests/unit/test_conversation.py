from models.gemma_client import MockGemmaClient
from rag.conversation import analyze


def test_vague_symptom_asks_clarification():
    out = analyze("출력 이상 있어요", [], MockGemmaClient())
    assert out["type"] == "clarify"
    assert "장비" in out["message"] or "부품" in out["message"]


def test_detailed_query_is_ready():
    out = analyze("ABC-1234 부품에서 ERR_TX_041 발생 후 출력이 3dB 감소했습니다",
                  [], MockGemmaClient())
    assert out["type"] == "ready"


def test_followup_answer_completes_phenomenon():
    history = [{"role": "assistant", "content": "어느 장비에서 발생했나요?"},
               {"role": "user", "content": "레이더 송신 모듈이고 열이 많이 납니다"}]
    out = analyze("레이더 송신 모듈이고 열이 많이 납니다", history, MockGemmaClient())
    assert out["type"] == "ready"
    assert "레이더" in out["refined_query"]
