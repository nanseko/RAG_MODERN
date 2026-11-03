"""Correct legacy vectors and synchronize the persistent vector store."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database.db import get_conn
from database.vector_store import EMBEDDING_VERSION, put
from models.embedding import embed


def backfill():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT c.chunk_id,c.document_id,c.text FROM chunks c "
            "JOIN documents d ON d.document_id=c.document_id WHERE d.status='ACTIVE' "
            "AND (c.embedding IS NULL OR c.embedding_version!=?)", (EMBEDDING_VERSION,)).fetchall()
        print(f"backfill targets: {len(rows)}")
        for start in range(0, len(rows), 4):
            batch = rows[start:start + 4]
            vectors = embed([r["text"] for r in batch])
            with conn:
                for row, vector in zip(batch, vectors):
                    conn.execute("UPDATE chunks SET embedding=?,embedding_version=? WHERE chunk_id=?",
                                 (json.dumps(vector), EMBEDDING_VERSION, row["chunk_id"]))
                    put(conn, row["chunk_id"], row["document_id"], vector)
            print(f"done {min(start + 4, len(rows))}/{len(rows)}")
    finally:
        conn.close()


if __name__ == "__main__":
    backfill()
