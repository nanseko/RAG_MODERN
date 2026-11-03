"""OpenSearch retrieval and a transactional SQL outbox for recoverable indexing.

SQL metadata is authoritative. Search filters the exact permitted SQL snapshot
before top-k and returns its rows, never remote source text. No service fallback.
"""
import base64
import hashlib
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

from database.vector_store import DIMENSION, EMBEDDING_VERSION, validate_vector
from ingestion.metadata import extract_identifiers

SCHEMA_VERSION = 3
FILTER_BATCH = 5000
# Troubleshooting facets; keyword so boosts and filters stay exact.
TROUBLESHOOTING_KEYWORDS = ("role", "category", "outcome", "equipment")


class OpenSearchError(RuntimeError):
    """Selected OpenSearch service could not satisfy the operation."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def initialize_outbox(conn):
    # Portable DDL, executed by database initialization and safe inside savepoints.
    conn.execute("""CREATE TABLE IF NOT EXISTS search_outbox(
      document_id TEXT PRIMARY KEY, revision TEXT NOT NULL, operation TEXT NOT NULL,
      attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, updated_at TEXT NOT NULL,
      processing_token TEXT, lease_until TEXT, generation BIGINT NOT NULL DEFAULT 1)""")
    if getattr(conn, "backend", "sqlite") == "postgresql":
        columns = {r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='search_outbox'")}
    else:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(search_outbox)")}
    if "generation" not in columns:
        conn.execute("ALTER TABLE search_outbox ADD COLUMN generation BIGINT NOT NULL DEFAULT 1")
    conn.execute("CREATE TABLE IF NOT EXISTS search_document_generations(document_id TEXT PRIMARY KEY,generation BIGINT NOT NULL)")
    conn.execute("INSERT INTO search_document_generations(document_id,generation) SELECT document_id,generation FROM search_outbox WHERE 1=1 ON CONFLICT(document_id) DO NOTHING")


def _enqueue(conn, document_id, operation):
    initialize_outbox(conn)
    generation = conn.execute("""INSERT INTO search_document_generations(document_id,generation)
      VALUES(?,1) ON CONFLICT(document_id) DO UPDATE SET generation=search_document_generations.generation+1
      RETURNING generation""", (document_id,)).fetchone()[0]
    conn.execute("""INSERT INTO search_outbox(document_id,revision,operation,updated_at,generation)
      VALUES(?,?,?,?,?) ON CONFLICT(document_id) DO UPDATE SET revision=excluded.revision,
      operation=excluded.operation,generation=excluded.generation,attempts=0,last_error=NULL,updated_at=excluded.updated_at""",
                 (document_id, uuid.uuid4().hex, operation, _now(), generation))


def enqueue_document(conn, document_id):
    """Queue within the caller's metadata transaction; never commit it here."""
    _enqueue(conn, document_id, "upsert")


def enqueue_delete(conn, document_id):
    """Queue within the caller's deletion transaction; retries are idempotent."""
    _enqueue(conn, document_id, "delete")


def enqueue_backfill(conn):
    """Explicitly queue existing rows without changing corpus IDs or versions."""
    initialize_outbox(conn)
    rows = conn.execute("SELECT document_id,status FROM documents ORDER BY document_id").fetchall()
    for row in rows:
        _enqueue(conn, row["document_id"], "upsert" if row["status"] == "ACTIVE" else "delete")
    conn.commit()
    return len(rows)


def outbox_status(conn):
    initialize_outbox(conn)
    row = conn.execute("SELECT COUNT(*) AS pending,COALESCE(SUM(CASE WHEN last_error IS NOT NULL THEN 1 ELSE 0 END),0) AS failed FROM search_outbox").fetchone()
    return {"pending": int(row["pending"]), "failed": int(row["failed"])}


def _array(value):
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return []
    return [str(v) for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _source(row):
    identifiers = extract_identifiers(row.get("text", ""))
    source = {
        "chunk_id": row["chunk_id"], "document_id": row["document_id"],
        "document_version": row.get("document_version") or row.get("version") or "v1",
        "text": row.get("text") or "", "title": row.get("title") or "",
        "heading_path": row.get("heading_path") or "", "page_number": row.get("page_number"),
        "parent_chunk_id": row.get("parent_chunk_id"), "slide_number": row.get("slide_number"),
        "sheet_name": row.get("sheet_name"), "chunk_index": row.get("chunk_index") or 0,
        "part_numbers": _array(row.get("part_numbers", identifiers["part_numbers"])),
        "error_codes": _array(row.get("error_codes", identifiers["error_codes"])),
        "equipment": _array(row.get("equipment", identifiers["equipment"])),
        "role": row.get("role") or "other",
        "category": row.get("category") or "기타",
        "outcome": row.get("outcome") or "unknown",
        "allowed_groups": sorted(set(_array(row.get("allowed_groups", ["all"])))),
        "embedding_version": row.get("embedding_version") or "legacy",
        "content_hash": row.get("content_hash") or "", "status": row.get("status") or "ACTIVE",
    }
    fingerprint = hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True,
                                            separators=(",", ":")).encode("utf8")).hexdigest()
    source["source_fingerprint"] = fingerprint
    raw = row.get("embedding")
    if raw and source["embedding_version"] == EMBEDDING_VERSION:
        vector = json.loads(raw) if isinstance(raw, str) else raw
        validate_vector(vector)
        source["embedding"] = vector
    return source


