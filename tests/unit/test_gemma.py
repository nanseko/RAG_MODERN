import httpx

from models.gemma_client import RealGemmaClient


def _stub_client(payload: dict, status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(status, json=payload)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_real_gemma_returns_content():
    payload = {"choices": [{"message": {"content": "요약: 결과 [SOURCE_01]"}}]}
    c = RealGemmaClient("http://gemma.local/v1", api_key="test-key",
                        http=_stub_client(payload))
    out = c.generate("질문", [{"sid": "SOURCE_01"}])
    assert out == "요약: 결과 [SOURCE_01]"


def test_real_gemma_sends_system_and_sources():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        seen.update(_json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    c = RealGemmaClient("http://gemma.local/v1",
                        http=httpx.Client(transport=httpx.MockTransport(handler)))
    c.generate("고온 출력?", [{"sid": "SOURCE_01", "title": "보고서",
                               "version": "Rev.C", "page": 32,
                               "content": "출력 감소"}])
    sys_msg, user_msg = seen["messages"][0]["content"], seen["messages"][-1]["content"]
    assert "SOURCE 범위 내" in sys_msg
    assert "보고서" in user_msg and "출력 감소" in user_msg


def test_real_gemma_raises_on_server_error():
    c = RealGemmaClient("http://gemma.local/v1",
                        http=_stub_client({"error": "busy"}, status=500))
    try:
        c.generate("질문", [{"sid": "SOURCE_01"}])
        raised = False
    except Exception:
        raised = True
    assert raised
