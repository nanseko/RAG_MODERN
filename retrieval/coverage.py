"""역할 커버리지 대표 선택: 증상만 남고 원인/조치가 떨어지는 것을 막는다.

기존에는 순위 1위 청크만 문서 대표로 뽑혔다. 트러블슈팅에서는 같은 문서의
원인-조치 쌍이 증상과 함께 있어야 답이 나오므로, 역할이 비어 있으면
후보 풀에서 해당 역할을 채워 넣고 그래도 부족할 때만 문서 다양성으로 채운다.
"""
NEEDED = ("cause", "action")
CONTEXT_ROLES = ("symptom", "cause", "action", "verification")


def _role(c):
    return c.get("role") or "other"


def fill_role_gaps(chosen, pool, top_k, roles=NEEDED):
    """chosen에 없는 역할을 pool에서 최대 1건씩 보충한다."""
    out = list(chosen)
    have = {_role(c) for c in out}
    for role in roles:
        if role in have or len(out) >= top_k:
            continue
        hit = next((c for c in pool if _role(c) == role and c["chunk_id"] not in
                    {x["chunk_id"] for x in out}), None)
        if hit is not None:
            out.append(hit)
            have.add(role)
    return out


def select_representatives(ranked, top_k):
    """역할 커버리지 우선, 고유 파일당 대표 1건. 최대 top_k건.

    역할 보충이 먼저 예산을 써야 증상만으로 top_k가 꽉 차는 일이 없다.
    """
    chosen, seen_docs = [], set()
    # Companion roles from one file belong in role_context, not separate sources.
    # Deduplicate before applying the file budget so duplicates cannot use a slot.
    preferred = fill_role_gaps([], ranked, len(NEEDED))
    for c in preferred + list(ranked):
        if len(chosen) >= top_k:
            break
        key = c.get("content_hash") or c["document_id"]
        if key in seen_docs:
            continue
        seen_docs.add(key)
        chosen.append(c)
    return chosen


def role_context(chosen, permitted, window=1, per_role=1):
    """같은 문서에서 보완 역할을 훑어온다. 자식 위치 기반 확장은 parent가 담당.

    장문서 전체를 끌어오지 않도록 역할마다 최대 per_role건만 붙인다.
    """
    from retrieval.parent import expand_parent
    base = expand_parent([chosen], permitted, window)
    ids = {c["chunk_id"] for c in base}
    picked, counts = [], {}
    for c in sorted(permitted, key=lambda x: x.get("chunk_index", 0)):
        role = _role(c)
        if c["document_id"] != chosen["document_id"] or c["chunk_id"] in ids:
            continue
        if role not in CONTEXT_ROLES or counts.get(role, 0) >= per_role:
            continue
        counts[role] = counts.get(role, 0) + 1
        picked.append(c)
    return base + picked
