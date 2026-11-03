import json
import httpx
import pytest

from database.db import get_conn
from ingestion.indexer import index_document
from models.gemma_client import RealGemmaClient
from rag.graph import run_rag


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_QWEN", "0")
    with get_conn(str(tmp_path / "rag.sqlite3")) as c:
        yield c
    c.close()


class Reviewer:
    def __init__(self, accept=lambda text: "관련" in text):
        self.accept = accept
        self.questions = []
        self.sources = []
        self.rewrites = []

    def judge(self, question, text):
        self.questions.append(question)
        return self.accept(text)

    def rewrite(self, question, rejected):
        self.rewrites.append((question, rejected))
        return "대체 검색어"

    def generate(self, question, sources):
        self.sources = sources
        return "확인된 근거 " + " ".join(f"[{s['sid']}]" for s in sources)


def seed(conn, did, texts, **metadata):
    index_document(conn, {"document_id": did, "title": did, **metadata},
                   [{"text": text, "page_number": i + 1} for i, text in enumerate(texts)])


def test_source_target_counts_documents_not_chunks(conn):
    seed(conn, "one", ["관련 시험 사례 " + str(i) for i in range(8)])
    seed(conn, "two", ["관련 시험 비교"])
    seed(conn, "three", ["관련 시험 검증"])
    out = run_rag("시험", ["all"], conn, client=Reviewer())
    assert {s["document_id"] for s in out["sources"]} == {"one", "two", "three"}
    assert len(out["sources"]) == 3


def test_retry_filters_rejected_documents_before_top50(conn):
    for i in range(53):
        seed(conn, f"doc{i:02}", [f"시험 {i} " + ("관련" if i >= 50 else "탈락")])
    client = Reviewer()
    out = run_rag("시험", ["all"], conn, client=client, top_k=50, max_cycles=2)
    assert {s["document_id"] for s in out["sources"]} == {"doc50", "doc51", "doc52"}
    assert out["cycles"] == 2


def test_original_question_is_used_after_rewrite(conn):
    seed(conn, "one", ["탈락 자료"])
    seed(conn, "two", ["관련 자료"])
    client = Reviewer()
    out = run_rag("원래 질문", ["all"], conn, client=client, top_k=1, max_cycles=2)
    assert client.questions and set(client.questions) == {"원래 질문"}
    assert out["cycles"] == 2


def test_llm_rewrites_query_when_more_documents_needed(conn):
    seed(conn, "one", ["관련 자료"])
    client = Reviewer()
    run_rag("원래 질문", ["all"], conn, client=client, max_cycles=2)
    assert client.rewrites and client.rewrites[0][0] == "원래 질문"


def test_generation_receives_parent_context_and_real_version(conn, monkeypatch):
    seed(conn, "one", ["조건: 85도", "관련 시험: 출력 감소", "조치: 방열 개선"], version="v7")
    client = Reviewer()
    out = run_rag("시험", ["all"], conn, client=client, target_min=1)
    assert len(out["sources"]) == 1
    assert out["sources"][0]["version"] == "v7"
    assert "방열 개선" in client.sources[0]["content"]


def test_no_evidence_is_explicit(conn):
    seed(conn, "one", ["탈락 자료"])
    out = run_rag("시험", ["all"], conn, client=Reviewer())
    assert out["sources"] == []
    assert out["answer_status"] == "no_evidence"


def test_unknown_citation_invalidates_answer(conn):
    class Fabricating(Reviewer):
        def generate(self, question, sources):
            return "사실이라고 주장 [SOURCE_99]"
    seed(conn, "one", ["관련 자료"])
    out = run_rag("시험", ["all"], conn, client=Fabricating(), target_min=1)
    assert "사실이라고 주장" not in out["answer"]
    assert out["answer_status"] == "citation_error"


def test_llm_judgment_never_accepts_malformed_payload():
    client = RealGemmaClient("http://llm/v1", http=httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": []}))))
    with pytest.raises(RuntimeError):
        client.judge("질문", "자료")


def test_llm_judgment_is_strict_boolean():
    client = RealGemmaClient("http://llm/v1", http=httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"choices": [{"message": {"content": "yes but irrelevant"}}]}))))
    with pytest.raises(RuntimeError):
        client.judge("질문", "자료")


def test_sql_vector_store_persists_and_filters_before_ranking(conn):
    from database.vector_store import put, search
    positive = [1.0] + [0.0] * 1023
    negative = [0.0, 1.0] + [0.0] * 1022
    seed(conn, "one", ["원본"])
    cid = conn.execute("SELECT chunk_id FROM chunks").fetchone()[0]
    put(conn, cid, "one", positive)
    put(conn, "private", "two", positive)
    put(conn, "allowed", "three", negative)
    conn.commit()
    assert search(conn, positive, ["allowed"], k=1)[0][0] == "allowed"
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    c2 = get_conn(path)
    try:
        assert search(c2, positive, [cid], k=1)[0][1] == pytest.approx(1.0)
    finally:
        c2.close()


def test_embedding_result_is_independent_of_batch_padding():
    from models.embedding import embed
    alone = embed(["짧은 본문"])[0]
    batched = embed(["짧은 본문", "매우 긴 기술자료의 시험 조건과 결과 기록 " * 10])[0]
    assert alone == pytest.approx(batched, abs=1e-4)


def test_embedding_runtime_failure_is_not_silently_stored_as_null(conn, monkeypatch):
    import models.embedding
    monkeypatch.setenv("USE_QWEN", "1")
    def fail(*args, **kwargs):
        raise RuntimeError("GPU unavailable")
    monkeypatch.setattr(models.embedding, "embed", fail)
    with pytest.raises(RuntimeError, match="GPU unavailable"):
        seed(conn, "failure", ["시험 자료"])
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
