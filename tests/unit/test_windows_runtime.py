"""Native Windows launch plan and ownership-safe process lifecycle."""
import os
import socket
import sys
import time

def test_native_launch_plan_runs_worker_and_local_ocr(tmp_path):
    from scripts.windows_runtime import plan_services
    services = plan_services(tmp_path, {"RAG_TASK_BACKEND": "native", "RAG_OCR_MODE": "api",
                                       "RAG_OCR_API_URL": "http://127.0.0.1:10000/v1"})
    assert [s.name for s in services] == ["ocr", "worker", "api", "ui"]
    worker = services[1]
    assert "workers.native_worker" in worker.argv and "--stop-file" in worker.argv
    assert services[0].argv[0] == str(tmp_path / ".venv-ocr/Scripts/python.exe")
    assert services[0].port == 10000
    assert all("docker" not in " ".join(s.argv).lower() for s in services)

def test_external_windows_ocr_does_not_start_local_model(tmp_path):
    from scripts.windows_runtime import plan_services
    services = plan_services(tmp_path, {"RAG_TASK_BACKEND": "inline", "RAG_OCR_MODE": "api",
                                       "RAG_OCR_API_URL": "http://192.168.1.20:10000/v1"})
    assert [s.name for s in services] == ["api", "ui"]

def test_windows_launcher_refuses_unsupported_celery_profile(tmp_path):
    from scripts.windows_runtime import plan_services
    import pytest
    with pytest.raises(ValueError, match="native"):
        plan_services(tmp_path, {"RAG_TASK_BACKEND": "celery"})

def test_occupied_port_is_not_adopted_as_our_process(tmp_path):
    from scripts.windows_runtime import Service, start_service
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        service = Service("test", [sys.executable, "-c", "raise SystemExit(99)"], sock.getsockname()[1])
        registry = {}
        result = start_service(service, tmp_path, registry, dict(os.environ))
        assert result["state"] == "port_in_use"
        assert registry == {}

def test_stop_ignores_reused_process_id(tmp_path):
    from scripts.windows_runtime import stop_service
    import psutil
    process = psutil.Process()
    registry = {"unrelated": {"pid": process.pid, "created": process.create_time() - 1,
                              "exe": process.exe()}}
    assert stop_service("unrelated", registry)["state"] == "ownership_mismatch"
    assert process.is_running()

def test_managed_hidden_child_starts_and_stops_without_global_process_kill(tmp_path):
    from scripts.windows_runtime import Service, start_service, stop_service, process_matches
    registry = {}
    service = Service("owned-test", [sys.executable, "-u", "-c", "import time; print('ready'); time.sleep(60)"])
    result = start_service(service, tmp_path, registry, dict(os.environ))
    try:
        assert result["state"] == "started"
        assert process_matches(registry["owned-test"])
        assert start_service(service, tmp_path, registry, dict(os.environ))["state"] == "running"
    finally:
        stopped = stop_service("owned-test", registry)
    assert stopped["state"] == "stopped"
    assert not registry
