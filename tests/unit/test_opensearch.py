"""OpenSearch contracts: isolate SQL data and replace only the HTTP boundary."""
import json
import urllib.error

import pytest

from database.db import get_conn
from ingestion.indexer import index_document


def _module():
    import retrieval.opensearch_store as store
    return store


def _doc(cid="d1:v1:0", did="d1", text="pump ERR_PUMP_123 pressure", groups=None):
    return {"chunk_id": cid, "document_id": did, "document_version": "v1",
            "version": "v1", "text": text, "title": "report", "allowed_groups": groups or ["team"],
            "part_numbers": '[]', "error_codes": '["ERR_PUMP_123"]', "embedding_version": "disabled",
            "content_hash": "hash-one"}


def test_search_filters_acl_permitted_ids_and_versions_before_candidate_limits(monkeypatch):
    module = _module()
    monkeypatch.setenv("USE_QWEN", "1")
    vector = [1.0] + [0.0] * 1023
    monkeypatch.setattr("models.embedding.embed", lambda *a, **kw: [vector])
    row = _doc()
    bodies = []
    store = module.OpenSearchStore("http://localhost:9200", "test-chunks")
    def request(method, path, body=None, **kwargs):
        bodies.append(body)
        return {"hits": {"hits": [{"_id": row["chunk_id"], "_score": 2.0,
                                    "_source": {"text": "untrusted remote text"}}]}}
    monkeypatch.setattr(store, "_request", request)
    out = store.search("ERR_PUMP_123", [row], ["team"], k=1)
    assert out[0]["text"] == "pump ERR_PUMP_123 pressure"
    assert out[0]["retrieval_score"] > 0
    lexical = next(b for b in bodies if "bool" in b["query"])
    dense = next(b for b in bodies if "knn" in b["query"])
    filters = lexical["query"]["bool"]["filter"]
    assert {"terms": {"chunk_id": ["d1:v1:0"]}} in filters
    assert {"terms": {"document_id": ["d1"]}} in filters
    assert {"terms": {"document_version": ["v1"]}} in filters
    assert {"terms": {"allowed_groups": ["all", "team"]}} in filters
    assert any("source_fingerprint" in f.get("terms", {}) for f in filters)
    assert dense["query"]["knn"]["embedding"]["filter"] == {"bool": {"filter": filters + [
        {"term": {"embedding_version": "qwen3-last-token-v2"}}]}}
    assert "post_filter" not in dense


def test_hybrid_selected_backend_never_silently_uses_sqlite(monkeypatch):
    module = _module()
    from retrieval.hybrid import hybrid_search
    monkeypatch.setenv("RAG_SEARCH_BACKEND", "opensearch")
    monkeypatch.setattr(module.OpenSearchStore, "search", lambda *a, **kw: (_ for _ in ()).throw(
        module.OpenSearchError("unavailable")))
    with pytest.raises(module.OpenSearchError, match="unavailable"):
        hybrid_search("pressure", [_doc()], ["team"], k=1)


def test_http_failure_redacts_credentials_and_does_not_return_empty(monkeypatch):
    module = _module()
    def down(*a, **kw):
        raise urllib.error.URLError("secretpassword unreachable")
    monkeypatch.setattr(module.urllib.request, "urlopen", down)
    store = module.OpenSearchStore("https://localhost:9200", "test-chunks", username="reader", password="secretpassword")
    with pytest.raises(module.OpenSearchError) as caught:
        store.health()
    assert "secretpassword" not in str(caught.value)


