import json
import pytest

from database.db import get_conn
from ingestion.indexer import index_document
from rag.graph import run_rag


class Accept:
    def judge(self, question, text):
        return True

    def generate(self, question, sources):
        return " ".join(f"[{s['sid']}]" for s in sources)


def test_troubleshooting_sources_count_unique_files(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_QWEN", "0")
    conn = get_conn(str(tmp_path / "trouble.sqlite3"))
    try:
        for i in range(3):
            index_document(conn, {"document_id": f"case{i}"}, [
                {"heading_path": "증상", "text": f"레이더 출력 감소 사례 {i}"},
                {"heading_path": "원인", "text": f"냉각팬 정지 사례 {i}"},
                {"heading_path": "조치", "text": f"냉각팬 교체 사례 {i}"},
            ])
        out = run_rag("레이더 출력 감소", ["all"], conn, client=Accept(),
                      top_k=3, target_min=3, target_max=3, max_cycles=1)
        assert len({s["document_id"] for s in out["sources"]}) == 3
        assert all("냉각팬 정지" in s["content"] and "냉각팬 교체" in s["content"]
                   for s in out["sources"])
    finally:
        conn.close()


def test_duplicate_troubleshooting_file_cannot_satisfy_source_target(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_QWEN", "0")
    conn = get_conn(str(tmp_path / "duplicates.sqlite3"))
    try:
        for did in ("original", "copy"):
            index_document(conn, {"document_id": did, "content_hash": "same-file"}, [
                {"heading_path": "원인", "text": "레이더 냉각팬 정지"},
                {"heading_path": "조치", "text": "레이더 냉각팬 교체"},
            ])
        out = run_rag("레이더", ["all"], conn, client=Accept(),
                      target_min=3, target_max=5, max_cycles=2)
        assert len(out["sources"]) == 1
        assert out["answer_status"] == "insufficient_evidence"
    finally:
        conn.close()


def test_indexing_stores_current_classification(tmp_path, monkeypatch):
    from ingestion.classify import METADATA_VERSION
    monkeypatch.setenv("USE_QWEN", "0")
    conn = get_conn(str(tmp_path / "classification.sqlite3"))
    try:
        index_document(conn, {"document_id": "case", "title": "장애 보고서"},
                       [{"text": "레이더 고장 원인을 조사했다"}])
        row = conn.execute("SELECT category,metadata_version FROM documents").fetchone()
        assert row["category"] == "이슈·장애"
        assert row["metadata_version"] == METADATA_VERSION
    finally:
        conn.close()


def test_refresh_upgrades_troubleshooting_metadata_and_queues_search(tmp_path):
    from ingestion.refresh import refresh_stale
    conn = get_conn(str(tmp_path / "refresh.sqlite3"))
    try:
        conn.execute("INSERT INTO documents(document_id,title,metadata_version) VALUES('case','장애 보고서','v0')")
        for i, (heading, text) in enumerate([
            ("증상", "AN/TPS-43 출력 감소"),
            ("원인", "냉각팬 정지"),
            ("조치", "냉각팬 교체 후 정상 확인"),
        ]):
            conn.execute("INSERT INTO chunks(chunk_id,document_id,heading_path,text,chunk_index) VALUES(?,?,?,?,?)",
                         (f"chunk{i}", "case", heading, text, i))
        conn.commit()
        refresh_stale(conn)
        chunks = conn.execute("SELECT role,equipment FROM chunks ORDER BY chunk_index").fetchall()
        assert [c["role"] for c in chunks] == ["symptom", "cause", "action"]
        assert json.loads(chunks[0]["equipment"]) == ["AN/TPS-43"]
        row = conn.execute("SELECT outcome,trouble_json FROM documents").fetchone()
        assert row["outcome"] == "resolved"
        assert json.loads(row["trouble_json"])["cause"] == "냉각팬 정지"
        assert conn.execute("SELECT operation FROM search_outbox WHERE document_id='case'").fetchone()[0] == "upsert"
        assert refresh_stale(conn)["refreshed"] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("text", [
    "고장 미해결", "출력은 정상 아님", "장비 복구 실패", "조치 완료 예정",
    "증상이 해결되지 않았다", "정상 동작하지 않음",
])
def test_unresolved_case_does_not_receive_resolved_outcome(text):
    from ingestion.trouble import outcome_of
    assert outcome_of(text) == "unknown"


def test_search_backend_name_uses_normalized_environment(monkeypatch):
    from retrieval.hybrid import hybrid_search
    monkeypatch.setenv("RAG_SEARCH_BACKEND", " SQLite ")
    monkeypatch.setenv("USE_QWEN", "0")
    docs = [{"chunk_id": "one", "text": "레이더 고장", "allowed_groups": ["all"]}]
    assert hybrid_search("레이더", docs, ["all"])[0]["chunk_id"] == "one"


def test_reconcile_backend_name_uses_normalized_environment(tmp_path, monkeypatch):
    import retrieval.opensearch_store as store
    monkeypatch.setenv("RAG_SEARCH_BACKEND", " OpenSearch ")
    sentinel = {"pending": 7}
    from types import SimpleNamespace
    monkeypatch.setattr(store.OpenSearchStore, "from_env", lambda: SimpleNamespace(
        reconcile=lambda conn, limit: sentinel))
    conn = get_conn(str(tmp_path / "backend.sqlite3"))
    try:
        assert store.reconcile_selected(conn) == sentinel
    finally:
        conn.close()


def test_refined_search_preserves_original_question_at_review_boundaries(tmp_path, monkeypatch):
    import rag.graph as graph
    monkeypatch.setenv("USE_QWEN", "0")
    original = "AN/TPS-43 출력이 감소했고 냉각팬이 정지했다. 어떻게 조치하나?"
    refined = "레이더 냉각팬 출력 감소"
    searches, reranks, questions = [], [], []
    real_search, real_rerank = graph.hybrid_search, graph.rerank

    def search(question, *args, **kwargs):
        searches.append(question)
        return real_search(question, *args, **kwargs)

    def rerank(question, *args, **kwargs):
        reranks.append(question)
        return real_rerank(question, *args, **kwargs)

    class Recording(Accept):
        def judge(self, question, text):
            questions.append(question)
            return True

        def rewrite(self, question, rejected):
            questions.append(question)
            return "대체 검색어"

        def generate(self, question, sources):
            questions.append(question)
            return super().generate(question, sources)

    monkeypatch.setattr(graph, "hybrid_search", search)
    monkeypatch.setattr(graph, "rerank", rerank)
    conn = get_conn(str(tmp_path / "canonical.sqlite3"))
    try:
        index_document(conn, {"document_id": "one"}, [{"text": "레이더 냉각팬 교체"}])
        graph.run_rag(original, ["all"], conn, client=Recording(),
                      search_query=refined, target_min=3, max_cycles=2)
        assert searches == [refined, "대체 검색어"]
        assert reranks == [original, original]
        assert questions and set(questions) == {original}
    finally:
        conn.close()


def test_markdown_parser_keeps_separate_troubleshooting_sections(tmp_path):
    from ingestion.parser import parse_file
    from ingestion.trouble import assign_roles
    path = tmp_path / "case.md"
    path.write_text("# 레이더 고장\n## 증상\n출력 감소\n\n조건 | 값\n--- | ---\n온도 | 85\n## 원인\n냉각팬 정지\n## 조치\n냉각팬 교체\n", encoding="utf8")
    sections = parse_file(str(path))
    assert [s["heading_path"] for s in sections] == [
        "레이더 고장 / 증상", "레이더 고장 / 원인", "레이더 고장 / 조치"]
    assert [s["role"] for s in assign_roles(sections)] == ["symptom", "cause", "action"]
    assert "냉각팬 정지" not in sections[0]["text"]
    assert sections[1]["text"] == "냉각팬 정지"
    assert sections[2]["text"] == "냉각팬 교체"
    assert "조건 | 값\n--- | ---\n온도 | 85" in sections[0]["text"]


def test_spreadsheet_parser_preserves_zero_and_false_values(tmp_path):
    import openpyxl
    from ingestion.parser import parse_file
    path = tmp_path / "measurements.xlsx"
    workbook = openpyxl.Workbook()
    workbook.active.append(["오류 수", "동작 여부"])
    workbook.active.append([0, False])
    workbook.save(path)
    workbook.close()
    section = parse_file(str(path))[0]
    assert "| 0 | False |" in section["text"]


def test_source_budget_reserves_matching_cause_and_action_context(monkeypatch):
    import rag.graph as graph
    import ingestion.chunker as chunker
    monkeypatch.setattr(graph, "_tokens", len)
    monkeypatch.setattr(chunker, "_tokens", len)
    context = [
        {"document_id": "case", "chunk_id": "symptom", "role": "symptom", "page_number": 1, "text": "출력감소 " + "s" * 644},
        {"document_id": "case", "chunk_id": "cause", "role": "cause", "page_number": 2, "text": "냉각팬정지 " + "c" * 644},
        {"document_id": "case", "chunk_id": "action", "role": "action", "page_number": 3, "text": "냉각팬교체 " + "a" * 644},
    ]
    source = graph._source(context[0], context, 1, 1200)
    assert len(source["content"]) <= 1200
    assert all(word in source["content"] for word in ("출력감소", "냉각팬정지", "냉각팬교체"))


@pytest.mark.parametrize("omitted_role", ["other", "cause"])
def test_source_pages_only_list_context_included_in_budget(monkeypatch, omitted_role):
    import rag.graph as graph
    import ingestion.chunker as chunker
    monkeypatch.setattr(graph, "_tokens", len)
    monkeypatch.setattr(chunker, "_tokens", len)
    context = [
        {"document_id": "case", "chunk_id": "cause", "role": "cause", "page_number": 2, "text": "원인"},
        {"document_id": "case", "chunk_id": "action", "role": "action", "page_number": 3, "text": "조치"},
        {"document_id": "case", "chunk_id": "appendix", "role": omitted_role, "page_number": 99, "text": "부록 " + "x" * 100},
    ]
    source = graph._source(context[0], context, 1, 6)
    assert source["content"] == "원인\n\n조치"
    assert source["pages"] == [2, 3]
