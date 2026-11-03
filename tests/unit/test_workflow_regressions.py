"""User-visible ingestion failures; tests use a temporary corpus/database."""
import json
import zipfile

import pytest

from database.db import get_conn
from ingestion.collector import scan_dir
from ingestion.pipeline import run_job, scan_once


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_QWEN", "0")
    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setenv("RAG_DOCS_DIR", str(docs))
    monkeypatch.setenv("RAG_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("RAG_DB_PATH", str(tmp_path / "rag.sqlite3"))
    return docs, get_conn(str(tmp_path / "rag.sqlite3"))


def test_scan_recurses_and_same_names_are_distinct(corpus):
    docs, conn = corpus
    for folder, text in [("one", "원인 분석"), ("two", "조치 검증")]:
        (docs / folder).mkdir()
        (docs / folder / "report.md").write_text(text, encoding="utf8")
    jobs = scan_dir(conn, str(docs))
    assert len(jobs) == 2
    assert len({j["document_id"] for j in jobs}) == 2


def test_scan_resumes_pending_and_failed_work(corpus, monkeypatch):
    docs, conn = corpus
    (docs / "report.md").write_text("고온시험 기록", encoding="utf8")
    job = scan_dir(conn, str(docs))[0]
    # Collection can complete before a worker starts; a later scan must drain it.
    out = scan_once(str(docs), conn.execute("PRAGMA database_list").fetchone()[2])
    assert out["done"] == 1
    assert conn.execute("SELECT status FROM ingestion_jobs WHERE job_id=?",
                        (job["job_id"],)).fetchone()[0] == "DONE"


def test_empty_document_never_becomes_active(corpus):
    docs, conn = corpus
    (docs / "empty.txt").write_text("", encoding="utf8")
    job = scan_dir(conn, str(docs))[0]
    assert run_job(conn, job["job_id"])["status"] == "FAILED"
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE status='ACTIVE'").fetchone()[0] == 0


def test_failed_replacement_preserves_previous_chunks(corpus, monkeypatch):
    import ingestion.indexer as indexer
    docs, conn = corpus
    p = docs / "report.md"
    p.write_text("원본 고온시험 자료", encoding="utf8")
    job = scan_dir(conn, str(docs))[0]
    assert run_job(conn, job["job_id"])["status"] == "DONE"
    before = [tuple(r) for r in conn.execute("SELECT chunk_id,text FROM chunks")]
    p.write_text("수정한 시험 자료", encoding="utf8")
    job2 = scan_dir(conn, str(docs))[0]
    def failure(*args, **kwargs):
        raise RuntimeError("embedding unavailable")
    monkeypatch.setattr(indexer, "_chunk_embeddings", failure)
    assert run_job(conn, job2["job_id"])["status"] == "FAILED"
    assert [tuple(r) for r in conn.execute("SELECT chunk_id,text FROM chunks")] == before
    assert conn.execute("SELECT version FROM documents").fetchone()[0] == "v1"


def test_reindex_preserves_acl(corpus):
    docs, conn = corpus
    p = docs / "report.md"
    p.write_text("비공개 시험 자료", encoding="utf8")
    job = scan_dir(conn, str(docs))[0]
    run_job(conn, job["job_id"])
    conn.execute("UPDATE documents SET allowed_groups=?", (json.dumps(["team"]),))
    conn.execute("UPDATE chunks SET allowed_groups=?", (json.dumps(["team"]),))
    conn.commit()
    p.write_text("개정한 비공개 시험 자료", encoding="utf8")
    run_job(conn, scan_dir(conn, str(docs))[0]["job_id"])
    assert json.loads(conn.execute("SELECT allowed_groups FROM documents").fetchone()[0]) == ["team"]
    assert json.loads(conn.execute("SELECT allowed_groups FROM chunks").fetchone()[0]) == ["team"]


def test_real_hwpx_paragraphs_and_table_are_extracted(tmp_path):
    from ingestion.parser import parse_file
    p = tmp_path / "real.hwpx"
    xml = '<hs:sec xmlns:hs="urn:section" xmlns:hp="urn:paragraph"><hp:p><hp:run><hp:t>일반 본문</hp:t></hp:run></hp:p><hp:p><hp:run><hp:tbl><hp:tr><hp:tc><hp:subList><hp:p><hp:run><hp:t>표 안의 결과</hp:t></hp:run></hp:p></hp:subList></hp:tc></hp:tr></hp:tbl></hp:run></hp:p></hs:sec>'
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Contents/section0.xml", xml)
    text = "\n".join(s["text"] for s in parse_file(str(p)))
    assert "일반 본문" in text and "표 안의 결과" in text
    assert text.count("표 안의 결과") == 1


def test_docx_and_pptx_tables_are_searchable(tmp_path):
    from docx import Document
    from pptx import Presentation
    from pptx.util import Inches
    from ingestion.parser import parse_file
    doc = Document()
    doc.add_paragraph("시험 본문")
    doc.add_table(1, 2).cell(0, 1).text = "문서표 결과"
    dp = tmp_path / "table.docx"
    doc.save(dp)
    ppt = Presentation()
    slide = ppt.slides.add_slide(ppt.slide_layouts[6])
    table = slide.shapes.add_table(1, 2, Inches(1), Inches(1), Inches(4), Inches(1)).table
    table.cell(0, 1).text = "슬라이드표 결과"
    pp = tmp_path / "table.pptx"
    ppt.save(pp)
    assert "문서표 결과" in "\n".join(s["text"] for s in parse_file(str(dp)))
    assert "슬라이드표 결과" in "\n".join(s["text"] for s in parse_file(str(pp)))


def test_chunking_splits_long_unbroken_text():
    from ingestion.chunker import hybrid_chunk
    chunks = hybrid_chunk([{"text": "가" * 12000}], target_tokens=100, max_tokens=150)
    assert len(chunks) > 1
    assert all(len(c["text"]) <= 600 for c in chunks)


def test_markdown_names_do_not_collide(corpus):
    docs, conn = corpus
    (docs / "report.md").write_text("첫 자료", encoding="utf8")
    (docs / "report.txt").write_text("둘째 자료", encoding="utf8")
    jobs = scan_dir(conn, str(docs))
    for job in jobs:
        assert run_job(conn, job["job_id"])["status"] == "DONE"
    import os
    from pathlib import Path
    paths = list(Path(os.environ["RAG_MD_DIR"]).glob("*.md"))
    assert len(paths) == 2
    assert len({p.read_text(encoding="utf8") for p in paths}) == 2
