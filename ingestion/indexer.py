import hashlib
import json
import os
import uuid

from database.vector_store import EMBEDDING_VERSION, put, validate_vector
from ingestion.classify import METADATA_VERSION, classify
from ingestion.metadata import extract_identifiers
from retrieval.opensearch_store import enqueue_document


def _chunk_embeddings(texts):
    """Disabled explicitly for keyword-only demo. Runtime failures otherwise fail the job."""
    if os.getenv("USE_QWEN", "1") == "0":
        return [None] * len(texts)
    from models.embedding import embed
    return embed(texts, is_query=False)


def index_document(conn, doc, chunks, vectors=None, commit=True):
    if not chunks or not any(c.get("text", "").strip() for c in chunks):
        raise ValueError("no searchable text produced")
    # Compute all embeddings before touching the last active version.
    vecs = vectors if vectors is not None else _chunk_embeddings([c.get("text", "") for c in chunks])
    if len(vecs) != len(chunks):
        raise ValueError("embedding count mismatch")
    for v in vecs:
        if v is not None:
            validate_vector(v)
    did = doc.get("document_id") or uuid.uuid4().hex[:16]
    groups = doc.get("allowed_groups", ["all"])
    from ingestion.trouble import assign_roles, document_summary
    assign_roles(chunks)
    trouble_json, outcome = document_summary(chunks)
    full_text = "\n".join(c.get("text", "") for c in chunks)
    category = classify(doc.get("title", did), doc.get("file_name", ""),
                        full_text, extract_identifiers(full_text))
    h = doc.get("content_hash") or hashlib.sha256(
        "\n".join(c.get("text", "") for c in chunks).encode("utf8")).hexdigest()
    conn.execute("SAVEPOINT replace_document")
    try:
        conn.execute("DELETE FROM chunks WHERE document_id=?", (did,))
        conn.execute(
            "INSERT OR REPLACE INTO documents(document_id,title,file_name,file_type,version,"
            "content_hash,status,allowed_groups,source_uri,markdown_path,embedding_version,original_path,"
            "trouble_json,outcome,category,metadata_version) VALUES(?,?,?,?,?,?,'ACTIVE',?,?,?,?,?,?,?,?,?)",
            (did, doc.get("title", did), doc.get("file_name", ""), doc.get("file_type", ""),
             doc.get("version", "v1"), h, json.dumps(groups), doc.get("source_uri"),
             doc.get("markdown_path"), EMBEDDING_VERSION if any(v is not None for v in vecs) else "disabled",
             doc.get("original_path"), trouble_json, outcome, category, METADATA_VERSION))
        conn.execute("INSERT OR REPLACE INTO document_versions VALUES(?,?,?,?,?,?)",
                     (did, doc.get("version", "v1"), doc.get("original_path"), doc.get("markdown_path"), json.dumps(groups), h))
        for i, (c, v) in enumerate(zip(chunks, vecs)):
            cid = f"{did}:{doc.get('version', 'v1')}:{i}"
            ids = extract_identifiers(c.get("text", ""))
            version = EMBEDDING_VERSION if v is not None else "disabled"
            conn.execute(
                "INSERT INTO chunks(chunk_id,document_id,text,title,heading_path,page_number,"
                "parent_chunk_id,part_numbers,error_codes,allowed_groups,document_version,embedding,"
                "embedding_version,chunk_index,slide_number,sheet_name,role,equipment)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, did, c.get("text", ""), c.get("title") or doc.get("title", did),
                 c.get("heading_path", ""), c.get("page_number"), c.get("parent_chunk_id"),
                 json.dumps(ids["part_numbers"]), json.dumps(ids["error_codes"]), json.dumps(groups),
                 doc.get("version", "v1"), json.dumps(v) if v is not None else None, version,
                 i, c.get("slide_number"), c.get("sheet_name"), c.get("role", "other"),
                 json.dumps(ids["equipment"], ensure_ascii=False)))
            if v is not None and getattr(conn, "backend", "sqlite") == "sqlite":
                put(conn, cid, did, v)
        enqueue_document(conn, did)
        conn.execute("RELEASE SAVEPOINT replace_document")
        if commit:
            conn.commit()
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT replace_document")
        conn.execute("RELEASE SAVEPOINT replace_document")
        raise
    return did
