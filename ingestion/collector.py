"""Recursive, canonical-path collection with isolated per-file failures."""
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

SUPPORTED = {".txt", ".md", ".markdown", ".html", ".htm", ".pdf",
             ".docx", ".pptx", ".xlsx", ".hwpx", ".doc", ".hwp", ".ppt",
             ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
PROCESSOR_VERSION = "ingest-v4"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def source_key(path):
    return os.path.normcase(str(Path(path).resolve()))


def _previous(conn, path):
    key = source_key(path)
    rows = conn.execute("SELECT rowid,* FROM ingestion_jobs WHERE source_key=? "
                        "OR lower(file_name)=lower(?) ORDER BY rowid DESC", (key, Path(path).name))
    return next((r for r in rows if source_key(r["file_path"]) == key), None)


def enqueue_file(conn, path, allowed_groups=None, force=False, input_path=None):
    path = Path(path).resolve()
    if path.suffix.lower() not in SUPPORTED:
        raise ValueError(f"unsupported: {path.suffix}")
    digest = _hash(input_path or path)
    # Serialize identity assignment as well as insertion.
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = _previous(conn, path)
        existing = conn.execute("SELECT * FROM documents WHERE document_id=?",
                                (previous["document_id"],)).fetchone() if previous else None
        if existing and existing["status"] == "DELETED" and not force:
            return None
        if previous and not force and previous["content_hash"] == digest:
            if previous["status"] in ("PENDING", "RUNNING", "FAILED"):
                return None
            if previous["processor_version"] == PROCESSOR_VERSION and (
                allowed_groups is None or not existing or json.loads(existing["allowed_groups"]) == allowed_groups
            ):
                return None
        # Failed staged upload must not reindex unchanged active disk content on every scan.
        if existing and input_path is None and existing["content_hash"] == digest and previous["input_path"]:
            return None
        did = previous["document_id"] if previous else uuid.uuid4().hex[:16]
        if previous and previous["status"] in ("PENDING", "FAILED"):
            conn.execute("UPDATE ingestion_jobs SET status='SUPERSEDED' WHERE job_id=?", (previous["job_id"],))
        jid = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO ingestion_jobs(job_id,document_id,file_name,file_path,file_type,"
            "content_hash,changed,status,stage,created_at,updated_at,allowed_groups,processor_version,source_key,input_path)"
            " VALUES(?,?,?,?,?,?,?,'PENDING','COLLECT',?,?,?,?,?,?)",
            (jid, did, path.name, str(path), path.suffix[1:].lower(), digest, int(previous is not None),
             _now(), _now(), json.dumps(allowed_groups) if allowed_groups is not None else None,
             PROCESSOR_VERSION, source_key(path), str(input_path) if input_path else None))
    return {"job_id": jid, "document_id": did, "file_name": path.name,
            "changed": previous is not None, "status": "PENDING"}


def _collection_failure(conn, path, error):
    previous = _previous(conn, path)
    if previous and previous["status"] in ("PENDING", "RUNNING", "FAILED"):
        return None
    jid = uuid.uuid4().hex
    did = previous["document_id"] if previous else uuid.uuid4().hex[:16]
    conn.execute(
        "INSERT INTO ingestion_jobs(job_id,document_id,file_name,file_path,file_type,content_hash,"
        "status,stage,error,created_at,updated_at,source_key,processor_version)"
        " VALUES(?,?,?,?,?,'','FAILED','COLLECT',?,?,?,?,?)",
        (jid, did, path.name, str(path), path.suffix[1:], str(error)[:500],
         _now(), _now(), source_key(path), PROCESSOR_VERSION))
    conn.commit()
    return {"job_id": jid, "document_id": did, "file_name": path.name, "status": "FAILED"}


def scan_dir(conn, docs_dir):
    root = Path(docs_dir).resolve()
    jobs = []
    for p in sorted(root.rglob("*")):
        if ".uploads" in p.relative_to(root).parts or not p.is_file() or p.suffix.lower() not in SUPPORTED:
            continue
        if p.is_symlink() or not p.resolve().is_relative_to(root):
            continue
        try:
            job = enqueue_file(conn, p)
        except OSError as e:
            job = _collection_failure(conn, p, e)
        if job:
            jobs.append(job)
    return jobs
