import json
import sqlite3

from database.db import get_conn, migrate
from ingestion.chunker import hybrid_chunk
from ingestion.indexer import index_document
from retrieval.vector import cosine_topk


def _vec(i: int, dim: int = 1024) -> str:
    v = [0.0] * dim
    v[i] = 1.0
    return json.dumps(v)


def test_cosine_topk_ranks_by_stored_vectors():
    docs = [
        {"chunk_id": "c1", "text": "a", "embedding": _vec(0)},
        {"chunk_id": "c2", "text": "b", "embedding": _vec(1)},
    ]
    q = [0.0] * 1024
    q[1] = 1.0
    out = cosine_topk(q, docs, k=2)
    assert [d["chunk_id"] for d in out] == ["c2", "c1"]


def test_cosine_topk_skips_missing_embeddings():
    docs = [
        {"chunk_id": "c1", "text": "a"},
        {"chunk_id": "c2", "text": "b", "embedding": _vec(5)},
    ]
    q = [0.0] * 1024
    q[5] = 1.0
    out = cosine_topk(q, docs, k=2)
    assert [d["chunk_id"] for d in out] == ["c2"]


def test_migrate_adds_embedding_column(tmp_path):
    db = str(tmp_path / "old.sqlite3")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE documents(document_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE chunks(chunk_id TEXT PRIMARY KEY, text TEXT)")
    conn.commit()
    conn.close()
    conn = get_conn(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()]
    conn.close()
    assert "embedding" in cols


def test_index_stores_embedding_json(tmp_path):
    db = str(tmp_path / "v.sqlite3")
    conn = get_conn(db)
    secs = [{"title": "t", "heading_path": "", "page_number": None,
             "text": "고온 구간에서 출력이 감소했다"}]
    did = index_document(conn, {"title": "t"}, hybrid_chunk(secs))
    row = conn.execute("SELECT embedding FROM chunks WHERE document_id=?",
                       (did,)).fetchone()
    conn.close()
    vec = json.loads(row[0])
    assert len(vec) == 1024
    assert abs(sum(x * x for x in vec) - 1.0) < 1e-3


def test_vector_search_handles_rows_without_embeddings():
    docs = [{"chunk_id": "c1", "document_id": "d1", "text": "출력 저하 기록",
             "allowed_groups": ["all"]}]
    from retrieval.vector import vector_search
    out = vector_search("출력 저하", docs, k=1)
    assert out[0]["chunk_id"] == "c1"
