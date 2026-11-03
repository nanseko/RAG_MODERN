"""RRF 융합 가중치: 어떤 신호를 신뢰할지 정하는 지점.

BM25는 코드 정확일치에 강하고, dense는 비슷한 서술에 강하다. 환경에 따라
가중치를 다르게 주는 것이 HyDE의 핵심이므로, 여기에 한곳에 모아 둔다.
"""
def rrf_fuse(rank_lists, k=60, weights=None):
    scores: dict[str, float] = {}
    for i, lst in enumerate(rank_lists):
        w = 1.0 if weights is None else weights[i]
        for rank, cid in enumerate(lst, start=1):
            scores[cid] = scores.get(cid, 0) + w * 1.0 / (k + rank)
    return sorted(scores, key=lambda x: scores[x], reverse=True)