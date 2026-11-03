from database.db import get_conn
from ingestion.collector import scan_dir
from ingestion.pipeline import retry_job, run_job
from rag.graph import run_rag


def _setup(tmp_path, text: str):
    d = tmp_path / "docs"
    d.mkdir()
    with open(d / "a.md", "w", encoding="utf-8") as f:
        f.write(text)
    conn = get_conn(str(tmp_path / "t.sqlite3"))
    jobs = scan_dir(conn, str(d))
    return conn, str(d), jobs[0]["job_id"]


def test_run_job_reaches_active_with_chunks(tmp_path):
    conn, _d, jid = _setup(tmp_path, "고온시험 출력 감소 기록")
    out = run_job(conn, jid)
    assert out["status"] == "DONE"
    n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert n > 0
    st = conn.execute("SELECT status FROM documents").fetchone()[0]
    conn.close()
    assert st == "ACTIVE"


def test_changed_file_bumps_version_and_replaces_chunks(tmp_path):
    conn, d, jid = _setup(tmp_path, "원본 내용 기록")
    run_job(conn, jid)
    with open(f"{d}/a.md", "w", encoding="utf-8") as f:
        f.write("수정된 내용 기록")
    jid2 = scan_dir(conn, d)[0]["job_id"]
    run_job(conn, jid2)
    ver = conn.execute("SELECT version FROM documents").fetchone()[0]
    texts = [r[0] for r in conn.execute("SELECT text FROM chunks").fetchall()]
    conn.close()
    assert ver == "v2"
    assert all("원본" not in t for t in texts)


def test_failed_job_records_error_and_retry_increments(tmp_path):
    conn, d, jid = _setup(tmp_path, "정상 본문")
    conn.execute("UPDATE ingestion_jobs SET file_path=? WHERE job_id=?",
                 (f"{d}/missing.md", jid))
    conn.commit()
    out = run_job(conn, jid)
    assert out["status"] == "FAILED" and out["error"]
    out2 = retry_job(conn, jid)
    row = conn.execute("SELECT attempts FROM ingestion_jobs WHERE job_id=?",
                       (jid,)).fetchone()
    conn.close()
    assert out2["status"] == "FAILED" and row[0] == 2


def test_deleted_doc_excluded_from_search(tmp_path):
    conn, _d, jid = _setup(tmp_path, "고온시험 출력 감소 기록")
    run_job(conn, jid)
    conn.execute("UPDATE documents SET status='DELETED'")
    conn.commit()
    out = run_rag("고온시험 출력 감소", ["all"], conn)
    conn.close()
    assert out["answer"] == "관련 근거를 찾을 수 없음."


def test_pipeline_saves_md_copy(tmp_path):
    import os
    conn, _d, jid = _setup(tmp_path, "고온시험 출력 감소 기록")
    run_job(conn, jid, md_dir=str(tmp_path / "md"))
    files = os.listdir(str(tmp_path / "md"))
    conn.close()
    assert len(files) == 1 and files[0].endswith(".md")
    with open(os.path.join(str(tmp_path / "md"), files[0]), encoding="utf-8") as f:
        assert "고온시험 출력 감소 기록" in f.read()


def test_scan_once_processes_pending_file(tmp_path):
    from ingestion.pipeline import scan_once
    d = tmp_path / "docs"
    d.mkdir()
    with open(d / "b.md", "w", encoding="utf-8") as f:
        f.write("주기 스캔 본문 기록")
    out = scan_once(str(d), str(tmp_path / "s.sqlite3"))
    assert out["jobs"] == 1 and out["done"] == 1


def test_startup_autoscan_processes_file(tmp_path, monkeypatch):
    import sqlite3

    d = tmp_path / "docs"
    d.mkdir()
    with open(d / "c.md", "w", encoding="utf-8") as f:
        f.write("시작 스캔 본문 기록")
    monkeypatch.setenv("RAG_AUTOSCAN", "1")
    monkeypatch.setenv("RAG_DOCS_DIR", str(d))
    monkeypatch.setenv("RAG_DB_PATH", str(tmp_path / "a.sqlite3"))
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
    conn = sqlite3.connect(str(tmp_path / "a.sqlite3"))
    st = conn.execute("SELECT status FROM ingestion_jobs").fetchone()[0]
    conn.close()
    assert st == "DONE"
