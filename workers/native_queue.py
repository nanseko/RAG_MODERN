"""Durable SQL commands and a single native worker lease; no broker required."""
import json
import time
import uuid

WORKER_LEASE_SECONDS = 30
COMMAND_LEASE_SECONDS = 60


def initialize(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS native_commands(
      task_id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
      created_at DOUBLE PRECISION NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
      claim_token TEXT, lease_expires_at DOUBLE PRECISION, result TEXT, error TEXT);
    CREATE INDEX IF NOT EXISTS native_commands_pending ON native_commands(status,created_at);
    CREATE TABLE IF NOT EXISTS native_worker_state(
      slot TEXT PRIMARY KEY, owner TEXT, heartbeat_at DOUBLE PRECISION NOT NULL,
      lease_expires_at DOUBLE PRECISION NOT NULL, stopping INTEGER NOT NULL DEFAULT 0);
    """)
    conn.commit()


def enqueue_command(conn, kind, payload=None):
    if kind not in ("scan", "reconcile"):
        raise ValueError("unsupported native command")
    encoded = json.dumps(payload or {}, sort_keys=True)
    now = time.time()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            "SELECT task_id FROM native_commands WHERE kind=? AND payload=? AND status='PENDING' ORDER BY created_at LIMIT 1",
            (kind, encoded)).fetchone()
        if previous:
            return previous[0]
        task_id = uuid.uuid4().hex
        conn.execute("INSERT INTO native_commands(task_id,kind,payload,created_at,updated_at) VALUES(?,?,?,?,?)",
                     (task_id, kind, encoded, now, now))
    return task_id


def claim_command(conn, now=None):
    now = time.time() if now is None else now
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE native_commands SET status='FAILED',claim_token=NULL,lease_expires_at=NULL,updated_at=? "
                     "WHERE status='RUNNING' AND lease_expires_at<?", (now, now))
        row = conn.execute("SELECT * FROM native_commands WHERE status IN ('PENDING','FAILED') AND attempts<3 "
                           "ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return None
        token = uuid.uuid4().hex
        conn.execute("UPDATE native_commands SET status='RUNNING',attempts=attempts+1,claim_token=?,"
                     "lease_expires_at=?,updated_at=?,error=NULL WHERE task_id=?",
                     (token, now + COMMAND_LEASE_SECONDS, now, row["task_id"]))
        return dict(conn.execute("SELECT * FROM native_commands WHERE task_id=?", (row["task_id"],)).fetchone())


def finish_command(conn, command, result=None, error=None):
    with conn:
        count = conn.execute("UPDATE native_commands SET status=?,result=?,error=?,updated_at=?,"
                             "claim_token=NULL,lease_expires_at=NULL WHERE task_id=? AND status='RUNNING' AND claim_token=?",
                             ("FAILED" if error else "DONE", json.dumps(result) if result is not None else None,
                              type(error).__name__ if error else None, time.time(), command["task_id"], command["claim_token"])).rowcount
    return bool(count)


def acquire_worker(conn, owner, now=None):
    now = time.time() if now is None else now
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO native_worker_state(slot,owner,heartbeat_at,lease_expires_at) VALUES('native',NULL,0,0) "
                     "ON CONFLICT(slot) DO NOTHING")
        count = conn.execute("UPDATE native_worker_state SET owner=?,heartbeat_at=?,lease_expires_at=?,stopping=0 "
                             "WHERE slot='native' AND (owner=? OR lease_expires_at<=?)",
                             (owner, now, now + WORKER_LEASE_SECONDS, owner, now)).rowcount
    return bool(count)


def heartbeat(conn, owner, now=None, stopping=False):
    now = time.time() if now is None else now
    with conn:
        count = conn.execute("UPDATE native_worker_state SET heartbeat_at=?,lease_expires_at=?,stopping=? "
                             "WHERE slot='native' AND owner=? AND lease_expires_at>?",
                             (now, now + WORKER_LEASE_SECONDS, int(stopping), owner, now)).rowcount
    return bool(count)


def release_worker(conn, owner):
    with conn:
        conn.execute("UPDATE native_worker_state SET owner=NULL,lease_expires_at=0 WHERE slot='native' AND owner=?", (owner,))


def worker_health(conn, now=None):
    """Read only: an absent/expired worker must fail readiness."""
    now = time.time() if now is None else now
    row = conn.execute("SELECT owner,heartbeat_at,lease_expires_at,stopping FROM native_worker_state WHERE slot='native'").fetchone()
    if not row or not row["owner"] or row["lease_expires_at"] <= now:
        raise RuntimeError("No live native ingestion worker heartbeat")
    if row["stopping"]:
        raise RuntimeError("Native ingestion worker is stopping")
    return {"status": "ready", "backend": "native", "workers": 1,
            "heartbeat_age_seconds": max(0, round(now - row["heartbeat_at"], 2))}
