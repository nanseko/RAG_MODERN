"""Lazy dispatch boundary: native SQL on Windows, optional legacy Celery."""
from app.operations import selected_backends


def _native():
    backend = selected_backends()["RAG_TASK_BACKEND"]
    if backend == "inline":
        raise RuntimeError("Asynchronous dispatch requires native or celery backend")
    return backend == "native"


def _command(kind, payload=None):
    from database.db import get_conn
    from workers.native_queue import enqueue_command, initialize
    conn = get_conn()
    try:
        initialize(conn)
        return enqueue_command(conn, kind, payload)
    finally:
        conn.close()


def dispatch_job(job_id, md_dir=None):
    if not isinstance(job_id, str) or not job_id:
        raise ValueError("job_id must be a nonempty string")
    if _native():
        # The committed ingestion_jobs row is the queue; polling rescues jobs
        # even if the API stopped before this acknowledgement was returned.
        return job_id
    from workers.tasks import dispatch_job as legacy
    return legacy(job_id, md_dir=md_dir)


def dispatch_scan(docs_dir=None, md_dir=None):
    if _native():
        from app.settings import get_settings
        return _command("scan", {"docs_dir": str(docs_dir or get_settings().docs), "md_dir": md_dir})
    from workers.tasks import dispatch_scan as legacy
    return legacy() if docs_dir is None and md_dir is None else legacy(docs_dir=docs_dir, md_dir=md_dir)


def dispatch_reconcile():
    if _native():
        return _command("reconcile")
    from workers.tasks import dispatch_reconcile as legacy
    return legacy()
