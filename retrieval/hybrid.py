import json
import os

from retrieval.bm25 import bm25_search
from retrieval.rrf import rrf_fuse
from retrieval.vector import vector_search


def _groups(doc):
    groups = doc.get("allowed_groups", ["all"])
    if isinstance(groups, str):
        try:
            groups = json.loads(groups)
        except (ValueError, TypeError):
            return set()  # malformed ACL must not make a document public
    if not isinstance(groups, list):
        return set()
    return set(groups)


def acl_filter(docs, user_groups):
    groups = set(user_groups or [])
    return [d for d in docs if "all" in _groups(d) or groups & _groups(d)]


def dedup(docs):
    seen, out = set(), []
    for doc in docs:
        key = (doc.get("document_id", ""), (doc.get("text") or "").strip())
        if key[1] and key not in seen:
            seen.add(key)
            out.append(doc)
    return out


def hybrid_search(query, docs, user_groups=None, k=50, conn=None):
    docs = acl_filter(docs, user_groups or [])
    if not docs:
        return []
    backend = os.getenv("RAG_SEARCH_BACKEND", "sqlite").strip().lower()
    if backend == "opensearch":
        from retrieval.opensearch_store import OpenSearchStore
        return OpenSearchStore.from_env().search(query, docs, user_groups or [], k)
    if backend != "sqlite":
        raise ValueError("RAG_SEARCH_BACKEND must be sqlite or opensearch")
    if getattr(conn, "backend", "sqlite") == "postgresql":
        raise ValueError("PostgreSQL requires RAG_SEARCH_BACKEND=opensearch")
    keyword = bm25_search(query, docs, k)
    dense = vector_search(query, docs, k, conn=conn)
    rankings = [[d["chunk_id"] for d in result] for result in (keyword, dense)]
    order = rrf_fuse(rankings)
    by_id = {d["chunk_id"]: d for d in docs}
    scores = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking, 1):
            scores[cid] = scores.get(cid, 0.0) + 1 / (60 + rank)
    return [{**by_id[cid], "retrieval_score": scores[cid]} for cid in order[:k]]
