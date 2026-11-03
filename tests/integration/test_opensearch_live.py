"""Opt-in live OpenSearch contract; disposable index and isolated metadata only.

RAG_TEST_OPENSEARCH_URL=http://127.0.0.1:9200 python -m pytest tests/integration/test_opensearch_live.py -q
"""
import os
import uuid

import pytest

from database.db import get_conn
from database.vector_store import EMBEDDING_VERSION
from ingestion.indexer import index_document
from retrieval.opensearch_store import OpenSearchStore, enqueue_delete


@pytest.mark.skipif(not os.getenv("RAG_TEST_OPENSEARCH_URL"), reason="explicit isolated OpenSearch service URL required")
def test_live_acl_exclusions_stale_content_update_and_deletion(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_DB_BACKEND", "sqlite")
    monkeypatch.setenv("USE_QWEN", "1")
    vector = [1.0] + [0.0] * 1023
    alternate = [0.8, 0.6] + [0.0] * 1022
    monkeypatch.setattr("models.embedding.embed", lambda *a, **kw: [vector])
    store = OpenSearchStore(os.environ["RAG_TEST_OPENSEARCH_URL"], "rag-contract-" + uuid.uuid4().hex,
                            username=os.getenv("RAG_TEST_OPENSEARCH_USERNAME"),
                            password=os.getenv("RAG_TEST_OPENSEARCH_PASSWORD"),
                            verify_tls=os.getenv("RAG_TEST_OPENSEARCH_VERIFY_TLS", "1") != "0",
                            ca_certs=os.getenv("RAG_TEST_OPENSEARCH_CA_CERTS"))
    conn = get_conn(str(tmp_path / "live.sqlite3"))
    try:
        assert store.health()["healthy"]
        for did, groups, text, embedding in (
                ("private", ["private"], "critical pressure " * 50, vector),
                ("accepted", ["team"], "critical pressure " * 30, vector),
                ("wanted", ["team"], "critical pressure procedure", alternate)):
            index_document(conn, {"document_id": did, "title": did, "allowed_groups": groups},
                           [{"text": text}], vectors=[embedding])
        assert store.reconcile(conn)["synced"] == 3
        def rows():
            return [dict(r) for r in conn.execute("SELECT c.*,d.content_hash,d.status FROM chunks c JOIN documents d ON d.document_id=c.document_id WHERE d.status='ACTIVE'")]
        permitted = [r for r in rows() if r["document_id"] != "accepted"]
        result = store.search("critical pressure", permitted, ["team"], k=1)
        assert [r["document_id"] for r in result] == ["wanted"]
        assert result[0]["embedding_version"] == EMBEDDING_VERSION
        # Same chunk IDs/version with changed content: remote content must not consume
        # the limit or determine current-row ranking before its outbox is synced.
        index_document(conn, {"document_id": "wanted", "title": "wanted", "allowed_groups": ["team"]},
                       [{"text": "critical pressure changed procedure"}], vectors=[alternate])
        current = [r for r in rows() if r["document_id"] == "wanted"]
        assert store.search("critical pressure", current, ["team"], k=1) == []
        assert store.reconcile(conn)["synced"] == 1
        assert store.search("critical pressure", current, ["team"], k=1)[0]["text"] == "critical pressure changed procedure"
        # Replacement with a new version removes the old remote IDs after upsert.
        index_document(conn, {"document_id": "wanted", "title": "wanted", "version": "v2", "allowed_groups": ["team"]},
                       [{"text": "critical pressure version two"}], vectors=[alternate])
        assert store.reconcile(conn)["synced"] == 1
        count = store._request("POST", f"/{store.index}/_count", {"query": {"term": {"document_id": "wanted"}}})
        assert count["count"] == 1
        conn.execute("UPDATE documents SET status='DELETED' WHERE document_id='wanted'")
        enqueue_delete(conn, "wanted")
        conn.commit()
        assert store.reconcile(conn)["synced"] == 1
        count = store._request("POST", f"/{store.index}/_count", {"query": {"term": {"document_id": "wanted"}}})
        assert count["count"] == 0
    finally:
        conn.close()
        store._request("DELETE", f"/{store.index}", allow_404=True)