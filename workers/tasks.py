"""JSON task entry points. SQL job records remain the durable source of truth.

Only IDs and configured paths cross Redis. Every task opens and closes its own
DB connection; no SQLite/psycopg connection is serialized or shared by workers.
"""
import os
from pathlib import Path

from workers.celery_app import celery_app


def _delay(task, *args, **kwargs):
    return task.apply_async(args=list(args), kwargs=kwargs).id


def dispatch_scan(docs_dir=None, md_dir=None):
    return _delay(scan_task, docs_dir=docs_dir, md_dir=md_dir)


def dispatch_job(job_id, md_dir=None):
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("job_id must be a nonempty string")
    return _delay(run_job_task, job_id, md_dir=md_dir)


def dispatch_reconcile():
    return _delay(reconcile_task)


def task_status(task_id):
    result = celery_app.AsyncResult(task_id)
    payload = {"task_id": task_id, "status": result.state}
    if result.successful():
        payload["result"] = result.result
    elif result.failed():
        # Exceptions can contain connection credentials; the SQL error is the
        # user-facing job diagnosis, while the queue exposes only its type.
        payload["error"] = type(result.result).__name__
    return payload


def _retry_delay(task):
    return min(300, 5 * 2 ** task.request.retries)


@celery_app.task(bind=True, name="rag.run_job", max_retries=3)
def run_job_task(self, job_id, md_dir=None):
    from database.db import get_conn
    from ingestion.pipeline import MAX_ATTEMPTS, run_job
    conn = None
    try:
        conn = get_conn()
        result = run_job(conn, job_id, md_dir=md_dir)
        row = conn.execute("SELECT attempts,status FROM ingestion_jobs WHERE job_id=?", (job_id,)).fetchone()
    except Exception as exc:
        raise self.retry(exc=exc, countdown=_retry_delay(self))
    finally:
        if conn is not None:
            conn.close()
    if row and ((result["status"] == "FAILED" and row["attempts"] < MAX_ATTEMPTS)
                or result["status"] == "PENDING"):
        raise self.retry(countdown=_retry_delay(self))
    return result


@celery_app.task(bind=True, name="rag.scan", max_retries=3)
def scan_task(self, docs_dir=None, md_dir=None):
    from app.settings import get_settings
    from database.db import get_conn
    from database.jobs import reclaim_expired
    from ingestion.collector import scan_dir
    from ingestion.pipeline import MAX_ATTEMPTS
    conn = None
    try:
        root = Path(docs_dir or get_settings().docs).resolve()
        root.mkdir(parents=True, exist_ok=True)
        conn = get_conn()
        recovered = reclaim_expired(conn)
        collected = scan_dir(conn, root)
        pending = conn.execute(
            "SELECT job_id,file_path FROM ingestion_jobs WHERE "
            "(status='PENDING' OR (status='FAILED' AND stage!='COLLECT')) AND attempts<? ORDER BY rowid",
            (MAX_ATTEMPTS,)).fetchall()
        ids = [row["job_id"] for row in pending if Path(row["file_path"]).resolve().is_relative_to(root)]
    except Exception as exc:
        raise self.retry(exc=exc, countdown=_retry_delay(self))
    finally:
        if conn is not None:
            conn.close()
    task_ids = [dispatch_job(jid, md_dir=md_dir) for jid in ids]
    dispatch_reconcile()
    return {"status": "QUEUED", "jobs": len(collected), "recovered": recovered,
            "queued": len(task_ids), "task_ids": task_ids, "job_ids": ids}


@celery_app.task(bind=True, name="rag.reconcile", max_retries=3)
def reconcile_task(self):
    from database.db import get_conn
    from database.jobs import reclaim_expired
    from ingestion.pipeline import MAX_ATTEMPTS
    from retrieval.opensearch_store import reconcile_selected
    conn = None
    try:
        conn = get_conn()
        recovered = reclaim_expired(conn)
        # Beat also rescues SQL jobs whose broker delivery was lost, independent
        # of filesystem autoscan. Duplicate delivery is safe because of tokens.
        ids = [row[0] for row in conn.execute(
            "SELECT job_id FROM ingestion_jobs WHERE (status='PENDING' OR "
            "(status='FAILED' AND stage!='COLLECT')) AND attempts<? ORDER BY rowid", (MAX_ATTEMPTS,)).fetchall()]
        sync = reconcile_selected(conn, limit=int(os.getenv("RAG_QUEUE_RECONCILE_LIMIT", "100")))
    except Exception as exc:
        raise self.retry(exc=exc, countdown=_retry_delay(self))
    finally:
        if conn is not None:
            conn.close()
    for jid in ids:
        dispatch_job(jid)
    return {**sync, "recovered": recovered, "queued": len(ids)}