import json

from ingestion.metadata import extract_identifiers
from retrieval.boost import BoostWeights, boost_scores
from retrieval.rrf import rrf_fuse


def test_equipment_model_extracted():
    ids = extract_identifiers("AN/TPS-43 레이더의 KT-1 지휘계 고장")
    assert "AN/TPS-43" in ids["equipment"]
    assert "KT-1" in ids["equipment"]


def test_equipment_empty_without_model():
    assert extract_identifiers("일반 점검 기록")["equipment"] == []


def test_rrf_respects_weights():
    a = ["x", "y", "z"]
    b = ["z", "y", "x"]
    assert rrf_fuse([a, b], weights=[3.0, 1.0])[0] == "x"
    assert rrf_fuse([a, b], weights=[1.0, 3.0])[0] == "z"


def test_rrf_weights_default_equal():
    a = ["x", "y"]
    b = ["y", "x"]
    fused = rrf_fuse([a, b])
    assert set(fused[:2]) == {"x", "y"}


def test_exact_code_match_outranks_semantic_twin():
    q = "ERR_PUMP_123 발생 대처"
    docs = [
        {"chunk_id": "hit", "text": "펌프 관련 일반 점검 안내", "error_codes": "[]",
         "equipment": "[]", "outcome": "unknown"},
        {"chunk_id": "miss", "text": "펌프 관련 일반 점검 안내", "error_codes": json.dumps(["ERR_PUMP_123"]),
         "equipment": "[]", "outcome": "unknown"},
    ]
    out = boost_scores(q, docs, {"base": 1.0, "hit": 0.0})
    assert out[0]["chunk_id"] == "miss"


def test_same_equipment_boost_applies():
    q = "레이더 점검"
    docs = [
        {"chunk_id": "same", "text": "점검 절차", "error_codes": "[]",
         "equipment": json.dumps(["AN/TPS-43"]), "outcome": "unknown"},
        {"chunk_id": "other", "text": "점검 절차", "error_codes": "[]",
         "equipment": "[]", "outcome": "unknown"},
    ]
    out = boost_scores(q, docs, {"base": 1.0})
    assert out[0]["chunk_id"] == "same"


def test_resolved_beats_temporary():
    docs = [
        {"chunk_id": "tmp", "text": "펌프 오류", "error_codes": "[]", "equipment": "[]",
         "outcome": "temporary"},
        {"chunk_id": "fix", "text": "펌프 오류", "error_codes": "[]", "equipment": "[]",
         "outcome": "resolved"},
    ]
    out = boost_scores("펌프 오류", docs, {"base": 1.0})
    assert out[0]["chunk_id"] == "fix"


def test_boost_is_bounded_and_never_invents_evidence():
    docs = [{"chunk_id": "a", "text": "x", "error_codes": json.dumps(["ERR_A_1"]),
             "equipment": json.dumps(["KT-1"]), "outcome": "resolved"}]
    w = BoostWeights().as_dict()
    out = boost_scores("ERR_A_1 KT-1", docs, w)
    assert out[0]["boost"] <= w["max_total"]
    assert out[0]["chunk_id"] == "a"


def test_malformed_identifier_columns_do_not_crash():
    docs = [{"chunk_id": "a", "text": "x", "error_codes": "{bad", "equipment": None}]
    out = boost_scores("ERR_A_1", docs, {"base": 1.0})
    assert out[0]["chunk_id"] == "a"


def test_rag_prefers_chunk_carrying_the_exact_error_code(tmp_path):
    from database.db import get_conn
    from ingestion.indexer import index_document
    from rag.graph import run_rag
    conn = get_conn(str(tmp_path / "b.sqlite3"))
    index_document(conn, {"document_id": "plain", "title": "일반"}, [
        {"text": "펌프 오류 시 점검 순서 안내", "title": "일반"}])
    index_document(conn, {"document_id": "exact", "title": "정확"}, [
        {"text": "펌프 오류 시 점검 순서 안내", "title": "정확"}])
    conn.execute("UPDATE chunks SET error_codes='[\"ERR_PUMP_123\"]' WHERE document_id='exact'")
    conn.commit()
    out = run_rag("ERR_PUMP_123 펌프 오류 대처", ["all"], conn, client=AcceptAll(),
                  target_min=1, target_max=3, max_cycles=1)
    ids = [s["document_id"] for s in out["sources"]]
    conn.close()
    assert ids and ids[0] == "exact"


def test_rag_prefers_resolved_case(tmp_path):
    from database.db import get_conn
    from ingestion.indexer import index_document
    from rag.graph import run_rag
    conn = get_conn(str(tmp_path / "r.sqlite3"))
    index_document(conn, {"document_id": "temp", "title": "임시"}, [
        {"text": "냉각팬 오류Temporary 조치", "title": "임시"}])
    index_document(conn, {"document_id": "done", "title": "완료"}, [
        {"text": "냉각팬 오류Temporary 조치", "title": "완료"}])
    conn.execute("UPDATE documents SET outcome='resolved' WHERE document_id='done'")
    conn.commit()
    out = run_rag("냉각팬 오류Temporary 조치", ["all"], conn, client=AcceptAll(),
                  target_min=1, target_max=3, max_cycles=1)
    ids = [s["document_id"] for s in out["sources"]]
    conn.close()
    assert ids and ids[0] == "done"


class AcceptAll:
    def judge(self, question, text):
        return True

    def generate(self, question, sources):
        return " ".join(f"[{s['sid']}]" for s in sources)