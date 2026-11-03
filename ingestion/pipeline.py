"""Durable ingestion with leases, per-document claims and immutable artifacts."""
import json
import os
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.settings import get_settings
from ingestion.collector import _hash
from database.jobs import claim_job, owns_claim, reclaim_expired, update_claim
from retrieval.opensearch_store import reconcile_selected

MAX_ATTEMPTS = 3
_SCAN_LOCK = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class ClaimLostError(RuntimeError):
    pass


def _set(conn, jid, token, **values):
    if not update_claim(conn, jid, token, **values):
        raise ClaimLostError("job lease was replaced or cancelled")


def _bump(version):
    try:
        return f"v{int(version.lstrip('v')) + 1}"
    except (ValueError, AttributeError):
        return "v1"


def reclaim_abandoned(conn):
    return reclaim_expired(conn)


def run_job(conn, job_id, md_dir=None, *, on_claim=None):
    from ingestion.chunker import hybrid_chunk
    from ingestion.indexer import _chunk_embeddings, index_document
    from ingestion.normalizer import normalize
    from ingestion.parser import parse_file

    job = conn.execute("SELECT rowid,* FROM ingestion_jobs WHERE job_id=?", (job_id,)).fetchone()
    if job is None:
        return {"job_id": job_id, "status": "NOT_FOUND"}
    result = {"job_id": job_id, "document_id": job["document_id"], "file_name": job["file_name"]}
    if job["status"] in ("DONE", "SUPERSEDED"):
        return {**result, "status": job["status"], "search_sync": reconcile_selected(conn)}
    if job["attempts"] >= MAX_ATTEMPTS:
        return {**result, "status": "FAILED", "error": "max retries"}
    token = claim_job(conn, job_id, MAX_ATTEMPTS)
    if token is None:
        status = conn.execute("SELECT status FROM ingestion_jobs WHERE job_id=?", (job_id,)).fetchone()
        return {**result, "status": status[0] if status else "NOT_FOUND"}
    temp_md = None
    snapshot = None
    backup = None
    published = False
    input_file = Path(job["input_path"] or job["file_path"])
    destination = Path(job["file_path"])
    try:
        if on_claim is not None:
            on_claim(token)
        settings = get_settings()
        digest = _hash(input_file)
        if job["content_hash"] and digest != job["content_hash"]:
            raise ValueError("file changed after collection; scan again")
        originals = Path(settings.originals)
        originals.mkdir(parents=True, exist_ok=True)
        # Parse stable bytes, not a source file that could change during a long model call.
        with tempfile.NamedTemporaryFile(dir=originals, suffix=input_file.suffix, delete=False) as f:
            snapshot = Path(f.name)
        shutil.copyfile(input_file, snapshot)
        if _hash(snapshot) != digest:
            raise ValueError("file changed during snapshot; scan again")
        sections = parse_file(str(snapshot))
        _set(conn, job_id, token, stage="NORMALIZE")
        for section in sections:
            section["title"] = job["file_name"]
            section["text"] = normalize(section.get("text", ""))
        if not any(s["text"].strip() for s in sections):
            raise ValueError("텍스트를 추출하지 못했습니다. 스캔 PDF는 OCR 또는 텍스트 변환이 필요합니다.")
        _set(conn, job_id, token, stage="CHUNKING")
        chunks = hybrid_chunk(sections, settings.target_tokens, settings.max_tokens)
        _set(conn, job_id, token, stage="EMBEDDING")
        vectors = _chunk_embeddings([c["text"] for c in chunks])
        md_root = Path(md_dir or settings.md)
        md_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf8", dir=md_root,
                                         suffix=".tmp", delete=False) as f:
            temp_md = Path(f.name)
            f.write(f"# {job['file_name']}\n\n")
            for s in sections:
                if s.get("heading_path"):
                    f.write(f"## {s['heading_path']}\n\n")
                if s.get("page_number"):
                    f.write(f"<!-- page: {s['page_number']} -->\n")
                f.write(s["text"] + "\n\n")
        _set(conn, job_id, token, stage="INDEXING")
        conn.execute("BEGIN IMMEDIATE")
        try:
            latest = conn.execute("SELECT rowid FROM ingestion_jobs WHERE document_id=? ORDER BY rowid DESC LIMIT 1",
                                  (job["document_id"],)).fetchone()[0]
            if latest != job["rowid"]:
                raise ValueError("superseded by a newer job")
            if _hash(input_file) != digest:
                raise ValueError("file changed during processing; scan again")
            if not owns_claim(conn, job_id, token):
                raise ClaimLostError("job lease was replaced or cancelled before commit")
            existing = conn.execute("SELECT version,allowed_groups FROM documents WHERE document_id=?",
                                    (job["document_id"],)).fetchone()
            version = _bump(existing["version"]) if existing else "v1"
            groups = json.loads(job["allowed_groups"]) if job["allowed_groups"] is not None else (
                json.loads(existing["allowed_groups"]) if existing else ["all"])
            md_path = md_root / f"{Path(job['file_name']).stem}--{job['document_id']}--{version}.md"
            original_path = originals / f"{job['document_id']}--{version}.{job['file_type']}"
            index_document(conn, {
                "document_id": job["document_id"], "title": job["file_name"],
                "file_name": job["file_name"], "file_type": job["file_type"], "version": version,
                "allowed_groups": groups, "content_hash": digest, "source_uri": str(destination.resolve()),
                "markdown_path": str(md_path.resolve()), "original_path": str(original_path.resolve()),
            }, chunks, vectors=vectors, commit=False)
            n = conn.execute("SELECT COUNT(*) FROM chunks WHERE document_id=?", (job["document_id"],)).fetchone()[0]
            if n != len(chunks) or n == 0:
                raise ValueError("index validation failed")
            os.replace(temp_md, md_path)
            temp_md = None
            os.replace(snapshot, original_path)
            snapshot = None
            if job["input_path"]:
                if destination.exists():
                    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".backup", delete=False) as f:
                        backup = Path(f.name)
                    shutil.copyfile(destination, backup)
                os.replace(input_file, destination)
                published = True
            completed = conn.execute(
                "UPDATE ingestion_jobs SET status='DONE',stage='ACTIVE',content_hash=?,updated_at=?,"
                "claim_token=NULL,lease_expires_at=NULL WHERE job_id=? AND claim_token=? AND status='RUNNING'",
                (digest, _now(), job_id, token)).rowcount
            if not completed:
                raise ClaimLostError("job lease lost at commit")
            conn.commit()
        except Exception:
            conn.rollback()
            if published:
                os.replace(destination, input_file)
                if backup:
                    os.replace(backup, destination)
                    backup = None
            raise
        return {**result, "status": "DONE", "chunks": n, "version": version, "search_sync": reconcile_selected(conn)}
    except Exception as e:
        conn.rollback()
        if not update_claim(conn, job_id, token, status="FAILED", stage="FAILED", error=str(e)[:500]):
            return {**result, "status": "LOST_CLAIM", "error": str(e)[:500]}
        return {**result, "status": "FAILED", "error": str(e)[:500]}
    finally:
        for artifact in (temp_md, snapshot, backup):
            if artifact:
                artifact.unlink(missing_ok=True)


