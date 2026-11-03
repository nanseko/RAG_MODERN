import math

from models.embedding import embed
from models.reranker import score_pairs
from retrieval.rerank import rerank
from retrieval.vector import vector_search


def _docs():
    return [
        {"chunk_id": "c1", "document_id": "d1", "text": "오늘 점심 메뉴는 김치찌개다",
         "allowed_groups": ["all"]},
        {"chunk_id": "c2", "document_id": "d2",
         "text": "고온 구간에서 RF 출력이 3dB 감소했다", "allowed_groups": ["all"]},
    ]


def _norm(v) -> float:
    return math.sqrt(sum(x * x for x in v))


def test_embedding_dim_1024_and_normalized():
    vecs = embed(["고온시험에서 출력이 감소한 사례", "일반 문서 내용"])
    assert len(vecs) == 2
    assert all(len(v) == 1024 for v in vecs)
    assert all(abs(_norm(v) - 1.0) < 1e-3 for v in vecs)


def test_reranker_scores_relevant_higher():
    q = "고온시험 출력 감소 사례"
    pairs = [(q, "고온 구간에서 RF 출력이 3dB 감소했다"),
             (q, "오늘 점심 메뉴는 김치찌개다")]
    scores = score_pairs(pairs)
    assert len(scores) == 2
    assert all(isinstance(s, float) for s in scores)
    assert scores[0] > scores[1]


def test_dense_finds_paraphrase_without_shared_words():
    docs = _docs()
    out = vector_search("열 때문에 성능이 떨어진 경우", docs, k=2)
    assert out[0]["chunk_id"] == "c2"


def test_rerank_orders_paraphrase_first():
    out = rerank("열 때문에 성능이 떨어진 경우", _docs(), top_n=2)
    assert out[0]["chunk_id"] == "c2"
