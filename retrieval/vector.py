import json
import math
import os

from models.runtime import model_path
from database.vector_store import EMBEDDING_VERSION


def _qwen_available():
    return os.getenv("USE_QWEN", "1") == "1" and os.path.isdir(model_path("models/qwen3-embedding-0.6b", "RAG_EMBEDDING_MODEL_DIR"))


def _stored_vec(doc):
    raw = doc.get("embedding")
    if not raw:
        return None
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(value, list) or not value or not all(
                isinstance(x, (int, float)) and math.isfinite(x) for x in value):
            return None
        return value
    except (ValueError, TypeError):
        return None


def cosine_topk(query_vec, docs, k=50):
    scored = []
    qnorm = math.sqrt(sum(x * x for x in query_vec))
    for doc in docs:
        vector = _stored_vec(doc)
        if vector is None or len(vector) != len(query_vec):
            continue
        norm = math.sqrt(sum(x * x for x in vector)) * qnorm
        if norm:
            score = sum(a * b for a, b in zip(query_vec, vector)) / norm
            scored.append((score, {**doc, "vector_score": score}))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [d for _, d in scored[:k]]


def _qwen_search(query, docs, k, conn=None):
    from models.embedding import embed
    qvec = embed([query], is_query=True)[0]
    by_id = {d["chunk_id"]: d for d in docs}
    scored = {}
    if conn is not None:
        from database.vector_store import search
        scored.update(search(conn, qvec, list(by_id), k))
    current = [d for d in docs if d.get("embedding_version", EMBEDDING_VERSION) == EMBEDDING_VERSION]
    for doc in cosine_topk(qvec, current, k):
        scored.setdefault(doc["chunk_id"], doc["vector_score"])
    # Legacy vectors used incorrect pooling; do not compare them against corrected query vectors.
    missing = [d for d in docs if d["chunk_id"] not in scored and
               (d.get("embedding_version") != EMBEDDING_VERSION or _stored_vec(d) is None)]
    if missing:
        vectors = embed([d.get("text", "") for d in missing])
        for d, vector in zip(missing, vectors):
            norm = math.sqrt(sum(x * x for x in qvec) * sum(x * x for x in vector))
            scored[d["chunk_id"]] = sum(a * b for a, b in zip(qvec, vector)) / norm if norm else 0
    order = sorted(scored, key=scored.get, reverse=True)
    return [{**by_id[cid], "vector_score": scored[cid]} for cid in order[:k]]


def _tfidf_search(query, docs, k):
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    texts = [d.get("text") or "" for d in docs]
    vec = TfidfVectorizer(analyzer="char", ngram_range=(2, 4)).fit(texts + [query])
    sims = cosine_similarity(vec.transform([query]), vec.transform(texts))[0]
    ranked = sorted(zip(docs, sims), key=lambda item: item[1], reverse=True)
    return [{**d, "vector_score": float(score)} for d, score in ranked[:k]]


def vector_search(query, docs, k=50, conn=None):
    if not docs:
        return []
    if _qwen_available():
        return _qwen_search(query, docs, k, conn=conn)
    return _tfidf_search(query, docs, k)
