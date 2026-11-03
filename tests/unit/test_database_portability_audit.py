"""Postgres-compatible chunks lack SQLite's implicit rowid; SQL failure closes clients."""
import sqlite3

import pytest
from database.db import SCHEMA, ADDITIONS
from rag.graph import _load_rows, run_rag

def test_chunk_loading_works_without_sqlite_hidden_rowid():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # A WITHOUT ROWID table exposes only declared columns, as PostgreSQL does.
    columns = conn.execute("PRAGMA table_info(chunks)").fetchall()
    conn.execute("ALTER TABLE chunks RENAME TO old_chunks")
    spec = [f'"{r[1]}" {r[2]}' + (" PRIMARY KEY" if r[5] else "") for r in columns]
    conn.execute("CREATE TABLE chunks (" + ",".join(spec) + ") WITHOUT ROWID")
    for table, columns in ADDITIONS.items():
        names = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in names:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    try:
        assert _load_rows(conn) == []
    finally:
        conn.close()

def test_sql_load_failure_closes_owned_llm_client(monkeypatch):
    import rag.graph as graph
    class Client:
        mode = "live"
        closed = False
        def close(self):
            self.closed = True
    client = Client()
    monkeypatch.setattr(graph, "get_client", lambda: client)
    class FailedDB:
        def execute(self, *args):
            raise sqlite3.OperationalError("database unavailable")
    with pytest.raises(sqlite3.OperationalError):
        run_rag("출력 저하", ["all"], FailedDB())
    assert client.closed
