"""Prevent double-click startup from allocating two copies of GPU models."""
from pathlib import Path
import pytest

def test_controller_lock_rejects_another_launcher_before_process_start(tmp_path):
    from scripts.windows_runtime import controller_lock
    with controller_lock(tmp_path):
        with pytest.raises(RuntimeError, match="launcher"):
            with controller_lock(tmp_path):
                pytest.fail("A second start must not allocate another OCR model")
    with controller_lock(tmp_path):
        pass

def test_managed_https_ocr_requires_explicit_external_management(tmp_path):
    from scripts.windows_runtime import plan_services
    with pytest.raises(ValueError, match="HTTP"):
        plan_services(tmp_path, {"RAG_TASK_BACKEND": "native", "RAG_OCR_MODE": "api",
                                "RAG_OCR_API_URL": "https://127.0.0.1/v1"})
    services = plan_services(tmp_path, {"RAG_TASK_BACKEND": "native", "RAG_OCR_MODE": "api",
            "RAG_OCR_API_URL": "https://127.0.0.1/v1", "RAG_MANAGE_OCR": "0"})
    assert all(s.name != "ocr" for s in services)
