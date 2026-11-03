"""Transactional ingestion leases shared by local and distributed workers."""
import os
import uuid
from datetime import datetime, timedelta, timezone


def _now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.isoformat(timespec="microseconds")


def _expiry():
    seconds = float(os.getenv("RAG_JOB_LEASE_SECONDS", "3600"))
    if seconds <= 0:
        raise ValueError("RAG_JOB_LEASE_SECONDS must be positive")
    return _iso(_now() + timedelta(seconds=seconds))


def claim_job(conn, job_id, max_attempts=3):
    """Serialize the predicate across connections, then claim with a unique token."""
    token = uuid.uuid4().hex
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        claimed = conn.execute(
            "UPDATE ingestion_jobs SET status='RUNNING',stage='PARSING',attempts=attempts+1,"
            "error=NULL,updated_at=?,claim_token=?,lease_expires_at=? "
            "WHERE job_id=? AND status IN ('PENDING','FAILED') AND attempts<? "
            "AND NOT EXISTS(SELECT 1 FROM ingestion_jobs other "
            "WHERE other.document_id=ingestion_jobs.document_id AND other.status='RUNNING' "
            "AND other.job_id!=ingestion_jobs.job_id)",
            (_iso(_now()), token, _expiry(), job_id, max_attempts)).rowcount
    return token if claimed else None


def owns_claim(conn, job_id, token):
    return conn.execute("SELECT 1 FROM ingestion_jobs WHERE job_id=? AND claim_token=? AND status='RUNNING'",
                        (job_id, token)).fetchone() is not None


def update_claim(conn, job_id, token, **values):
    """A stale worker can neither renew a replaced lease nor overwrite its result."""
    permitted = {"status", "stage", "error", "content_hash"}
    if not values or not set(values).issubset(permitted):
        raise ValueError("unsupported job update fields")
    values["updated_at"] = _iso(_now())
    if values.get("status", "RUNNING") == "RUNNING":
        values["lease_expires_at"] = _expiry()
    else:
        values["lease_expires_at"] = None
        values["claim_token"] = None
    updated = conn.execute(
        "UPDATE ingestion_jobs SET " + ",".join(f"{key}=?" for key in values)
        + " WHERE job_id=? AND claim_token=? AND status='RUNNING'",
        (*values.values(), job_id, token)).rowcount
    conn.commit()
    return bool(updated)


def reclaim_expired(conn):
    """Recover expired leases, also covering legacy jobs that predate lease tokens."""
    now = _iso(_now())
    cutoff = _iso(_now() - timedelta(seconds=float(os.getenv("RAG_JOB_LEASE_SECONDS", "3600"))))
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        recovered = conn.execute(
            "UPDATE ingestion_jobs SET status='FAILED',stage='RECOVERED',"
            "error='abandoned worker; retrying',updated_at=?,claim_token=NULL,lease_expires_at=NULL "
            "WHERE status='RUNNING' AND ((lease_expires_at IS NOT NULL AND lease_expires_at<?) "
            "OR (lease_expires_at IS NULL AND (updated_at IS NULL OR updated_at<?)))",
            (now, now, cutoff)).rowcount
    return recovered