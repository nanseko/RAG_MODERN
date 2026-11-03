import json
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from api.dependencies import user_groups as current_groups
from app.settings import get_settings
from app.operations import queue_selected
from database.db import get_conn
from ingestion.collector import SUPPORTED, enqueue_file, source_key
from ingestion.pipeline import run_job, scan_once
from retrieval.hybrid import acl_filter

import workers
from workers import dispatch

router = APIRouter(prefix="/api/v1")


def _queue_backend() -> bool:
    return queue_selected()


def _execute_job(conn, job, queued):
    if not queued:
        return run_job(conn, job["job_id"])
    try:
        task_id = dispatch.dispatch_job(job["job_id"])
    except Exception as error:
        # SQL and the staged file are durable; Beat rescues lost delivery.
        return {"status": "PENDING", "job_id": job["job_id"],
                "document_id": job["document_id"], "task_id": None,
                "delivery_error": type(error).__name__}
    return {"status": "QUEUED", "job_id": job["job_id"],
            "document_id": job["document_id"], "task_id": task_id}


def _document(conn, did, groups):
    row = conn.execute("SELECT * FROM documents WHERE document_id=?", (did,)).fetchone()
    if not row or not acl_filter([dict(row)], groups):
        raise HTTPException(404, "문서를 찾을 수 없습니다")
    return dict(row)


@router.post("/documents")
def upload(f: UploadFile, user_groups: str | None = Form(default=None), groups=Depends(current_groups)):
    queued = _queue_backend()
    name = f.filename or ""
    if not name or "/" in name or "\\" in name or Path(name).name != name or name in (".", ".."):
        raise HTTPException(400, "파일 이름에 경로를 포함할 수 없습니다")
    if Path(name).suffix.lower() not in SUPPORTED:
        raise HTTPException(415, "지원하지 않는 문서 형식입니다")
    settings = get_settings()
    root = Path(settings.docs).resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / name
    if destination.is_symlink() or not destination.resolve().is_relative_to(root):
        raise HTTPException(400, "유효하지 않은 저장 경로입니다")
    allowed = [g.strip() for g in user_groups.split(",") if g.strip()] if user_groups is not None else None
    if allowed == []:
        raise HTTPException(400, "문서 허용 그룹이 비어 있습니다")
    conn = get_conn()
    temp = None
    try:
        # Refuse overwrite of an existing document that this local identity cannot read.
        for row in conn.execute("SELECT * FROM documents WHERE lower(file_name)=lower(?)", (name,)):
            if row["source_uri"] and source_key(row["source_uri"]) == source_key(destination):
                _document(conn, row["document_id"], groups)
        staging = root / ".uploads"
        staging.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=staging, prefix="upload-", suffix=Path(name).suffix, delete=False) as out:
            temp = Path(out.name)
            size = 0
            while block := f.file.read(65536):
                size += len(block)
                if size > int(os.getenv("RAG_UPLOAD_MAX_MB", "50")) * 1024 * 1024:
                    raise HTTPException(413, "업로드 허용 크기를 초과했습니다")
                out.write(block)
        job = enqueue_file(conn, destination, allowed_groups=allowed, input_path=temp)
        if job:
            temp = None  # durable job owns the staged file, including on FAILED
            return _execute_job(conn, job, queued)
        row = conn.execute("SELECT * FROM ingestion_jobs WHERE source_key=? ORDER BY rowid DESC LIMIT 1",
                           (source_key(destination),)).fetchone()
        if row and row["status"] in ("PENDING", "FAILED"):
            return _execute_job(conn, row, queued)
        return {"status": "SKIPPED_UNCHANGED", "document_id": row["document_id"] if row else None, "chunks": 0}
    finally:
        conn.close()
        if temp:
            temp.unlink(missing_ok=True)


@router.post("/scan")
@router.post("/preprocess")
def scan(groups=Depends(current_groups)):
    if _queue_backend():
        try:
            return {"status": "QUEUED", "task_id": dispatch.dispatch_scan()}
        except Exception as error:
            raise HTTPException(503, "작업 큐에 연결할 수 없습니다") from error
    out = scan_once()
    conn = get_conn()
    try:
        visible = []
        for item in out["results"]:
            job = conn.execute("SELECT * FROM ingestion_jobs WHERE job_id=?", (item["job_id"],)).fetchone()
            doc = conn.execute("SELECT allowed_groups FROM documents WHERE document_id=?", (item["document_id"],)).fetchone()
            acl = job["allowed_groups"] if job and job["allowed_groups"] is not None else (doc[0] if doc else ["all"])
            if acl_filter([{"allowed_groups": acl}], groups):
                visible.append(item)
        return {**out, "results": visible, "jobs": len(visible),
                "done": sum(r["status"] == "DONE" for r in visible),
                "failed": sum(r["status"] == "FAILED" for r in visible)}
    finally:
        conn.close()


@router.get("/documents")
def documents(groups=Depends(current_groups)):
    conn = get_conn()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT document_id,title,file_name,file_type,version,status,allowed_groups FROM documents ORDER BY file_name")]
        return {"documents": acl_filter(rows, groups)}
    finally:
        conn.close()


@router.get("/jobs")
def jobs(groups=Depends(current_groups)):
    conn = get_conn()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT j.job_id,j.document_id,j.file_name,j.status,j.stage,j.error,j.attempts,"
            "COALESCE(j.allowed_groups,d.allowed_groups,'[\"all\"]') AS allowed_groups "
            "FROM ingestion_jobs j LEFT JOIN documents d ON d.document_id=j.document_id "
            "ORDER BY j.rowid DESC LIMIT 100")]
        return {"jobs": acl_filter(rows, groups)}
    finally:
        conn.close()


