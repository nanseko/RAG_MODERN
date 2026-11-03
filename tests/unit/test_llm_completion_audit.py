import httpx
import pytest
from models.gemma_client import RealGemmaClient

@pytest.mark.parametrize("reason", ["length", "content_filter"])
def test_incomplete_llm_response_is_never_used_as_final_answer(reason):
    http = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"choices": [{"finish_reason": reason,
                               "message": {"content": "해결 절차 일부 [SOURCE_01]"}}]})))
    client = RealGemmaClient("http://internal.local/v1", http=http)
    try:
        with pytest.raises(RuntimeError):
            client.generate("출력 저하 해결", [{"sid": "SOURCE_01", "content": "검증된 해결 절차"}])
    finally:
        http.close()
