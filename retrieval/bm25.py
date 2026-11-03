def bm25_search(query, docs, k=50):
    from rank_bm25 import BM25Okapi
    corpus = [(d.get("text") or "").split() for d in docs]
    if not corpus:
        return []
    bm = BM25Okapi(corpus)
    scores = bm.get_scores(query.split())
    ranked = sorted(zip(docs, scores), key=lambda x: x[1], reverse=True)
    top = [d for d, s in ranked[:k] if s > 0]
    return top or [d for d, _ in ranked[:k]]
