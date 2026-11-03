import re
import uuid
from urllib.parse import quote

from app.settings import get_settings
from ingestion.chunker import _tokens, _split
from models.gemma_client import get_client
from rag.answer_review import validate_review, approved
from retrieval.boost import boost_scores
from retrieval.coverage import role_context, select_representatives
from retrieval.hybrid import acl_filter, dedup, hybrid_search
from retrieval.rerank import rerank

CITATION = re.compile(r"\[SOURCE_[^\]\r\n]+\]", re.IGNORECASE)


def check_evidence(ctx):
    return any(c.get("text", "").strip() for c in ctx)


def map_citations(text, sources):
    by_sid = {s["sid"]: s for s in sources}
    def replace(match):
        s = by_sid.get(match[0][1:-1])
        if s is None:
            return ""
        pages = s.get("pages") or ([s["page"]] if s.get("page") else [])
        location = " / p." + ",".join(map(str, pages)) if pages else ""
        return f"{s['title']} {s.get('version', '')}{location}"
    return CITATION.sub(replace, text)


def _load_rows(conn):
    rows = [dict(r) for r in conn.execute(
        "SELECT c.*,d.version,d.source_uri,d.markdown_path,d.file_name,d.content_hash,"
        "d.allowed_groups AS doc_allowed_groups,d.outcome AS doc_outcome,"
        "d.category AS doc_category "
        "FROM chunks c JOIN documents d ON d.document_id=c.document_id "
        "WHERE d.status='ACTIVE' ORDER BY c.document_id,c.chunk_index,c.chunk_id")]
    for r in rows:
        # Authoritative ACL lives on documents; chunk rows carry a stale copy.
        r["allowed_groups"] = r.pop("doc_allowed_groups")
        # Resolution outcome is a document-level fact used to rank resolved cases first.
        r["outcome"] = r.pop("doc_outcome") or r.get("outcome") or "unknown"
        r["category"] = r.pop("doc_category") or r.get("category") or "기타"
    return rows


