from database.db import get_conn
from ingestion.trouble import extract_trouble, outcome_of, role_of


def test_role_of_headings():
    assert role_of("4.3 원인분석", "", "") == "cause"
    assert role_of("4.4 조치결과", "", "") == "action"
    assert role_of("4.2 시험결과", "", "") == "verification"
    assert role_of("4.1 시험조건", "", "") == "symptom"
    assert role_of("", "", "일반 본문 내용") == "other"


def test_role_of_inline_heading_line():
    assert role_of("", "", "## 조치 내용\n방열판을 교체했다") == "action"


def test_outcome_levels():
    assert outcome_of("조치를 완료했고 정상 동작을 확인했다") == "resolved"
    assert outcome_of("임시 조치 후 관찰 중이다") == "temporary"
    assert outcome_of("원인을 분석하고 있다") == "unknown"


def test_extract_trouble_groups_sections():
    secs = [
        {"heading_path": "증상", "text": "출력 감소"},
        {"heading_path": "원인", "text": "방열 문제"},
        {"heading_path": "조치", "text": "방열판 교체 완료, 정상 확인"},
    ]
    t = extract_trouble(secs)
    assert t["symptom"] == "출력 감소" and t["cause"] == "방열 문제"
    assert "방열판 교체" in t["action"] and t["outcome"] == "resolved"


def test_migrate_adds_trouble_columns(tmp_path):
    conn = get_conn(str(tmp_path / "t.sqlite3"))
    dcols = {r[1] for r in conn.execute("PRAGMA table_info(documents)").fetchall()}
    ccols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()}
    conn.close()
    assert {"trouble_json", "outcome"} <= dcols
    assert "role" in ccols


def test_pipeline_stores_roles_and_outcome(tmp_path):
    from ingestion.collector import scan_dir
    from ingestion.pipeline import run_job
    d = tmp_path / "docs"
    d.mkdir()
    with open(d / "case.md", "w", encoding="utf-8") as f:
        f.write("## 증상\n출력 감소\n## 원인분석\n방열 문제\n## 조치결과\n방열판 교체 완료, 정상 확인\n")
    conn = get_conn(str(tmp_path / "t.sqlite3"))
    jid = scan_dir(conn, str(d))[0]["job_id"]
    run_job(conn, jid)
    roles = {r[0] for r in conn.execute("SELECT DISTINCT role FROM chunks").fetchall()}
    out = conn.execute("SELECT outcome FROM documents").fetchone()[0]
    conn.close()
    assert roles & {"symptom", "cause", "action"}
    assert out == "resolved"
