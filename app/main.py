import asyncio
import os
from contextlib import asynccontextmanager
from threading import Lock

from fastapi.responses import JSONResponse, PlainTextResponse

from fastapi import FastAPI

from app.environment import load_environment

load_environment()

from api.routes.documents import router as d_router
from api.routes.query import router as q_router
from app.settings import get_settings
from app.operations import readiness, selected_backends
from ingestion.scheduler import DocumentScanner


@asynccontextmanager
async def lifespan(app):
    settings = get_settings()
    backends = selected_backends()
    scanner = None
    if backends["RAG_TASK_BACKEND"] != "native":
        scanner = DocumentScanner(settings.scan_interval_min * 60)
        if settings.autoscan:
            await asyncio.to_thread(scanner.scan)
        scanner.start()
    app.state.scanner = scanner
    try:
        yield
    finally:
        if scanner is not None:
            scanner.stop()


app = FastAPI(title="Local RAG", lifespan=lifespan)
app.include_router(q_router)
app.include_router(d_router)

_request_lock = Lock()
_request_count = 0


@app.middleware("http")
async def count_http_requests(request, call_next):
    global _request_count
    try:
        return await call_next(request)
    finally:
        with _request_lock:
            _request_count += 1


@app.get("/")
def root():
    return {"health": "/health", "docs": "/docs", "query": "POST /api/v1/query"}


@app.get("/health")
def health():
    from models.runtime import model_path
    settings = get_settings()
    backends = selected_backends()
    return {"status": "ok", "llm_mode": "live" if os.getenv("GEMMA_API_URL", "").strip() else "mock",
            "llm_model": os.getenv("GEMMA_MODEL", "gemma-4-31b"),
            "embedding_mode": "qwen" if os.getenv("USE_QWEN", "1") == "1" and os.path.isdir(
                model_path("models/qwen3-embedding-0.6b", "RAG_EMBEDDING_MODEL_DIR")) else "keyword_demo",
            "vector_store": "opensearch" if backends["RAG_SEARCH_BACKEND"] == "opensearch" else "sqlite-vec",
            "database_backend": backends["RAG_DB_BACKEND"], "task_backend": backends["RAG_TASK_BACKEND"],
            "ocr_mode": os.getenv("RAG_OCR_MODE", "disabled"),
            "scan_interval_min": settings.scan_interval_min}


@app.get("/ready")
def ready():
    result = readiness()
    return JSONResponse(result, status_code=200 if result["status"] == "ready" else 503)


@app.get("/metrics")
def metrics():
    from database.db import get_conn
    try:
        conn = get_conn()
        try:
            docs = conn.execute("SELECT COUNT(*) FROM documents WHERE status='ACTIVE'").fetchone()[0]
            jobs = conn.execute("SELECT COUNT(*) FROM ingestion_jobs WHERE status='PENDING'").fetchone()[0]
        finally:
            conn.close()
    except Exception:
        docs, jobs = -1, -1
    with _request_lock:
        requests_served = _request_count
    body = ("# HELP rag_http_request API requests served by this process\n"
            "# TYPE rag_http_request counter\n"
            f'rag_http_request{{route="all"}} {requests_served}\n' 
            f"rag_documents_active {docs}\n"
            f"rag_jobs_pending {jobs}\n")
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")
