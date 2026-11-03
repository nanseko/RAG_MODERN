"""Single-process, broker-free ingestion worker for native Windows services."""
import argparse
import json
import logging
import os
import signal
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from database.db import get_conn
from database.jobs import reclaim_expired
from ingestion.pipeline import MAX_ATTEMPTS, run_job
from retrieval.opensearch_store import reconcile_selected
from workers import native_queue as queue

logger = logging.getLogger(__name__)


class NativeWorker:
    def __init__(self, poll_interval=2, batch_size=1, retry_delay=5, stop_file=None):
        if poll_interval <= 0 or batch_size < 1 or retry_delay < 0:
            raise ValueError("invalid native worker polling/batch/retry limits")
        self.poll_interval, self.batch_size, self.retry_delay = poll_interval, batch_size, retry_delay
        self.owner = uuid.uuid4().hex
        self.stop = threading.Event()
        self._heartbeat_stop = threading.Event()
        self.stop_file = Path(stop_file).resolve() if stop_file else None
        self._heartbeat_thread = None
        self._active_job = None
        self._active_command = None
        self._next_scan = None
        self._next_reconcile = 0

    def acquire(self):
        conn = get_conn()
        try:
            queue.initialize(conn)
            return queue.acquire_worker(conn, self.owner)
        finally:
            conn.close()

    def _beat(self):
        while not self._heartbeat_stop.wait(5):
            conn = None
            try:
                conn = get_conn()
                self._check_stop_file()
                if not queue.heartbeat(conn, self.owner, stopping=self.stop.is_set()):
                    self.stop.set()
                    logger.error("Native worker lease was lost")
                    return
                self._renew_leases(conn)
            except Exception as error:
                logger.error("Native worker heartbeat failed: %s", type(error).__name__)
                # Fail closed. The expired lease allows a supervised restart.
                self.stop.set()
                return
            finally:
                if conn is not None:
                    conn.close()

    def _renew_leases(self, conn):
        # Exact tokens prevent a paused/replaced worker from renewing a newer
        # owner's job or command during long model calls.
        active = self._active_job
        if active:
            job_id, token = active
            expires = datetime.now(timezone.utc) + timedelta(seconds=float(os.getenv("RAG_JOB_LEASE_SECONDS", "3600")))
            conn.execute("UPDATE ingestion_jobs SET lease_expires_at=? WHERE job_id=? AND claim_token=? AND status='RUNNING'",
                         (expires.isoformat(timespec="microseconds"), job_id, token))
        command = self._active_command
        if command:
            conn.execute("UPDATE native_commands SET lease_expires_at=? WHERE task_id=? AND claim_token=? AND status='RUNNING'",
                         (time.time() + queue.COMMAND_LEASE_SECONDS, command["task_id"], command["claim_token"]))
        conn.commit()

    def _check_stop_file(self):
        if self.stop_file and self.stop_file.is_file():
            self.stop.set()
        return self.stop.is_set()

    def _schedule_scan(self, conn):
        from app.settings import get_settings
        settings = get_settings()
        now = time.monotonic()
        first = self._next_scan is None
        if first:
            self._next_scan = now + settings.scan_interval_min * 60
        if (first and settings.autoscan) or (settings.scan_interval_min > 0 and now >= self._next_scan):
            queue.enqueue_command(conn, "scan", {"docs_dir": settings.docs, "md_dir": None})
            self._next_scan = now + settings.scan_interval_min * 60

    def tick(self):
        conn = get_conn()
        try:
            stopping = self._check_stop_file()
            if not queue.heartbeat(conn, self.owner, stopping=stopping):
                raise RuntimeError("Native worker does not own the live worker lease")
            if stopping:
                return {"processed": 0, "recovered": 0, "status": "STOPPING"}
            recovered = reclaim_expired(conn)
            self._schedule_scan(conn)
            command = queue.claim_command(conn)
            if command:
                self._active_command = command
                try:
                    payload = json.loads(command["payload"])
                    if command["kind"] == "scan":
                        from ingestion.collector import scan_dir
                        root = Path(payload["docs_dir"]).resolve()
                        root.mkdir(parents=True, exist_ok=True)
                        result = {"collected": len(scan_dir(conn, root))}
                    else:
                        result = reconcile_selected(conn, limit=int(os.getenv("RAG_QUEUE_RECONCILE_LIMIT", "100")))
                    queue.finish_command(conn, command, result=result)
                except Exception as error:
                    conn.rollback()
                    queue.finish_command(conn, command, error=error)
                    logger.error("Native command failed: %s", type(error).__name__)
                finally:
                    self._active_command = None
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=self.retry_delay)).isoformat(timespec="microseconds")
            pending = conn.execute(
                "SELECT job_id FROM ingestion_jobs j WHERE (status='PENDING' OR "
                "(status='FAILED' AND stage!='COLLECT' AND updated_at<=?)) AND attempts<? "
                "AND NOT EXISTS(SELECT 1 FROM ingestion_jobs active WHERE active.document_id=j.document_id "
                "AND active.status='RUNNING') ORDER BY rowid LIMIT ?",
                (cutoff, MAX_ATTEMPTS, self.batch_size)).fetchall()
            processed = 0
            for row in pending:
                if self._check_stop_file():
                    queue.heartbeat(conn, self.owner, stopping=True)
                    break
                try:
                    def on_claim(token):
                        self._active_job = (row["job_id"], token)
                    result = run_job(conn, row["job_id"], on_claim=on_claim)
                    logger.info("Ingestion job %s: %s", row["job_id"], result["status"])
                    processed += 1
                finally:
                    self._active_job = None
            if time.monotonic() >= self._next_reconcile:
                try:
                    reconcile_selected(conn, limit=int(os.getenv("RAG_QUEUE_RECONCILE_LIMIT", "100")))
                except Exception as error:
                    conn.rollback()
                    logger.error("Native search reconciliation failed: %s", type(error).__name__)
                self._next_reconcile = time.monotonic() + 30
            return {"processed": processed, "recovered": recovered}
        finally:
            conn.close()

    def run(self, once=False):
        if not self.acquire():
            raise RuntimeError("Another native ingestion worker already owns the SQL lease")
        self._heartbeat_thread = threading.Thread(target=self._beat, name="rag-worker-heartbeat", daemon=True)
        self._heartbeat_thread.start()
        try:
            while not self.stop.is_set():
                try:
                    result = self.tick()
                    if once:
                        return result
                except Exception as error:
                    if once:
                        raise
                    logger.error("Native worker poll failed: %s", type(error).__name__)
                self.stop.wait(self.poll_interval)
        finally:
            self.close()

    def close(self):
        self.stop.set()
        self._heartbeat_stop.set()
        if self._heartbeat_thread and self._heartbeat_thread is not threading.current_thread():
            self._heartbeat_thread.join(timeout=10)
        conn = get_conn()
        try:
            queue.release_worker(conn, self.owner)
        finally:
            conn.close()


def main(argv=None):
    from app.settings import get_settings
    get_settings()  # Apply shared dotenv policy before reading worker options.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Process one bounded poll then exit")
    parser.add_argument("--poll-interval", type=float, default=float(os.getenv("RAG_NATIVE_POLL_SECONDS", "2")))
    parser.add_argument("--batch-size", type=int, default=int(os.getenv("RAG_NATIVE_BATCH_SIZE", "1")))
    parser.add_argument("--stop-file", help="Gracefully finish the current job and exit when this file exists")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    worker = NativeWorker(poll_interval=args.poll_interval, batch_size=args.batch_size, stop_file=args.stop_file)
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda *_: worker.stop.set())
    worker.run(once=args.once)


if __name__ == "__main__":
    main()
