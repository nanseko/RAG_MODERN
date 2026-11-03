import os
import re

from models.runtime import model_path

CODE = re.compile(r"[A-Z]{2,5}-\d{3,6}|ERR_[A-Z]+_\d+")


def _qwen_available():
    return os.getenv("USE_QWEN", "1") == "1" and os.path.isdir(
        model_path("models/qwen3-reranker-0.6b", "RAG_RERANKER_MODEL_DIR"))


def _qwen_rerank(query, docs, top_n):
    from models.reranker import score_pairs
    scores = []
    for i in range(0, len(docs), 4):
        scores.extend(score_pairs([(query, d.get("text", "")) for d in docs[i:i + 4]]))
    ranked = sorted(zip(scores, docs), key=lambda item: item[0], reverse=True)
    return [{**d, "rerank_score": float(score)} for score, d in ranked[:top_n]]


def _heuristic_rerank(query, docs, top_n):
    codes = set(CODE.findall(query))
    qtokens = set(re.findall(r"[\w-]+", query.lower()))
    scored = []
    for d in docs:
        text = d.get("text", "")
        boost = sum(2.0 for code in codes if code in text)
        overlap = len(qtokens & set(re.findall(r"[\w-]+", text.lower()))) / max(1, len(qtokens))
        scored.append((boost + overlap, d))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [{**d, "rerank_score": score} for score, d in scored[:top_n]]


def rerank(query, docs, top_n=8):
    if not docs:
        return []
    return _qwen_rerank(query, docs, top_n) if _qwen_available() else _heuristic_rerank(query, docs, top_n)
