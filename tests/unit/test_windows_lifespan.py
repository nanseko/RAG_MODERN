from fastapi.testclient import TestClient
from app.main import app

def test_native_worker_owns_automatic_scans_without_api_scanner(monkeypatch):
    import app.main as main
    monkeypatch.setenv("RAG_TASK_BACKEND", "native")
    def no_scanner(*args, **kwargs):
        raise AssertionError("Native worker owns startup and periodic scans")
    monkeypatch.setattr(main, "DocumentScanner", no_scanner)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert app.state.scanner is None
