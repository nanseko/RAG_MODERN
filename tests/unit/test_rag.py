from rag.graph import check_evidence, map_citations, run_rag


class StubClient:
    """judge: '고온' 포함 시 관련. generate: 고정 답변."""

    def __init__(self):
        self.judged = []

    def judge(self, question, text) -> bool:
        self.judged.append(text)
        return "고온" in (text or "")

    def generate(self, question, sources) -> str:
        return "요약: 확인됨 " + " ".join(f"[SOURCE_{i + 1:02d}]"
                                        for i in range(len(sources)))


def _seed(conn):
    from ingestion.indexer import index_document
    docs = [("d1", "고온 구간에서 출력이 감소했다"),
            ("d2", "오늘 점심 메뉴는 김치찌개다"),
            ("d3", "고온 방열 구조를 개선했다")]
    for did, text in docs:
        index_document(conn, {"document_id": did, "title": did},
                       [{"text": text, "title": did}])


def test_judge_loop_excludes_irrelevant(tmp_path):
    from database.db import get_conn
    conn = get_conn(str(tmp_path / "j.sqlite3"))
    _seed(conn)
    stub = StubClient()
    out = run_rag("고온 출력", ["all"], conn, client=stub,
                  target_min=3, target_max=5, max_cycles=3)
    conn.close()
    ids = [s["document_id"] for s in out["sources"]]
    assert "d2" not in ids
    assert set(ids) == {"d1", "d3"}
    assert any("김치" in t for t in stub.judged)
    assert out["cycles"] >= 2


def test_judge_loop_stops_when_enough(tmp_path):
    from database.db import get_conn
    conn = get_conn(str(tmp_path / "j2.sqlite3"))
    from ingestion.indexer import index_document
    for i in range(5):
        index_document(conn, {"document_id": f"g{i}", "title": f"g{i}"},
                       [{"text": f"고온 사례 {i}번 출력 기록", "title": f"g{i}"}])
    stub = StubClient()
    out = run_rag("고온 출력", ["all"], conn, client=stub,
                  target_min=3, target_max=5, max_cycles=3)
    conn.close()
    assert out["cycles"] == 1 and 3 <= len(out["sources"]) <= 5


def test_invalid_source_id_rejected():
    srcs = [{"sid": "SOURCE_01", "title": "보고서", "version": "Rev.C", "page": 32}]
    out = map_citations("원인 [SOURCE_01] 확인 [SOURCE_99]", srcs)
    assert "보고서" in out and "SOURCE_99" not in out


def test_no_evidence_abstains():
    assert check_evidence([]) is False
    assert check_evidence([{"text": "x"}]) is True