def retry_job(conn, job_id, md_dir=None):
    return run_job(conn, job_id, md_dir=md_dir)


def scan_once(docs_dir=None, db_path=None, md_dir=None):
    from database.db import get_conn
    from ingestion.collector import scan_dir
    settings = get_settings()
    root = Path(docs_dir or settings.docs).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not _SCAN_LOCK.acquire(blocking=False):
        return {"jobs": 0, "done": 0, "failed": 0, "status": "BUSY", "results": []}
    conn = None
    try:
        conn = get_conn(db_path or settings.db)
        reclaim_abandoned(conn)
        jobs = scan_dir(conn, str(root))
        pending = conn.execute("SELECT * FROM ingestion_jobs WHERE (status='PENDING' OR (status='FAILED' AND stage!='COLLECT')) AND attempts<? ORDER BY rowid",
                               (MAX_ATTEMPTS,)).fetchall()
        results = [run_job(conn, r["job_id"], md_dir=md_dir) for r in pending
                   if Path(r["file_path"]).resolve().is_relative_to(root)]
        for r in conn.execute("SELECT * FROM ingestion_jobs WHERE status='FAILED' AND stage='COLLECT'"):
            if Path(r["file_path"]).resolve().is_relative_to(root):
                results.append({"job_id": r["job_id"], "document_id": r["document_id"], "file_name": r["file_name"],
                                "status": "FAILED", "error": r["error"]})
        return {"jobs": len(jobs), "done": sum(r["status"] == "DONE" for r in results),
                "failed": sum(r["status"] == "FAILED" for r in results), "status": "DONE", "results": results, "search_sync": reconcile_selected(conn)}
    finally:
        if conn:
            conn.close()
        _SCAN_LOCK.release()