@router.post("/jobs/{job_id}/retry")
def retry(job_id: str, groups=Depends(current_groups)):
    queued = _queue_backend()
    conn = get_conn()
    try:
        job = conn.execute("SELECT * FROM ingestion_jobs WHERE job_id=?", (job_id,)).fetchone()
        if not job or job["status"] != "FAILED":
            raise HTTPException(404, "재시도할 실패 작업이 없습니다")
        doc = conn.execute("SELECT allowed_groups FROM documents WHERE document_id=?", (job["document_id"],)).fetchone()
        acl = job["allowed_groups"] if job["allowed_groups"] is not None else (doc[0] if doc else ["all"])
        if not acl_filter([{"allowed_groups": acl}], groups):
            raise HTTPException(404, "작업을 찾을 수 없습니다")
        p = Path(job["file_path"]).resolve()
        if not p.is_relative_to(Path(get_settings().docs).resolve()):
            raise HTTPException(400, "원본 경로가 문서 폴더 밖에 있습니다")
        try:
            new = enqueue_file(conn, p, allowed_groups=json.loads(acl) if isinstance(acl, str) else acl,
                               force=True, input_path=job["input_path"])
        except OSError as e:
            raise HTTPException(404, "재처리할 원본 파일이 없습니다") from e
        return _execute_job(conn, new, queued)
    finally:
        conn.close()


@router.get("/documents/{document_id}")
def status(document_id: str, groups=Depends(current_groups)):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM documents WHERE document_id=?", (document_id,)).fetchone()
        job = conn.execute("SELECT * FROM ingestion_jobs WHERE document_id=? ORDER BY rowid DESC LIMIT 1",
                           (document_id,)).fetchone()
        if not row and not job:
            raise HTTPException(404, "문서를 찾을 수 없습니다")
        permission = dict(row or job)
        if permission.get("allowed_groups") is None:
            permission["allowed_groups"] = ["all"]
        if not acl_filter([permission], groups):
            raise HTTPException(404, "문서를 찾을 수 없습니다")
        body = {"document_id": document_id, "status": row["status"] if row else job["status"]}
        if row:
            body["version"] = row["version"]
        job_acl = job["allowed_groups"] if job and job["allowed_groups"] is not None else permission["allowed_groups"]
        if job and acl_filter([{"allowed_groups": job_acl}], groups):
            body.update({k: job[k] for k in ("stage", "error", "attempts")})
            body.update({"job_id": job["job_id"], "job_status": job["status"]})
        return body
    finally:
        conn.close()


def _download(did, groups, markdown, version=None):
    conn = get_conn()
    try:
        doc = _document(conn, did, groups)
        if doc["status"] != "ACTIVE":
            raise HTTPException(404, "사용 가능한 문서가 아닙니다")
        settings = get_settings()
        artifact = doc
        if version:
            row = conn.execute("SELECT * FROM document_versions WHERE document_id=? AND version=?",
                               (did, version)).fetchone()
            if not row or not acl_filter([dict(row)], groups):
                raise HTTPException(404, "해당 문서 버전을 찾을 수 없습니다")
            artifact = dict(row)
        value = artifact["markdown_path"] if markdown else (artifact.get("original_path") or doc["source_uri"])
        root = Path(settings.md if markdown else (settings.originals if artifact.get("original_path") else settings.docs)).resolve()
        if not value:
            raise HTTPException(404, "사본이 없습니다. 전처리를 실행하세요.")
        p = Path(value).resolve()
        if not p.is_relative_to(root) or not p.is_file():
            raise HTTPException(404, "파일을 찾을 수 없습니다")
        return FileResponse(p, filename=p.name if markdown else doc["file_name"])
    finally:
        conn.close()


@router.get("/documents/{document_id}/download")
def download(document_id: str, version: str | None = None, groups=Depends(current_groups)):
    return _download(document_id, groups, False, version)


@router.get("/documents/{document_id}/markdown")
def markdown(document_id: str, version: str | None = None, groups=Depends(current_groups)):
    return _download(document_id, groups, True, version)


@router.post("/documents/{document_id}/reindex")
def reindex(document_id: str, groups=Depends(current_groups)):
    queued = _queue_backend()
    conn = get_conn()
    try:
        doc = _document(conn, document_id, groups)
        p = Path(doc["source_uri"] or Path(get_settings().docs) / doc["file_name"]).resolve()
        if not p.is_relative_to(Path(get_settings().docs).resolve()) or not p.is_file():
            raise HTTPException(404, "원본 파일을 찾을 수 없습니다")
        job = enqueue_file(conn, p, allowed_groups=json.loads(doc["allowed_groups"]), force=True)
        return _execute_job(conn, job, queued)
    finally:
        conn.close()


@router.delete("/documents/{document_id}")
def delete(document_id: str, groups=Depends(current_groups)):
    queued = _queue_backend()
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _document(conn, document_id, groups)
        conn.execute("UPDATE ingestion_jobs SET status='CANCELLED' WHERE document_id=? AND status IN ('RUNNING','PENDING','FAILED')", (document_id,))
        conn.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
        conn.execute("UPDATE documents SET status='DELETED' WHERE document_id=?", (document_id,))
        from retrieval.opensearch_store import enqueue_delete, reconcile_selected
        enqueue_delete(conn, document_id)
        conn.commit()
        if queued:
            try:
                dispatch.dispatch_reconcile()
            except Exception:
                pass  # Beat will retry the durable tombstone.
            sync = {"status": "PENDING"}
        else:
            sync = reconcile_selected(conn)
        return {"document_id": document_id, "status": "DELETED", "search_sync": sync}
    finally:
        conn.close()
