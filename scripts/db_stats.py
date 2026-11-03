"""벡터 DB 현황: python scripts/db_stats.py"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = os.getenv("RAG_DB_PATH", "data/rag.sqlite3")
c = sqlite3.connect(DB)
n = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
v = c.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL").fetchone()[0]
s = c.execute("SELECT SUM(LENGTH(embedding)) FROM chunks").fetchone()[0] or 0
d = c.execute("SELECT COUNT(*) FROM documents WHERE status='ACTIVE'").fetchone()[0]
print(f"DB={DB} size_MB={round(os.path.getsize(DB)/1048576,1)}")
print(f"docs_active={d} chunks={n} with_vector={v} vec_MB={round(s/1048576,1)}")
