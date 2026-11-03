"""Manage only this project's native Windows app processes; never global taskkill."""
import argparse
from dataclasses import dataclass
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

@dataclass
class Service:
    name: str
    argv: list[str]
    port: int | None = None
    stop_file: str | None = None

def plan_services(root, environment):
    root = Path(root).resolve()
    python = str(root / ".venv/Scripts/python.exe")
    backend = environment.get("RAG_TASK_BACKEND", "native").strip().lower()
    if backend not in ("native", "inline"):
        raise ValueError("Windows launcher requires RAG_TASK_BACKEND=native or inline")
    services = []
    url = urlsplit(environment.get("RAG_OCR_API_URL", ""))
    if (environment.get("RAG_OCR_MODE", "disabled") == "api"
            and url.hostname in ("127.0.0.1", "localhost", "::1")
            and environment.get("RAG_MANAGE_OCR", "1") == "1"):
        if url.scheme != "http":
            raise ValueError("Managed OCR requires loopback HTTP; set RAG_MANAGE_OCR=0 for a TLS proxy.")
        ocr_python = Path(environment.get("RAG_OCR_PYTHON_PATH") or ".venv-ocr/Scripts/python.exe")
        if not ocr_python.is_absolute():
            ocr_python = root / ocr_python
        model = str(Path(environment.get("RAG_OCR_MODEL_DIR") or root / "models/unlimited-ocr"))
        services.append(Service("ocr", [str(ocr_python), str(root / "scripts/run_ocr_server.py"),
            "--host", url.hostname, "--port", str(url.port or 10000), "--model-dir", model], url.port or 10000))
    if backend == "native":
        stop = str(root / "data/runtime/native-worker.stop")
        services.append(Service("worker", [python, "-m", "workers.native_worker", "--stop-file", stop],
                                stop_file=stop))
    services.extend([
        Service("api", [python, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"], 8000),
        Service("ui", [python, "-m", "streamlit", "run", str(root / "ui/streamlit_app.py"),
                       "--server.address", "127.0.0.1", "--server.port", "8501"], 8501),
    ])
    return services

def process_matches(record):
    import psutil
    try:
        process = psutil.Process(int(record["pid"]))
        return (abs(process.create_time() - record["created"]) < .01
                and os.path.normcase(process.exe()) == os.path.normcase(record["exe"])
                and process.is_running())
    except (psutil.Error, KeyError, TypeError, ValueError):
        return False

def port_busy(port):
    if port is None:
        return False
    with socket.socket() as probe:
        probe.settimeout(.3)
        return probe.connect_ex(("127.0.0.1", port)) == 0

def start_service(service, root, registry, environment):
    import psutil
    root = Path(root).resolve()
    if service.name in registry and process_matches(registry[service.name]):
        return {"service": service.name, "state": "running", "pid": registry[service.name]["pid"]}
    if port_busy(service.port):
        return {"service": service.name, "state": "port_in_use"}
    if not Path(service.argv[0]).is_file():
        return {"service": service.name, "state": "missing_interpreter"}
    logs = root / "data/runtime/logs"
    logs.mkdir(parents=True, exist_ok=True)
    if service.stop_file:
        Path(service.stop_file).unlink(missing_ok=True)
    with (logs / (service.name + ".log")).open("ab", buffering=0) as output:
        child = subprocess.Popen(service.argv, cwd=root, env=environment, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT,
            creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0)
    process = psutil.Process(child.pid)
    record = {"pid": child.pid, "created": process.create_time(), "exe": process.exe(),
              "log": str(logs / (service.name + ".log")), "stop_file": service.stop_file}
    registry[service.name] = record
    time.sleep(.2)
    if child.poll() is not None:
        registry.pop(service.name, None)
        return {"service": service.name, "state": "failed", "exit_code": child.returncode,
                "log": record["log"]}
    return {"service": service.name, "state": "started", "pid": child.pid, "log": record["log"]}

def stop_service(name, registry):
    import psutil
    record = registry.get(name)
    if not record:
        return {"service": name, "state": "not_managed"}
    if not process_matches(record):
        return {"service": name, "state": "ownership_mismatch"}
    if record.get("stop_file"):
        Path(record["stop_file"]).write_text("stop\n", encoding="utf8")
        # Active OCR/indexing work finishes before this worker exits.
        return {"service": name, "state": "stop_requested", "pid": record["pid"]}
    process = psutil.Process(record["pid"])
    process.terminate()
    try:
        process.wait(timeout=10)
    except psutil.TimeoutExpired:
        return {"service": name, "state": "stopping", "pid": record["pid"]}
    registry.pop(name, None)
    return {"service": name, "state": "stopped"}

def gpu_probe(interpreter):
    if not Path(interpreter).is_file():
        return {"ready": False, "error": "missing_ocr_environment"}
    code = ("import json,torch; print(json.dumps({'ready':bool(torch.cuda.is_available() and "
            "torch.cuda.is_bf16_supported()),'torch':torch.__version__,"
            "'cuda':torch.version.cuda,'gpu':torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,"
            "'arch':torch._C._cuda_getArchFlags() if hasattr(torch._C,'_cuda_getArchFlags') else ''}))")
    try:
        result = subprocess.run([str(interpreter), "-c", code], timeout=60, capture_output=True,
                                text=True, encoding="utf8",
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode:
            return {"ready": False, "error": "ocr_runtime_import_failed"}
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return {"ready": False, "error": "ocr_runtime_probe_failed"}

def wait_ocr(port, key, seconds):
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            headers = {"Authorization": "Bearer " + key} if key else {}
            with opener.open(Request(f"http://127.0.0.1:{port}/health", headers=headers), timeout=2) as response:
                if json.load(response).get("status") == "ready":
                    return True
        except (OSError, ValueError):
            pass
        time.sleep(1)
    return False

def doctor(root, environment):
    services = plan_services(root, environment)
    ocr = next((service for service in services if service.name == "ocr"), None)
    return {"platform": sys.platform, "profile": "Windows native",
            "task_backend": environment.get("RAG_TASK_BACKEND", "native"),
            "llm_configured": bool(environment.get("GEMMA_API_URL", "").strip()),
            "main_python_present": (Path(root) / ".venv/Scripts/python.exe").is_file(),
            "ocr": gpu_probe(ocr.argv[0]) if ocr else {"managed": False},
            "services": [{"service": s.name, "port": s.port,
                          "interpreter_present": Path(s.argv[0]).is_file()} for s in services]}

@contextmanager
def controller_lock(root):
    """An OS lock spans GPU startup so simultaneous run.bat calls cannot race."""
    import msvcrt
    path = Path(root) / "data/runtime/launcher.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("Another Windows launcher is starting or stopping services.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "status", "doctor"))
    args = parser.parse_args(argv)
    if os.name != "nt":
        parser.error("This launcher runs native Windows processes.")
    from app.environment import load_environment
    load_environment()
    environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    environment.setdefault("RAG_TASK_BACKEND", "native")
    if args.action == "doctor":
        result = doctor(ROOT, environment)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["main_python_present"] and result["ocr"].get("ready", True) else 1
    state = ROOT / "data/runtime/windows-processes.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    registry = json.loads(state.read_text(encoding="utf8")) if state.is_file() else {}
    results = []
    try:
        if args.action == "status":
            results = [{"service": name, "state": "running" if process_matches(row) else "stopped",
                        "pid": row["pid"], "log": row.get("log")} for name, row in registry.items()]
        elif args.action == "stop":
            # Stop taking new jobs before stopping OCR. A busy worker retains OCR.
            if "worker" in registry and process_matches(registry["worker"]):
                results.append(stop_service("worker", registry))
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and process_matches(registry["worker"]):
                    time.sleep(.2)
                if process_matches(registry["worker"]):
                    print(json.dumps({"services": results, "next": "Run stop again after active work finishes."},
                                     ensure_ascii=False, indent=2))
                    return 0
                registry.pop("worker", None)
            for name in ("ui", "api", "ocr"):
                results.append(stop_service(name, registry))
        else:
            for service in plan_services(ROOT, environment):
                if service.name == "ocr":
                    if service.name not in registry or not process_matches(registry[service.name]):
                        probe = gpu_probe(service.argv[0])
                        if not probe.get("ready"):
                            results.append({"service": "ocr", "state": "unavailable", "diagnosis": probe})
                            continue
                    environment["RAG_OCR_SERVER_KEY"] = environment.get("RAG_OCR_SERVER_KEY") or environment.get("RAG_OCR_API_KEY", "")
                result = start_service(service, ROOT, registry, environment)
                results.append(result)
                if service.name == "ocr" and result["state"] in ("started", "running"):
                    if not wait_ocr(service.port, environment.get("RAG_OCR_SERVER_KEY", ""),
                                    float(environment.get("RAG_OCR_STARTUP_TIMEOUT", "300"))):
                        result["state"] = "not_ready"
    finally:
        pending = state.with_suffix(".tmp")
        pending.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf8")
        pending.replace(state)
    print(json.dumps({"services": results}, ensure_ascii=False, indent=2))
    return 1 if any(item["state"] in ("failed", "unavailable", "missing_interpreter", "not_ready", "port_in_use")
                    for item in results) else 0

def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] in ("start", "stop"):
        try:
            with controller_lock(ROOT):
                return _main(arguments)
        except RuntimeError as exc:
            print(json.dumps({"status": "busy", "message": str(exc)}))
            return 1
    return _main(arguments)

if __name__ == "__main__":
    raise SystemExit(main())
