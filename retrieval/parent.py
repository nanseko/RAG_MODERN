def expand_parent(chosen, all_chunks, window=1):
    by_doc = {}
    for c in all_chunks:
        by_doc.setdefault(c["document_id"], []).append(c)
    for siblings in by_doc.values():
        siblings.sort(key=lambda c: c.get("chunk_index", 0))
    out, seen = [], set()
    for c in chosen:
        siblings = by_doc.get(c["document_id"], [c])
        i = next((k for k, s in enumerate(siblings) if s["chunk_id"] == c["chunk_id"]), 0)
        for sibling in siblings[max(0, i - window):i + window + 1]:
            if sibling["chunk_id"] not in seen:
                seen.add(sibling["chunk_id"])
                out.append(sibling)
    return out
