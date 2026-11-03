"""문서 상관관계(Obsidian식): 명시 인용·첨부·버전 엣지 자동 추출."""
import os
import re


def extract_edges(from_doc: str, text: str, known_files: dict) -> list:
    """known_files={file_name: document_id}. → [(from,to,kind)]."""
    edges = []
    t = text or ""
    for name, did in known_files.items():
        if did != from_doc and name in t:
            kind = "attachment" if re.search(r"첨부", t[:t.index(name) + 20] if name in t else "") else "references"
            edges.append((from_doc, did, kind))
    return edges


def store_edges(conn, edges: list) -> int:
    n = 0
    for frm, to, kind in edges:
        conn.execute("INSERT OR IGNORE INTO document_edges(from_doc,to_doc,kind) VALUES(?,?,?)",
                     (frm, to, kind))
        n += 1
    conn.commit()
    return n


def backlinks(conn, document_id: str) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT e.from_doc AS document_id, d.title, e.kind FROM document_edges e"
        " LEFT JOIN documents d ON d.document_id=e.from_doc"
        " WHERE e.to_doc=?", (document_id,)).fetchall()]


def related(conn, document_id: str) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT e.to_doc AS document_id, d.title, e.kind FROM document_edges e"
        " LEFT JOIN documents d ON d.document_id=e.to_doc"
        " WHERE e.from_doc=?", (document_id,)).fetchall()]