def test_index_enqueue_is_durable_and_transactional(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setenv("USE_QWEN", "0")
    conn = get_conn(str(tmp_path / "outbox.sqlite3"))
    module.initialize_outbox(conn)
    conn.commit()
    conn.execute("BEGIN")
    index_document(conn, {"document_id": "d1", "title": "report"}, [{"text": "old text"}], commit=False)
    assert conn.execute("SELECT operation FROM search_outbox WHERE document_id='d1'").fetchone()[0] == "upsert"
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM search_outbox").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    conn.close()


def test_failed_bulk_retains_outbox_and_does_not_delete_last_index(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setenv("USE_QWEN", "0")
    conn = get_conn(str(tmp_path / "retry.sqlite3"))
    index_document(conn, {"document_id": "d1"}, [{"text": "current text"}])
    store = module.OpenSearchStore("http://localhost:9200", "test-chunks")
    monkeypatch.setattr(store, "ensure_index", lambda: None)
    calls = []
    def broken(method, path, body=None, **kwargs):
        calls.append(path)
        if "_bulk" in path:
            return {"errors": True, "items": [{"index": {"status": 503, "error": {"type": "unavailable"}}}]}
        return {}
    monkeypatch.setattr(store, "_request", broken)
    result = store.reconcile(conn)
    assert result["failed"] == 1
    row = conn.execute("SELECT attempts,last_error FROM search_outbox WHERE document_id='d1'").fetchone()
    assert row["attempts"] == 1 and row["last_error"]
    assert not any("_delete_by_query" in path for path in calls)
    assert conn.execute("SELECT text FROM chunks").fetchone()[0] == "current text"
    conn.close()


def test_reconciler_retries_bulk_then_purges_stale_chunks_and_consumes_delete(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setenv("USE_QWEN", "0")
    conn = get_conn(str(tmp_path / "reconcile.sqlite3"))
    index_document(conn, {"document_id": "d1", "version": "v2", "allowed_groups": ["team"]}, [{"text": "new ERR_PUMP_123"}])
    store = module.OpenSearchStore("http://localhost:9200", "test-chunks")
    monkeypatch.setattr(store, "ensure_index", lambda: None)
    calls = []
    def success(method, path, body=None, **kwargs):
        calls.append((path, body))
        return {"errors": False, "items": [{"index": {"status": 201}}]} if "_bulk" in path else {"deleted": 1, "failures": []}
    monkeypatch.setattr(store, "_request", success)
    assert store.reconcile(conn)["synced"] == 1
    bulk = next(body for path, body in calls if "_bulk" in path)
    source = json.loads(bulk.splitlines()[1])
    assert source["document_version"] == "v2" and source["allowed_groups"] == ["team"]
    assert source["error_codes"] == ["ERR_PUMP_123"]
    assert "embedding" not in source
    assert conn.execute("SELECT COUNT(*) FROM search_outbox").fetchone()[0] == 0
    conn.execute("UPDATE documents SET status='DELETED' WHERE document_id='d1'")
    module.enqueue_delete(conn, "d1")
    conn.commit()
    assert store.reconcile(conn)["synced"] == 1
    assert calls[-1][1]["query"] == {"bool": {"filter": [{"term": {"document_id": "d1"}}, {"range": {"index_generation": {"lte": 2}}}]}}
    conn.close()


def test_mapping_uses_keyword_identifiers_and_filtered_1024dim_vectors(monkeypatch):
    module = _module()
    store = module.OpenSearchStore("http://localhost:9200", "test-chunks")
    requests = []
    def request(method, path, body=None, **kwargs):
        requests.append((method, path, body))
        return None if method == "GET" else {"acknowledged": True}
    monkeypatch.setattr(store, "_request", request)
    store.ensure_index()
    mapping = next(body for method, path, body in requests if method == "PUT")
    props = mapping["mappings"]["properties"]
    assert props["text"]["type"] == "text"
    assert props["part_numbers"]["type"] == props["error_codes"]["type"] == "keyword"
    assert props["embedding"]["type"] == "knn_vector" and props["embedding"]["dimension"] == 1024
    assert props["embedding"]["method"]["engine"] == "lucene"


def test_mapping_declares_troubleshooting_fields(monkeypatch):
    module = _module()
    store = module.OpenSearchStore("http://localhost:9200", "test-chunks")
    requests = []
    def request(method, path, body=None, **kwargs):
        requests.append((method, path, body))
        return None if method == "GET" else {"acknowledged": True}
    monkeypatch.setattr(store, "_request", request)
    store.ensure_index()
    mapping = next(body for method, path, body in requests if method == "PUT")
    props = mapping["mappings"]["properties"]
    for field in ("role", "category", "outcome", "equipment"):
        assert props[field]["type"] == "keyword", field
    assert mapping["mappings"]["_meta"]["rag_schema_version"] == module.SCHEMA_VERSION


def test_source_document_carries_troubleshooting_fields():
    module = _module()
    row = {"chunk_id": "c1", "document_id": "d1", "text": "AN/TPS-43 냉각팬 오류",
           "role": "cause", "category": "이슈·장애", "outcome": "resolved",
           "equipment": '["AN/TPS-43"]'}
    source = module._source(row)
    assert source["role"] == "cause"
    assert source["category"] == "이슈·장애"
    assert source["outcome"] == "resolved"
    assert source["equipment"] == ["AN/TPS-43"]


def test_source_defaults_missing_troubleshooting_fields():
    module = _module()
    source = module._source({"chunk_id": "c1", "document_id": "d1", "text": "x"})
    assert source["role"] == "other" and source["outcome"] == "unknown"
    assert source["equipment"] == [] and source["category"] == "기타"


def test_equipment_falls_back_to_extraction_when_column_missing():
    module = _module()
    source = module._source({"chunk_id": "c1", "document_id": "d1",
                             "text": "KT-1 지휘계 점검"})
    assert source["equipment"] == ["KT-1"]


def test_stale_mapping_without_new_fields_is_rejected(monkeypatch):
    module = _module()
    store = module.OpenSearchStore("http://localhost:9200", "test-chunks")
    legacy = {"properties": {"embedding": {"dimension": 1024,
                                           "method": {"engine": "lucene"}},
                             "source_fingerprint": {"type": "keyword"}},
              "_meta": {"rag_schema_version": module.SCHEMA_VERSION - 1}}
    monkeypatch.setattr(store, "_request", lambda *a, **kw: {"test-chunks": {"mappings": legacy}})
    with pytest.raises(module.OpenSearchError, match="incompatible"):
        store.ensure_index()


def _facets(should):
    return {next(iter(term["terms"])) for term in should if "terms" in term}


def test_search_query_boosts_role_and_outcome_terms(monkeypatch):
    module = _module()
    monkeypatch.setenv("USE_QWEN", "0")
    store = module.OpenSearchStore("http://localhost:9200", "test-chunks")
    bodies = []
    def request(method, path, body=None, **kwargs):
        bodies.append(body)
        return {"hits": {"hits": []}}
    monkeypatch.setattr(store, "_request", request)
    store.search("AN/TPS-43 원인 조치 해결", [_doc()], ["team"], k=5)
    lexical = next(b for b in bodies if "bool" in b["query"])
    should = lexical["query"]["bool"]["should"]
    fields = _facets(should)
    assert {"role", "outcome", "equipment"} <= fields


def test_search_query_omits_facet_terms_without_signal(monkeypatch):
    module = _module()
    monkeypatch.setenv("USE_QWEN", "0")
    store = module.OpenSearchStore("http://localhost:9200", "test-chunks")
    bodies = []
    monkeypatch.setattr(store, "_request",
                        lambda method, path, body=None, **kw: bodies.append(body) or {"hits": {"hits": []}})
    store.search("자료 요약", [_doc()], ["team"], k=5)
    should = next(b for b in bodies if "bool" in b["query"])["query"]["bool"]["should"]
    fields = _facets(should)
    assert "role" not in fields and "outcome" not in fields


def test_backfill_preserves_corpus_identifiers_and_versions(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setenv("USE_QWEN", "0")
    conn = get_conn(str(tmp_path / "backfill.sqlite3"))
    index_document(conn, {"document_id": "keep-id", "version": "v7"}, [{"text": "historic text"}])
    conn.execute("DELETE FROM search_outbox")
    conn.commit()
    assert module.enqueue_backfill(conn) == 1
    assert conn.execute("SELECT version FROM documents WHERE document_id='keep-id'").fetchone()[0] == "v7"
    assert conn.execute("SELECT chunk_id FROM chunks").fetchone()[0] == "keep-id:v7:0"
    conn.close()

def test_malformed_search_response_fails_instead_of_claiming_no_evidence(monkeypatch):
    module = _module()
    monkeypatch.setenv("USE_QWEN", "0")
    store = module.OpenSearchStore("http://localhost:9200", "test-chunks")
    monkeypatch.setattr(store, "_request", lambda *a, **kw: {"error": {"type": "service_failure"}})
    with pytest.raises(module.OpenSearchError):
        store.search("pressure", [_doc()], ["team"], k=1)


def test_incomplete_bulk_acknowledgement_is_retained_for_retry(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setenv("USE_QWEN", "0")
    conn = get_conn(str(tmp_path / "incomplete.sqlite3"))
    index_document(conn, {"document_id": "d1"}, [{"text": "still durable"}])
    store = module.OpenSearchStore("http://localhost:9200", "test-chunks")
    monkeypatch.setattr(store, "ensure_index", lambda: None)
    monkeypatch.setattr(store, "_request", lambda *a, **kw: {"errors": False, "items": []})
    assert store.reconcile(conn)["failed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM search_outbox").fetchone()[0] == 1
    conn.close()


def test_new_outbox_revision_during_sync_is_not_acknowledged_or_lost(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setenv("USE_QWEN", "0")
    conn = get_conn(str(tmp_path / "revision.sqlite3"))
    index_document(conn, {"document_id": "d1"}, [{"text": "version one"}])
    store = module.OpenSearchStore("http://localhost:9200", "test-chunks")
    monkeypatch.setattr(store, "ensure_index", lambda: None)
    def replace(conn, document_id, operation, generation):
        index_document(conn, {"document_id": "d1", "version": "v2"}, [{"text": "version two"}])
    monkeypatch.setattr(store, "_replace_document", replace)
    result = store.reconcile(conn)
    assert result["pending"] == 1
    row = conn.execute("SELECT operation,processing_token FROM search_outbox").fetchone()
    assert row["operation"] == "upsert" and row["processing_token"] is None
    assert conn.execute("SELECT version FROM documents").fetchone()[0] == "v2"
    conn.close()

def test_admin_cli_backfills_and_reconciles_without_changing_source(tmp_path, monkeypatch, capsys):
    from scripts import opensearch_admin
    module = _module()
    monkeypatch.setenv("USE_QWEN", "0")
    db = str(tmp_path / "cli.sqlite3")
    conn = get_conn(db)
    index_document(conn, {"document_id": "historic", "version": "v9"}, [{"text": "historic source"}])
    conn.execute("DELETE FROM search_outbox")
    conn.commit()
    conn.close()
    store = module.OpenSearchStore("http://localhost:9200", "test-chunks")
    monkeypatch.setattr(module.OpenSearchStore, "from_env", lambda: store)
    monkeypatch.setattr(store, "ensure_index", lambda: None)
    def request(method, path, body=None, **kwargs):
        return ({"errors": False, "items": [{"index": {"status": 201}}]} if "_bulk" in path
                else {"deleted": 0, "failures": []})
    monkeypatch.setattr(store, "_request", request)
    assert opensearch_admin.main(["--db", db, "backfill"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["queued"] == 1 and result["synced"] == 1
    conn = get_conn(db)
    assert conn.execute("SELECT version FROM documents WHERE document_id='historic'").fetchone()[0] == "v9"
    assert conn.execute("SELECT text FROM chunks").fetchone()[0] == "historic source"
    assert conn.execute("SELECT COUNT(*) FROM search_outbox").fetchone()[0] == 0
    conn.close()

def test_pipeline_committed_job_survives_search_outage_with_visible_pending_sync(tmp_path, monkeypatch):
    from ingestion.collector import enqueue_file
    from ingestion.pipeline import run_job
    module = _module()
    monkeypatch.setenv("USE_QWEN", "0")
    monkeypatch.setenv("RAG_SEARCH_BACKEND", "opensearch")
    def fail(*a, **kw):
        raise module.OpenSearchError("service offline")
    monkeypatch.setattr(module.OpenSearchStore, "ensure_index", fail)
    path = tmp_path / "source.md"
    path.write_text("durable source", encoding="utf8")
    conn = get_conn(str(tmp_path / "pipeline.sqlite3"))
    job = enqueue_file(conn, path)
    result = run_job(conn, job["job_id"])
    assert result["status"] == "DONE"
    assert result["search_sync"]["status"] == "PENDING"
    assert result["search_sync"]["pending"] == 1
    assert conn.execute("SELECT status FROM ingestion_jobs").fetchone()[0] == "DONE"
    assert conn.execute("SELECT text FROM chunks").fetchone()[0] == "durable source"
    conn.close()


def test_empty_scan_consumes_durable_deletion_outbox(tmp_path, monkeypatch):
    from ingestion.pipeline import scan_once
    module = _module()
    monkeypatch.setenv("USE_QWEN", "0")
    monkeypatch.setenv("RAG_SEARCH_BACKEND", "opensearch")
    db = str(tmp_path / "scan.sqlite3")
    docs = tmp_path / "docs"
    docs.mkdir()
    conn = get_conn(db)
    module.enqueue_delete(conn, "deleted-doc")
    conn.commit()
    conn.close()
    store = module.OpenSearchStore("http://localhost:9200", "test-chunks")
    monkeypatch.setattr(module.OpenSearchStore, "from_env", lambda: store)
    monkeypatch.setattr(store, "ensure_index", lambda: None)
    monkeypatch.setattr(store, "_request", lambda *a, **kw: {"deleted": 3, "failures": []})
    result = scan_once(str(docs), db)
    assert result["jobs"] == 0
    assert result["search_sync"]["synced"] == 1
    conn = get_conn(db)
    assert conn.execute("SELECT COUNT(*) FROM search_outbox").fetchone()[0] == 0
    conn.close()

def test_stale_reconciler_bulk_and_cleanup_are_version_guarded(tmp_path, monkeypatch):
    module = _module()
    monkeypatch.setenv("USE_QWEN", "0")
    conn = get_conn(str(tmp_path / "generation.sqlite3"))
    index_document(conn, {"document_id": "d1"}, [{"text": "first"}])
    conn.execute("DELETE FROM search_outbox")
    conn.commit()
    index_document(conn, {"document_id": "d1"}, [{"text": "second"}])
    generation = conn.execute("SELECT generation FROM search_outbox WHERE document_id='d1'").fetchone()[0]
    assert generation == 2  # Acknowledged outbox deletion must not reset remote write order.
    store = module.OpenSearchStore("http://localhost:9200", "test-chunks")
    monkeypatch.setattr(store, "ensure_index", lambda: None)
    calls = []
    def request(method, path, body=None, **kwargs):
        calls.append((path, body))
        return ({"errors": False, "items": [{"index": {"status": 201}}]} if "_bulk" in path
                else {"deleted": 0, "failures": []})
    monkeypatch.setattr(store, "_request", request)
    assert store.reconcile(conn)["synced"] == 1
    bulk = next(body for path, body in calls if "_bulk" in path)
    operation = json.loads(bulk.splitlines()[0])["index"]
    assert operation["version"] == 2 and operation["version_type"] == "external_gte"
    assert json.loads(bulk.splitlines()[1])["index_generation"] == 2
    delete = calls[-1][1]["query"]["bool"]
    assert {"range": {"index_generation": {"lt": 2}}} in delete["filter"]
    conn.close()