def _source(c, context, i, budget):
    text = "\n\n".join(x["text"] for x in context)
    included = context
    if _tokens(text) > budget:
        # Reserve a share for the matching passage and the cause/action pair.
        # Truncating the concatenation can silently discard the entire action.
        priority = [c]
        ids = {c["chunk_id"]}
        for role in ("cause", "action"):
            if any(x.get("role") == role for x in priority):
                continue
            companion = next((x for x in context if x.get("role") == role
                              and x["chunk_id"] not in ids), None)
            if companion is not None:
                priority.append(companion)
                ids.add(companion["chunk_id"])
        ordered = priority + [x for x in context if x["chunk_id"] not in ids]
        text, included = "", []
        separator_tokens = _tokens("\n\n")
        for position, chunk in enumerate(ordered):
            separator = "\n\n" if text else ""
            free = budget - _tokens(text) - (_tokens(separator) if text else 0)
            if free <= 0:
                break
            future = max(0, len(priority) - position - 1)
            limit = max(1, (free - separator_tokens * future) // (future + 1))
            passage = next(_split(chunk["text"], limit), "")
            # Token boundaries can change at joins; enforce the final hard cap.
            while passage and _tokens(text + separator + passage) > budget:
                passage = passage[:-1]
            if passage.strip():
                text += separator + passage
                included.append(chunk)
    return {
        "sid": f"SOURCE_{i:02d}", "document_id": c["document_id"], "chunk_id": c["chunk_id"],
        "title": c.get("file_name") or c.get("title") or c["document_id"], "page": c.get("page_number"),
        "version": c.get("version", "v1"), "content": text,
        "pages": sorted({x["page_number"] for x in included if x.get("page_number")}),
        "score": c.get("rerank_score", c.get("retrieval_score", 0.0)),
        "download_url": f"/api/v1/documents/{c['document_id']}/download?version={quote(c.get('version', 'v1'), safe='')}",
        "markdown_url": f"/api/v1/documents/{c['document_id']}/markdown?version={quote(c.get('version', 'v1'), safe='')}",
    }


def run_rag(query, user_groups, conn, top_k=None, client=None,
            target_min=None, target_max=None, max_cycles=None, search_query=None):
    settings = get_settings()
    top_k = top_k or settings.top_k
    target_min = settings.target_min if target_min is None else target_min
    target_max = settings.target_max if target_max is None else target_max
    max_cycles = settings.max_cycles if max_cycles is None else max_cycles
    if not (1 <= target_min <= target_max <= 5 and 1 <= max_cycles <= 3 and top_k > 0):
        raise ValueError("invalid retrieval limits")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must not be blank")
    query = query.strip()
    if search_query is not None and (not isinstance(search_query, str) or not search_query.strip()):
        raise ValueError("search_query must not be blank")
    owns_client = client is None
    client = client or get_client()
    base = {"request_id": uuid.uuid4().hex, "llm_mode": getattr(client, "mode", "custom")}
    rejected, accepted, kept = set(), set(), []
    excluded_files = set()
    rejected_info, trace = [], []
    q = query if search_query is None else search_query.strip()
    try:
        permitted = acl_filter(_load_rows(conn), user_groups)
        for cycle in range(1, max_cycles + 1):
            # Exclude documents BEFORE top-50, on every cycle.
            available = [r for r in permitted if r["document_id"] not in rejected | accepted
                         and (r.get("content_hash") or r["document_id"]) not in excluded_files]
            candidates = dedup(hybrid_search(q, available, user_groups, k=settings.candidates, conn=conn))
            # Boost exact part/error-code hits, same equipment, and resolved cases.
            boosted = boost_scores(query, candidates)
            ranked = rerank(query, boosted, len(boosted))
            # Pick unique files; role_context preserves each file's cause/action pair.
            representatives = select_representatives(ranked, top_k)
            approved_ids, rejected_ids = [], []
            for c in representatives:
                if len(kept) >= target_max:
                    break
                # Expand only ACL-permitted context; the judge sees the same context used by generation.
                context = role_context(c, permitted)
                source = _source(c, context, len(kept) + 1, max(1, settings.context_tokens // target_max))
                excluded_files.add(c.get("content_hash") or c["document_id"])
                if client.judge(query, source["content"]):  # ORIGINAL query, never rewrite
                    kept.append(source)
                    accepted.add(c["document_id"])
                    approved_ids.append(c["document_id"])
                else:
                    rejected.add(c["document_id"])
                    rejected_ids.append(c["document_id"])
                    rejected_info.append({"document_id": c["document_id"], "title": source["title"]})
            trace.append({"cycle": cycle, "search_query": q, "candidates": len(candidates),
                          "accepted_ids": approved_ids, "rejected_ids": rejected_ids,
                          "accepted_total": len(kept)})
            if len(kept) >= target_min:
                break
            if cycle < max_cycles:
                rewrite = getattr(client, "rewrite", None)
                q = rewrite(query, rejected_info) if rewrite else query + " 원인 조치 검증 결과"
        result = {**base, "sources": kept, "retrieval_count": len(kept), "cycles": len(trace),
                  "cycle_trace": trace, "rejected_count": len(rejected),
                  "answer_review_status": "not_applicable", "answer_revision_count": 0}
        if not kept:
            return {**result, "answer": "관련 근거를 찾을 수 없음.", "answer_status": "no_evidence"}
        raw = client.generate(query, kept)
        valid = {s["sid"] for s in kept}
        def citation_ok(text):
            used = {m[1:-1] for m in CITATION.findall(text)}
            return bool(used) and used <= valid
        if not citation_ok(raw):
            return {**result, "answer": "답변의 출처를 검증하지 못했습니다. 검토된 자료를 확인하세요.",
                    "answer_status": "citation_error"}
        reviewer = getattr(client, "review", None)
        result["answer_review_status"] = "mock" if base["llm_mode"] == "mock" else "not_checked"
        if callable(reviewer) and base["llm_mode"] != "mock":
            for attempt in range(2):
                verdict = validate_review(reviewer(query, raw, kept))
                if approved(verdict):
                    result["answer_review_status"] = "passed"
                    break
                result["answer_review_status"] = "failed"
                reviser = getattr(client, "revise", None)
                if attempt == 1 or not callable(reviser):
                    return {**result, "answer": "근거와 논리를 검토했으나 해결책을 확정하지 못했습니다. "
                            "아래 관련 자료를 확인하고 적용 조건을 추가로 확인하세요.",
                            "answer_status": "review_failed"}
                issues = verdict["issues"] or ["실패한 검토 항목: " + ", ".join(
                    key for key in ("grounded", "answers_question", "logically_consistent") if not verdict[key])]
                raw = reviser(query, raw, kept, issues)
                result["answer_revision_count"] += 1
                if not citation_ok(raw):
                    return {**result, "answer": "수정 답변의 출처를 검증하지 못했습니다. 검토된 자료를 확인하세요.",
                            "answer_status": "citation_error"}
        status = "answered" if len(kept) >= target_min else "insufficient_evidence"
        answer = map_citations(raw, kept)
        if status == "insufficient_evidence":
            answer = f"관련 자료를 {len(kept)}건만 확인했습니다. 자료가 부족해 답변 범위가 제한됩니다.\n\n" + answer
        return {**result, "answer": answer, "answer_status": status}
    finally:
        if owns_client:
            close = getattr(client, "close", None)
            if close:
                close()
