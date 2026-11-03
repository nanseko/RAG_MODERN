from database.db import get_conn
from ingestion.collector import scan_dir


def _write(path: str, text: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_scan_creates_pending_job_for_new_file(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    _write(str(d / "a.md"), "본문 내용")
    conn = get_conn(str(tmp_path / "t.sqlite3"))
    jobs = scan_dir(conn, str(d))
    conn.close()
    assert len(jobs) == 1
    assert jobs[0]["status"] == "PENDING"


def test_scan_skips_unchanged_file(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    _write(str(d / "a.md"), "본문 내용")
    conn = get_conn(str(tmp_path / "t.sqlite3"))
    assert len(scan_dir(conn, str(d))) == 1
    assert scan_dir(conn, str(d)) == []
    conn.close()


def test_scan_detects_changed_file(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    _write(str(d / "a.md"), "원본")
    conn = get_conn(str(tmp_path / "t.sqlite3"))
    scan_dir(conn, str(d))
    _write(str(d / "a.md"), "수정본")
    jobs = scan_dir(conn, str(d))
    conn.close()
    assert len(jobs) == 1 and jobs[0]["changed"] is True
