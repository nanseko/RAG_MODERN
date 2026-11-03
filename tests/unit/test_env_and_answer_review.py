"""Configuration and answer-quality regressions; no live LLM requests."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.settings import get_settings
from database.db import get_conn
from ingestion.indexer import index_document
from models.gemma_client import RealGemmaClient, get_client
from rag.graph import run_rag


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / "configured.env"
    monkeypatch.setenv("RAG_ENV_FILE", str(path))
    for name in ("GEMMA_API_URL", "GEMMA_API_KEY", "GEMMA_MODEL", "GEMMA_TIMEOUT", "GEMMA_MAX_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    return path


def test_dotenv_selects_live_client_with_literal_credentials(env_file):
    secret = "literal-$" + "{UNCHANGED}#key"
    env_file.write_text(
        "GEMMA_API_URL=http://company-llm/v1\n"
        "GEMMA_MODEL=internal-model\n"
        f"GEMMA_API_KEY='{secret}'\n"
        "GEMMA_TIMEOUT=45\nGEMMA_MAX_TOKENS=800\n", encoding="utf-8-sig")
    client = get_client()
    try:
        assert client.mode == "live"
        assert client.endpoint == "http://company-llm/v1/chat/completions"
        assert client.model == "internal-model"
        assert client.api_key == secret
        assert client.timeout == 45 and client.max_tokens == 800
    finally:
        client.close()


def test_shell_environment_overrides_dotenv(env_file, monkeypatch):
    env_file.write_text("GEMMA_API_URL=http://file/v1\nGEMMA_MODEL=file-model\n", encoding="utf8")
    monkeypatch.setenv("GEMMA_API_URL", "http://shell/v1")
    monkeypatch.setenv("GEMMA_MODEL", "shell-model")
    client = get_client()
    try:
        assert client.endpoint == "http://shell/v1/chat/completions"
        assert client.model == "shell-model"
    finally:
        client.close()


def test_dotenv_shared_by_settings_and_api_without_leaking_credentials(env_file, tmp_path, monkeypatch):
    docs = tmp_path / "configured-docs"
    env_file.write_text(
        f"RAG_DOCS_DIR={docs.as_posix()}\nGEMMA_API_URL=http://company/v1\n"
        "GEMMA_MODEL=private-model\nGEMMA_API_KEY=private-test-key\n"
        "RAG_API_TOKEN=local-test-token\n", encoding="utf8")
    monkeypatch.delenv("RAG_DOCS_DIR")
    assert get_settings().docs == str(docs)
    with TestClient(app) as c:
        health = c.get("/health").json()
        assert health["llm_mode"] == "live" and health["llm_model"] == "private-model"
        assert "private-test-key" not in json.dumps(health)
        assert c.get("/api/v1/documents").status_code == 401
        assert c.get("/api/v1/documents", headers={"Authorization": "Bearer local-test-token"}).status_code == 200


def test_missing_dotenv_remains_explicit_mock(env_file):
    assert get_client().mode == "mock"


def test_generation_requests_evidence_based_solution_and_validation():
    seen = {}
    def endpoint(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "방열 확인 [SOURCE_01]"}}]})
    client = RealGemmaClient("http://llm/v1", http=httpx.Client(transport=httpx.MockTransport(endpoint)))
    client.generate("출력이 감소하면 어떻게 해결하나요?",
                    [{"sid": "SOURCE_01", "title": "시험", "content": "냉각 후 회복",
                      "version": "v2", "page": 4}])
    prompt = "\n".join(m["content"] for m in seen["messages"])
    assert all(term in prompt for term in ("해결책", "검증 방법", "가설", "질문"))
    assert "냉각 후 회복" in prompt and "SOURCE_01" in prompt


def test_real_client_reviews_question_draft_and_same_evidence():
    seen = {}
    verdict = {"grounded": True, "answers_question": True, "logically_consistent": True, "issues": []}
    def endpoint(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(verdict)}}]})
    client = RealGemmaClient("http://llm/v1", http=httpx.Client(transport=httpx.MockTransport(endpoint)))
    out = client.review("고온 출력 저하 해결?", "방열을 점검합니다 [SOURCE_01]",
                        [{"sid": "SOURCE_01", "content": "냉각 후 회복", "version": "v2"}])
    assert out == verdict
    user = json.loads(seen["messages"][-1]["content"])
    assert user["question"] == "고온 출력 저하 해결?"
    assert "방열" in user["answer"] and user["sources"][0]["content"] == "냉각 후 회복"


@pytest.mark.parametrize("content", [
    "yes",
    '{"grounded":"true","answers_question":true,"logically_consistent":true,"issues":[]}',
    '{"grounded":true,"answers_question":true,"issues":[]}',
])
def test_malformed_answer_review_is_not_accepted(content):
    client = RealGemmaClient("http://llm/v1", http=httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"choices": [{"message": {"content": content}}]}))))
    with pytest.raises(RuntimeError, match="검토"):
        client.review("질문", "답 [SOURCE_01]", [{"sid": "SOURCE_01", "content": "근거"}])


@pytest.fixture
def evidence(monkeypatch):
    monkeypatch.setenv("USE_QWEN", "0")
    conn = get_conn()
    index_document(conn, {"document_id": "one", "title": "시험"}, [{"text": "고온 냉각 후 출력 회복"}])
    yield conn
    conn.close()


class SolutionClient:
    mode = "live"
    def __init__(self, passes_second=True):
        self.passes_second = passes_second
        self.reviews = []
        self.revisions = []
    def judge(self, question, text):
        return True
    def generate(self, question, sources):
        return "근거 없이 부품 교체 [SOURCE_01]"
    def review(self, question, answer, sources):
        self.reviews.append((question, answer, sources))
        passed = self.passes_second and len(self.reviews) == 2
        return {"grounded": passed, "answers_question": True, "logically_consistent": passed,
                "issues": [] if passed else ["부품 교체 근거가 없으므로 냉각 점검으로 수정"]}
    def revise(self, question, answer, sources, issues):
        self.revisions.append((question, answer, sources, issues))
        return "냉각 조건을 점검하고 동일 조건에서 출력 회복을 확인합니다 [SOURCE_01]"


def test_solution_is_revised_once_then_reviewed_again(evidence):
    client = SolutionClient()
    out = run_rag("출력 저하 해결?", ["all"], evidence, client=client, target_min=1)
    assert out["answer_status"] == "answered"
    assert out["answer_review_status"] == "passed"
    assert out["answer_revision_count"] == 1
    assert "냉각 조건" in out["answer"] and "부품 교체" not in out["answer"]
    assert len(client.reviews) == 2 and len(client.revisions) == 1
    assert all(r[0] == "출력 저하 해결?" for r in client.reviews)
    assert client.reviews[0][2] == client.reviews[1][2] == out["sources"]


def test_unresolved_grounding_or_logic_failure_abstains_but_keeps_sources(evidence):
    client = SolutionClient(passes_second=False)
    out = run_rag("출력 저하 해결?", ["all"], evidence, client=client, target_min=1)
    assert out["answer_status"] == "review_failed"
    assert out["answer_review_status"] == "failed"
    assert "부품 교체" not in out["answer"] and "냉각 조건을 점검하고" not in out["answer"]
    assert len(client.reviews) == 2 and len(client.revisions) == 1
    assert len(out["sources"]) == 1 and "version=v1" in out["sources"][0]["download_url"]


def test_revision_cannot_introduce_unknown_citations(evidence):
    class Inventing(SolutionClient):
        def revise(self, *args):
            return "검토 후에도 가짜 근거 [SOURCE_FAKE]"
    out = run_rag("질문", ["all"], evidence, client=Inventing(), target_min=1)
    assert out["answer_status"] == "citation_error"
    assert "가짜 근거" not in out["answer"]


def test_identical_files_do_not_count_as_multiple_sources(monkeypatch):
    monkeypatch.setenv("USE_QWEN", "0")
    conn = get_conn()
    try:
        for did in ("original", "copy"):
            index_document(conn, {"document_id": did, "title": did, "content_hash": "identical-file"},
                           [{"text": "고온 냉각 후 출력 회복"}])
        class Client:
            def judge(self, *args):
                return True
            def generate(self, question, sources):
                return "냉각 [SOURCE_01]"
        out = run_rag("고온 해결?", ["all"], conn, client=Client(), target_min=1)
        assert len(out["sources"]) == 1
    finally:
        conn.close()
