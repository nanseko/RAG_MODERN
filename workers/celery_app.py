"""Internal Redis/Celery application; never accept executable task payloads."""
import ipaddress
import os
from urllib.parse import urlparse

from celery import Celery

from app.environment import load_environment


def _internal_redis_url(value):
    parsed = urlparse(value)
    if parsed.scheme not in ("redis", "rediss") or not parsed.hostname:
        raise ValueError("Celery broker/result backend must be a Redis URL")
    host = parsed.hostname.lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        permitted = {name.strip().lower() for name in os.getenv("RAG_REDIS_ALLOWED_HOSTS", "").split(",") if name.strip()}
        internal = (host == "localhost" or "." not in host
                    or host.endswith((".internal", ".local", ".lan")) or host in permitted)
    else:
        internal = address.is_private or address.is_loopback
    if not internal:
        raise ValueError("Redis must be an internal host; authorize internal DNS names with RAG_REDIS_ALLOWED_HOSTS")
    return value


def create_celery_app():
    load_environment()
    broker = _internal_redis_url(os.getenv("RAG_REDIS_URL") or os.getenv("REDIS_URL") or "redis://127.0.0.1:6379/0")
    backend = _internal_redis_url(os.getenv("RAG_REDIS_RESULT_URL") or broker)
    visibility = int(os.getenv("RAG_QUEUE_VISIBILITY_SECONDS", "86400"))
    if visibility < 60:
        raise ValueError("RAG_QUEUE_VISIBILITY_SECONDS must be at least 60")
    app = Celery("rag", broker=broker, backend=backend, include=["workers.tasks"])
    app.conf.update(
        task_serializer="json", result_serializer="json", accept_content=["json"],
        result_accept_content=["json"], task_acks_late=True, task_reject_on_worker_lost=True,
        task_track_started=True, worker_prefetch_multiplier=1,
        broker_connection_retry_on_startup=True, result_expires=86400,
        broker_transport_options={"visibility_timeout": visibility},
        result_backend_transport_options={"visibility_timeout": visibility},
        visibility_timeout=visibility, task_default_queue="rag-ingestion", timezone="UTC", enable_utc=True,
    )
    interval = float(os.getenv("RAG_QUEUE_RECONCILE_SECONDS", "60"))
    if interval <= 0:
        raise ValueError("RAG_QUEUE_RECONCILE_SECONDS must be positive")
    app.conf.beat_schedule = {
        "rag-reconcile": {"task": "rag.reconcile", "schedule": interval},
    }
    return app


celery_app = create_celery_app()