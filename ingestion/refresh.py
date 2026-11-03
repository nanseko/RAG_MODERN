"""메타데이터 최신화 배치: 버전 스탬프로 구버전만 골라 재추출.

원문 재파싱 없이 chunks 텍스트로 식별자·분류를 갱신하므로 4만건도 저비용.
사람 손 필요 없음 (수동 라벨은 label_samples.json만 관리).
"""
import json

from ingestion.classify import METADATA_VERSION, classify
from ingestion.metadata import extract_identifiers
from ingestion.trouble import assign_roles, document_summary
from retrieval.opensearch_store import enqueue_document


def refresh_stale(conn, limit: int = 1000) -> dict:
    rows = conn.execute(
        "SELECT document_id,file_name,title FROM documents"
        " WHERE COALESCE(metadata_version,'v0')!=? AND status='ACTIVE' LIMIT ?",
        (METADATA_VERSION, limit)).fetchall()
    done = 0
    for r in rows:
        did = r[0]
        chunks = [dict(c) for c in conn.execute(
            "SELECT * FROM chunks WHERE document_id=? ORDER BY chunk_index,chunk_id", (did,)).fetchall()]
        assign_roles(chunks)
        trouble_json, outcome = document_summary(chunks)
        full = "\n".join(c.get("text") or "" for c in chunks)
        ids = extract_identifiers(full)
        cat = classify(r[2] or "", r[1] or "", full, ids)
        for c in chunks:
            cids = extract_identifiers(c.get("text") or "")
            conn.execute("UPDATE chunks SET part_numbers=?,error_codes=?,equipment=?,role=? WHERE chunk_id=?",
                         (json.dumps(cids["part_numbers"]), json.dumps(cids["error_codes"]),
                          json.dumps(cids["equipment"]), c["role"], c["chunk_id"]))
        conn.execute("UPDATE documents SET category=?,metadata_version=?,trouble_json=?,outcome=? WHERE document_id=?",
                     (cat, METADATA_VERSION, trouble_json, outcome, did))
        enqueue_document(conn, did)
        done += 1
    conn.commit()
    return {"checked": len(rows), "refreshed": done, "version": METADATA_VERSION}
