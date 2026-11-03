from database.db import get_conn
from ingestion.classify import METADATA_VERSION, classify, propagate
from ingestion.edges import extract_edges, store_edges
from ingestion.refresh import refresh_stale


def test_migrate_adds_taxonomy_columns_and_edges(tmp_path):
    conn = get_conn(str(tmp_path / "t.sqlite3"))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(documents)").fetchall()}
    assert {"category", "metadata_version"} <= cols
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert "document_edges" in tables


def test_classify_rules():
    assert classify("고온시험 결과", "고온시험_결과.pdf", "", {}) == "시험"
    assert classify("장애 보고서", "장애.docx", "", {}) == "이슈·장애"
    assert classify("사용자 매뉴얼", "매뉴얼.hwpx", "", {}) == "매뉴얼"
    assert classify("주간 공지", "공지.txt", "", {}) == "회의·공지"
    assert classify("무제", "a.md", "오늘 날씨", {}) == "기타"


def test_propagate_knn_assigns_nearest_label():
    labeled = [([1.0, 0.0], "시험"), ([0.0, 1.0], "매뉴얼")]
    assert propagate([0.9, 0.1], labeled) == "시험"
    assert propagate([0.1, 0.9], labeled) == "매뉴얼"
    assert propagate([0.5, 0.5], labeled, threshold=0.99) == "기타"


def test_edges_reference_and_store(tmp_path):
    conn = get_conn(str(tmp_path / "t.sqlite3"))
    conn.execute("INSERT INTO documents(document_id,file_name) VALUES('d1','원본.md'),('d2','참고.md')")
    conn.commit()
    edges = extract_edges("d1", "자세한 내용은 참고.md와 ABC-1234 문서를 보라",
                          {"참고.md": "d2"})
    store_edges(conn, edges)
    rows = conn.execute("SELECT from_doc,to_doc,kind FROM document_edges").fetchall()
    conn.close()
    assert ("d1", "d2", "references") in [(r[0], r[1], r[2]) for r in rows]


def test_refresh_stale_metadata(tmp_path):
    conn = get_conn(str(tmp_path / "t.sqlite3"))
    conn.execute("INSERT INTO documents(document_id,file_name,metadata_version) VALUES('d1','a.md','v0')")
    conn.execute("INSERT INTO chunks(chunk_id,document_id,text) VALUES('c1','d1','ERR_TX_041 발생 기록')")
    conn.commit()
    out = refresh_stale(conn)
    row = conn.execute("SELECT metadata_version,category FROM documents WHERE document_id='d1'").fetchone()
    ids = conn.execute("SELECT error_codes FROM chunks WHERE chunk_id='c1'").fetchone()
    conn.close()
    assert out["refreshed"] == 1 and row[0] == METADATA_VERSION
    assert "ERR_TX_041" in ids[0]
