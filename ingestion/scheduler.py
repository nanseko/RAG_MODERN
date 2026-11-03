"""Bounded periodic worker; a failed scan does not kill future scans."""
import logging
import threading

from ingestion.pipeline import scan_once
from app.operations import queue_selected

logger = logging.getLogger(__name__)


class DocumentScanner:
    def __init__(self, interval_seconds):
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self.last_result = None
        self.last_error = None
        self.thread = threading.Thread(target=self._loop, name="rag-document-scan", daemon=True)

    def scan(self):
        try:
            if queue_selected():
                from workers.dispatch import dispatch_scan
                self.last_result = {"status": "QUEUED", "task_id": dispatch_scan()}
            else:
                self.last_result = scan_once()
            self.last_error = None
        except Exception as e:
            self.last_result = None
            self.last_error = type(e).__name__
            logger.exception("Document scan failed; the next interval will retry")
        return self.last_result

    def _loop(self):
        while not self._stop.wait(self.interval_seconds):
            self.scan()

    def start(self):
        if self.interval_seconds > 0:
            self.thread.start()

    def stop(self):
        self._stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)
