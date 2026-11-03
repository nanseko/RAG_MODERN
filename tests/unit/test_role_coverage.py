from retrieval.coverage import fill_role_gaps, role_context, select_representatives


def _c(doc, cid, role, idx=0, text="t", score=None):
    return {"document_id": doc, "content_hash": doc, "chunk_id": cid, "role": role,
            "chunk_index": idx, "text": text, "document_version": "v1",
            "rerank_score": score if score is not None else 1.0}


def test_representatives_cover_cause_and_action():
    ranked = [_c("d1", "s1", "symptom", 0, "출력 감소"),
              _c("d1", "c1", "cause", 1, "방열 불량"),
              _c("d1", "a1", "action", 2, "방열판 교체"),
              _c("d2", "s2", "symptom", 0, "다른 증상"),
              _c("d3", "s3", "symptom", 0, "또 다른 증상")]
    out = select_representatives(ranked, top_k=3)
    roles = {c["role"] for representative in out for c in role_context(representative, ranked)}
    assert "cause" in roles and "action" in roles
    assert len({c["document_id"] for c in out}) == 3


def test_gap_fill_precedes_document_diversity():
    ranked = [_c(f"d{i}", f"s{i}", "symptom") for i in range(5)]
    ranked.append(_c("d9", "a9", "action", 9, "조치 본문"))
    out = select_representatives(ranked, top_k=3)
    assert any(c["chunk_id"] == "a9" for c in out)
    assert len({c["document_id"] for c in out}) >= 2


def test_no_invented_roles_when_pool_lacks_them():
    ranked = [_c(f"d{i}", f"s{i}", "symptom") for i in range(4)]
    out = select_representatives(ranked, top_k=3)
    assert {c["role"] for c in out} == {"symptom"}
    assert len(out) == 3


def test_document_diversity_preserved_among_symptoms():
    ranked = [_c(f"d{i}", f"s{i}", "symptom") for i in range(6)]
    out = select_representatives(ranked, top_k=4)
    assert len({c["document_id"] for c in out}) == 4


def test_fill_role_gaps_is_idempotent_and_bounded():
    ranked = [_c("d1", "s1", "symptom"), _c("d1", "c1", "cause"), _c("d2", "a2", "action")]
    once = fill_role_gaps([], ranked, top_k=4)
    twice = fill_role_gaps(once, ranked, top_k=4)
    assert [c["chunk_id"] for c in once] == [c["chunk_id"] for c in twice]


def test_role_context_pulls_companion_roles_from_same_doc():
    permitted = [_c("d1", "s1", "symptom", 0, "증상 본문"),
                 _c("d1", "c1", "cause", 1, "원인 본문"),
                 _c("d1", "a1", "action", 2, "조치 본문"),
                 _c("d1", "n1", "other", 3, "부록"),
                 _c("d2", "x1", "cause", 0, "다른 문서 원인")]
    ctx = role_context(_c("d1", "c1", "cause", 1, "원인 본문"), permitted)
    ids = {c["chunk_id"] for c in ctx}
    assert ids == {"s1", "c1", "a1"}


def test_role_context_caps_boilerplate_in_long_docs():
    permitted = [_c("d1", "s1", "symptom", 0)] + [
        _c("d1", f"n{i}", "other", i) for i in range(1, 60)] + [
        _c("d1", "c1", "cause", 70), _c("d1", "c2", "cause", 71)]
    ctx = role_context(_c("d1", "s1", "symptom", 0), permitted)
    assert len(ctx) == 3  # self + one symptom peer slot + one cause


def test_rag_source_carries_cause_and_action_for_symptom_hit(tmp_path):
    from database.db import get_conn
    from ingestion.indexer import index_document
    from rag.graph import run_rag
    conn = get_conn(str(tmp_path / "cov.sqlite3"))
    index_document(conn, {"document_id": "case1", "title": "레이더 고장기록"}, [
        {"text": "출력이 40% 감소했다", "title": "레이더 고장기록", "heading_path": "증상"},
        {"text": "냉각팬 정지导致 방열 불량", "title": "레이더 고장기록", "heading_path": "원인"},
        {"text": "냉각팬을 교체하고 출력 정상 확인", "title": "레이더 고장기록", "heading_path": "조치"},
    ])
    stub = StubAccept()
    out = run_rag("레이더 출력 감소", ["all"], conn, client=stub,
                  target_min=1, target_max=3, max_cycles=1)
    blob = " ".join(s["content"] for s in out["sources"])
    conn.close()
    assert "냉각팬 정지" in blob and "냉각팬을 교체" in blob


class StubAccept:
    """judge 항상 통과, generate 출처 나열."""

    def __init__(self):
        self.judged = []

    def judge(self, question, text) -> bool:
        self.judged.append(text)
        return True

    def generate(self, question, sources) -> str:
        return " ".join(f"[{s['sid']}]" for s in sources)