def source_fingerprint(row):
    return _source(row)["source_fingerprint"]


class OpenSearchStore:
    def __init__(self, url, index, username=None, password=None, verify_tls=True, ca_certs=None, timeout=30):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("OPENSEARCH_URL must be an HTTP(S) URL without embedded credentials or query")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,254}", index) or index in (".", ".."):
            raise ValueError("invalid OPENSEARCH_INDEX")
        if timeout <= 0 or bool(username) != bool(password):
            raise ValueError("positive timeout and both OpenSearch username/password are required")
        self.url, self.index, self.timeout = url.rstrip("/"), index, timeout
        self.headers = {"Accept": "application/json"}
        if username:
            token = base64.b64encode(f"{username}:{password}".encode("utf8")).decode("ascii")
            self.headers["Authorization"] = "Basic " + token
        self.context = ssl.create_default_context(cafile=ca_certs) if verify_tls else ssl._create_unverified_context()

    @classmethod
    def from_env(cls):
        return cls(os.getenv("OPENSEARCH_URL", "http://127.0.0.1:9200"),
                   os.getenv("OPENSEARCH_INDEX", "rag-chunks-v1"),
                   os.getenv("OPENSEARCH_USERNAME") or None, os.getenv("OPENSEARCH_PASSWORD") or None,
                   os.getenv("OPENSEARCH_VERIFY_TLS", "1").lower() not in ("0", "false", "no"),
                   os.getenv("OPENSEARCH_CA_CERTS") or None, float(os.getenv("OPENSEARCH_TIMEOUT", "30")))

    def _request(self, method, path, body=None, content_type="application/json", allow_404=False):
        headers = {**self.headers, "Content-Type": content_type}
        data = body.encode("utf8") if isinstance(body, str) else (
            json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf8") if body is not None else None)
        request = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self.context) as response:
                payload = response.read()
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as error:
            if error.code == 404 and allow_404:
                return None
            raise OpenSearchError(f"OpenSearch {method} request failed (HTTP {error.code})") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            # URL/client exceptions may contain credentials; never propagate their text.
            raise OpenSearchError(f"OpenSearch {method} request failed ({type(error).__name__})") from None

    def health(self):
        cluster = self._request("GET", "/_cluster/health")
        return {"backend": "opensearch", "index": self.index, "status": cluster.get("status", "unknown"),
                "healthy": cluster.get("status") in ("green", "yellow"),
                "nodes": cluster.get("number_of_nodes", 0)}

    def ensure_index(self):
        mapping = self._request("GET", f"/{self.index}/_mapping", allow_404=True)
        if mapping is not None:
            existing = mapping.get(self.index, {}).get("mappings", {})
            props = existing.get("properties", {})
            vector = props.get("embedding", {})
            if (existing.get("_meta", {}).get("rag_schema_version") != SCHEMA_VERSION
                    or vector.get("dimension") != DIMENSION
                    or vector.get("method", {}).get("engine") != "lucene"
                    or props.get("source_fingerprint", {}).get("type") != "keyword"
                    or any(props.get(f, {}).get("type") != "keyword"
                           for f in TROUBLESHOOTING_KEYWORDS)):
                raise OpenSearchError("OpenSearch mapping is incompatible; choose a new index and backfill")
            return
        props = {key: {"type": "keyword"} for key in (
            "chunk_id", "document_id", "document_version", "allowed_groups", "part_numbers", "error_codes",
            "embedding_version", "content_hash", "status", "source_fingerprint", "parent_chunk_id", "sheet_name")
            + TROUBLESHOOTING_KEYWORDS}
        props.update({key: {"type": "text"} for key in ("text", "title", "heading_path")})
        props.update({key: {"type": "integer"} for key in ("page_number", "slide_number", "chunk_index")})
        props["index_generation"] = {"type": "long"}
        props["embedding"] = {"type": "knn_vector", "dimension": DIMENSION,
                              "method": {"name": "hnsw", "engine": "lucene", "space_type": "cosinesimil"}}
        self._request("PUT", f"/{self.index}", {
            "settings": {"index": {"knn": True, "number_of_shards": 1, "number_of_replicas": 0}},
            "mappings": {"dynamic": "strict", "_meta": {"rag_schema_version": SCHEMA_VERSION}, "properties": props}})

    def search(self, query, docs, user_groups, k=50):
        from retrieval.hybrid import acl_filter
        docs = acl_filter(docs, user_groups)
        if not docs:
            return []
        if not 1 <= k <= 10000:
            raise ValueError("OpenSearch candidate limit must be between 1 and 10000")
        by_id = {d["chunk_id"]: d for d in docs}
        query_vector = None
        if os.getenv("USE_QWEN", "1") != "0":
            from models.embedding import embed
            query_vector = embed([query], is_query=True)[0]
            validate_vector(query_vector)
        lexical_scores, dense_scores = {}, {}
        for offset in range(0, len(docs), FILTER_BATCH):
            batch = docs[offset:offset + FILTER_BATCH]
            filters = [
                {"terms": {"chunk_id": [d["chunk_id"] for d in batch]}},
                {"terms": {"document_id": sorted({d["document_id"] for d in batch})}},
                {"terms": {"document_version": sorted({d.get("document_version") or d.get("version") or "v1" for d in batch})}},
                {"terms": {"allowed_groups": sorted({"all", *(user_groups or [])})}},
                {"terms": {"source_fingerprint": [source_fingerprint(d) for d in batch]}},
                {"term": {"status": "ACTIVE"}},
            ]
            identifiers = extract_identifiers(query)
            should = [{"multi_match": {"query": query, "fields": ["text", "title^2", "heading_path"]}}]
            for field in ("part_numbers", "error_codes", "equipment"):
                if identifiers[field]:
                    should.append({"terms": {field: identifiers[field], "boost": 5.0}})
            # Facet boosts mirror retrieval/boost.py so both backends rank alike.
            from ingestion.trouble import role_of
            role = role_of("", "", query)
            if role != "other":
                should.append({"terms": {"role": [role], "boost": 2.0}})
            if any(k in query for k in ("해결", "정상", "조치한", "대처")):
                should.append({"terms": {"outcome": ["resolved"], "boost": 1.5}})
            lexical = self._request("POST", f"/{self.index}/_search", {
                "size": k, "_source": False, "query": {"bool": {"filter": filters, "should": should, "minimum_should_match": 1}}})
            self._collect(lexical, by_id, lexical_scores)
            if query_vector is not None:
                dense = self._request("POST", f"/{self.index}/_search", {
                    "size": k, "_source": False, "query": {"knn": {"embedding": {
                        "vector": query_vector, "k": k, "filter": {"bool": {"filter": filters + [
                            {"term": {"embedding_version": EMBEDDING_VERSION}}]}}}}}})
                self._collect(dense, by_id, dense_scores)
        fused = {}
        for scores in (lexical_scores, dense_scores):
            for rank, cid in enumerate(sorted(scores, key=lambda cid: (-scores[cid], cid))[:k], 1):
                fused[cid] = fused.get(cid, 0.0) + 1 / (60 + rank)
        order = sorted(fused, key=lambda cid: (-fused[cid], cid))[:k]
        return [{**by_id[cid], "retrieval_score": fused[cid]} for cid in order]

    @staticmethod
    def _collect(response, by_id, scores):
        if not isinstance(response.get("hits"), dict) or not isinstance(response["hits"].get("hits"), list):
            raise OpenSearchError("OpenSearch returned malformed search results")
        if response.get("timed_out") or response.get("_shards", {}).get("failed", 0):
            raise OpenSearchError("OpenSearch returned incomplete search results")
        for hit in response.get("hits", {}).get("hits", []):
            cid = hit.get("_id")
            if cid in by_id:
                scores[cid] = max(scores.get(cid, float("-inf")), float(hit.get("_score") or 0.0))

    def _replace_document(self, conn, document_id, operation, generation):
        current = conn.execute("SELECT generation FROM search_document_generations WHERE document_id=?", (document_id,)).fetchone()
        if current and current[0] > generation:
            raise OpenSearchError("OpenSearch outbox revision superseded; latest revision retained for retry")
        rows = [dict(r) for r in conn.execute(
            "SELECT c.*,d.content_hash,d.status,d.allowed_groups AS document_allowed_groups,"
            "d.outcome AS document_outcome,d.category AS document_category "
            "FROM chunks c JOIN documents d ON d.document_id=c.document_id "
            "WHERE c.document_id=? AND d.status='ACTIVE' ORDER BY c.chunk_index,c.chunk_id", (document_id,)).fetchall()]
        conn.commit()
        sources = []
        if operation != "delete":
            for row in rows:
                row["allowed_groups"] = row.pop("document_allowed_groups")
                row.setdefault("outcome", row.pop("document_outcome", None))
                row.setdefault("category", row.pop("document_category", None))
                source = _source(row)
                source["index_generation"] = generation
                sources.append(source)
        if sources:
            # Publish replacements first; failed bulk never wipes the last searchable remote version.
            for offset in range(0, len(sources), 500):
                batch = sources[offset:offset + 500]
                lines = []
                for source in batch:
                    lines.extend([json.dumps({"index": {"_index": self.index, "_id": source["chunk_id"], "version": generation, "version_type": "external_gte"}}),
                                  json.dumps(source, ensure_ascii=False, allow_nan=False)])
                result = self._request("POST", "/_bulk?refresh=wait_for", "\n".join(lines) + "\n", content_type="application/x-ndjson")
                items = result.get("items", [])
                if (result.get("errors") is not False or len(items) != len(batch)
                        or any(next(iter(item.values())).get("status", 500) >= 300 for item in items)):
                    raise OpenSearchError("OpenSearch bulk indexing failed; outbox retained for retry")
        # External versions prevent a reclaimed worker from overwriting newer IDs;
        # the generation filter prevents it from deleting newer chunks afterwards.
        bound = "lt" if sources else "lte"
        query = {"bool": {"filter": [{"term": {"document_id": document_id}},
                                      {"range": {"index_generation": {bound: generation}}}]}}
        result = self._request("POST", f"/{self.index}/_delete_by_query?conflicts=proceed&refresh=true", {"query": query})
        if result.get("failures") or result.get("timed_out") or result.get("version_conflicts", 0):
            raise OpenSearchError("OpenSearch stale-chunk deletion incomplete; outbox retained for retry")

    def reconcile(self, conn, limit=100):
        initialize_outbox(conn)
        conn.commit()
        if limit < 1:
            raise ValueError("reconciliation limit must be positive")
        self.ensure_index()
        rows = conn.execute("SELECT document_id,revision,operation,generation FROM search_outbox WHERE lease_until IS NULL OR lease_until<? ORDER BY updated_at,document_id LIMIT ?", (_now(), limit)).fetchall()
        conn.commit()
        synced, failed = 0, 0
        errors = []
        for row in rows:
            token = uuid.uuid4().hex
            lease = (datetime.now(timezone.utc) + timedelta(seconds=max(300, self.timeout * 10))).isoformat(timespec="microseconds")
            claimed = conn.execute("UPDATE search_outbox SET processing_token=?,lease_until=? WHERE document_id=? AND revision=? AND (lease_until IS NULL OR lease_until<?)",
                                   (token, lease, row["document_id"], row["revision"], _now())).rowcount
            conn.commit()
            if not claimed:
                continue
            try:
                self._replace_document(conn, row["document_id"], row["operation"], row["generation"])
                conn.execute("DELETE FROM search_outbox WHERE document_id=? AND revision=? AND processing_token=?", (row["document_id"], row["revision"], token))
                conn.execute("UPDATE search_outbox SET processing_token=NULL,lease_until=NULL WHERE document_id=? AND processing_token=?", (row["document_id"], token))
                conn.commit()
                synced += 1
            except Exception as error:
                conn.rollback()
                message = str(error)[:500] if isinstance(error, OpenSearchError) else f"OpenSearch reconciliation failed ({type(error).__name__})"
                conn.execute("UPDATE search_outbox SET attempts=attempts+1,last_error=?,processing_token=NULL,lease_until=NULL WHERE document_id=? AND processing_token=?", (message, row["document_id"], token))
                conn.commit()
                failed += 1
                errors.append({"document_id": row["document_id"], "error": message})
        return {"status": "PENDING" if failed else "SYNCED", "synced": synced, "failed": failed,
                "pending": outbox_status(conn)["pending"], "errors": errors}


def reconcile_selected(conn, limit=100):
    """Automatic consumer hook. SQL job commit remains durable during an outage."""
    if os.getenv("RAG_SEARCH_BACKEND", "sqlite").strip().lower() != "opensearch":
        return {"status": "DISABLED", "synced": 0, "failed": 0}
    try:
        return OpenSearchStore.from_env().reconcile(conn, limit)
    except (OpenSearchError, ValueError) as error:
        return {"status": "PENDING", "synced": 0, "failed": 1,
                "pending": outbox_status(conn)["pending"], "error": str(error)}