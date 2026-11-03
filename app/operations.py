"""Explicit selected backends and bounded, read-only dependency probes."""
import os


def selected_backends():
    values = {}
    for name, default, choices in (
        ("RAG_DB_BACKEND", "sqlite", ("sqlite", "postgresql")),
        ("RAG_SEARCH_BACKEND", "sqlite", ("sqlite", "opensearch")),
        ("RAG_TASK_BACKEND", "inline", ("inline", "native", "celery")),
    ):
        value = os.getenv(name, default).strip().lower()
        if value not in choices:
            raise ValueError(f"{name} must be {' or '.join(choices)}")
        values[name] = value
    if values["RAG_DB_BACKEND"] == "postgresql" and values["RAG_SEARCH_BACKEND"] != "opensearch":
        raise ValueError("PostgreSQL requires RAG_SEARCH_BACKEND=opensearch")
    return values


def queue_selected():
    return selected_backends()["RAG_TASK_BACKEND"] in ("native", "celery")


def probe_search():
    from retrieval.opensearch_store import (
        OpenSearchStore, OpenSearchError, SCHEMA_VERSION, TROUBLESHOOTING_KEYWORDS,
    )
    from database.vector_store import DIMENSION
    store = OpenSearchStore.from_env()
    store.timeout = min(store.timeout, 2)
    health = store.health()
    if not health.get("healthy"):
        raise OpenSearchError("Search cluster is not healthy")
    # Do not create indexes or change mappings in a readiness check.
    mapping = store._request("GET", f"/{store.index}/_mapping", allow_404=True)
    existing = (mapping or {}).get(store.index, {}).get("mappings", {})
    props = existing.get("properties", {})
    vector = props.get("embedding", {})
    if (existing.get("_meta", {}).get("rag_schema_version") != SCHEMA_VERSION
            or vector.get("dimension") != DIMENSION
            or vector.get("method", {}).get("engine") != "lucene"
            or props.get("source_fingerprint", {}).get("type") != "keyword"
            or any(props.get(field, {}).get("type") != "keyword" for field in TROUBLESHOOTING_KEYWORDS)):
        raise OpenSearchError("Search index is absent or incompatible")
    return {"status": "ready", "backend": "opensearch", "cluster_status": health.get("status")}


def probe_queue():
    if selected_backends()["RAG_TASK_BACKEND"] == "native":
        from database.db import get_conn
        from workers.native_queue import worker_health
        conn = get_conn()
        try:
            return worker_health(conn)
        finally:
            conn.close()
    from redis import Redis
    from workers.celery_app import celery_app
    # The result backend can use a separate Redis host/db from the broker.
    for url in (celery_app.conf.broker_url, celery_app.conf.result_backend):
        client = Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        try:
            if not client.ping():
                raise RuntimeError("Redis did not respond")
        finally:
            client.close()
    inspector = celery_app.control.inspect(timeout=1)
    replies = inspector.ping() or {}
    queues = (inspector.active_queues() or {}) if replies else {}
    workers = [name for name, reply in replies.items()
               if reply.get("ok") == "pong" and any(
                   item.get("name") == celery_app.conf.task_default_queue for item in queues.get(name, []))]
    if not workers:
        raise RuntimeError("No ingestion worker responded on the selected queue")
    return {"status": "ready", "backend": "celery", "workers": len(workers)}



def probe_ocr():
    from ingestion.ocr import UnlimitedOCR, OCRFailure, validate_snapshot
    adapter = UnlimitedOCR.from_environment()
    if adapter.mode == "local":
        if not adapter.python_path.is_file():
            raise OCRFailure("Selected OCR Python runtime is absent")
        validate_snapshot(adapter.model_dir)
    # Snapshot/API configuration validation is cheap; readiness never loads the
    # multi-GB model or sends a document to the OCR/LLM server.
    return {"status": "ready", "mode": adapter.mode,
            "check": "configuration", "inference_checked": False,
            "scanned_documents_enabled": adapter.mode != "disabled"}


def readiness():
    try:
        backends = selected_backends()
    except ValueError:
        return {"status": "not_ready", "services": {"configuration": {"status": "not_ready", "error": "ValueError"}}}
    services = {}
    conn = None
    try:
        from database.db import get_conn
        conn = get_conn()
        conn.execute("SELECT 1").fetchone()
        services["database"] = {"status": "ready", "backend": backends["RAG_DB_BACKEND"]}
        if backends["RAG_SEARCH_BACKEND"] == "sqlite":
            conn.execute("SELECT vec_version()").fetchone()
            services["search"] = {"status": "ready", "backend": "sqlite-vec"}
    except Exception as error:
        services["database"] = {"status": "not_ready", "error": type(error).__name__}
    finally:
        if conn is not None:
            conn.close()
    for name, selected, probe in (
        ("ocr", True, probe_ocr),
        ("search", backends["RAG_SEARCH_BACKEND"] == "opensearch", probe_search),
        ("queue", backends["RAG_TASK_BACKEND"] in ("native", "celery"), probe_queue),
    ):
        if selected:
            try:
                services[name] = probe()
            except Exception as error:
                # Connection errors may contain passwords or document data.
                services[name] = {"status": "not_ready", "error": type(error).__name__}
    return {"status": "ready" if all(s["status"] == "ready" for s in services.values()) else "not_ready", "services": services}
